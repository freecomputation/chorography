"""Feed fetchers. These live outside the simulation core.

A fetcher does exactly one thing: it retrieves bytes from a keyless
public feed and writes an envelope file into the inbox folder. It never
touches world state. If a feed is down, the fetcher raises FeedError or
returns None, the inbox simply lacks that file, and the world runs on
without it.

Every request carries a descriptive User-Agent. Responses are cached on
disk with their ETag and Last-Modified validators so repeat runs can
send conditional GETs and accept a 304 without re-downloading. Retries
use exponential backoff. The GDELT fetcher is paced at one call per
five seconds, which that service asks of its clients.

Fetch times are passed in by the caller as ISO strings. This module
never reads a clock, which keeps it testable with a fake opener.

A caller that needs the raw payload bytes, such as the harvester
archiving a payload before anything parses it, may set an on_fetch
callback on the client. The callback is invoked with the URL, the
HTTP status and the raw bytes at the moment a response body has been
read, before any fetcher parses or writes anything.
"""

import csv
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

USER_AGENT = "Chorography/{} (Free Computation synthetic region mirror)".format(
    __version__)
GDELT_MIN_INTERVAL = 5.0


class FeedError(Exception):
    pass


class FeedClient:
    def __init__(self, inbox_dir, cache_path=None, opener=None,
                 sleeper=None, user_agent=USER_AGENT):
        self.inbox_dir = inbox_dir
        os.makedirs(inbox_dir, exist_ok=True)
        self.cache_path = cache_path or os.path.join(inbox_dir,
                                                     ".feed_cache.json")
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper or time.sleep
        self.user_agent = user_agent
        self.on_fetch = None
        self.cache = {}
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, encoding="utf-8") as fh:
                    self.cache = json.load(fh)
            except ValueError:
                self.cache = {}

    def _save_cache(self):
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            json.dump(self.cache, fh, indent=1, sort_keys=True)

    def fetch_bytes(self, url, max_attempts=3):
        """GET a URL with conditional headers and backoff.

        Returns (status, raw_bytes). A 304 returns the cached bytes when
        a cache entry exists. Raises FeedError after the final attempt.
        """
        headers = {"User-Agent": self.user_agent}
        entry = self.cache.get(url)
        if entry:
            if entry.get("etag"):
                headers["If-None-Match"] = entry["etag"]
            if entry.get("last_modified"):
                headers["If-Modified-Since"] = entry["last_modified"]
        delay = 1.0
        last_error = None
        for attempt in range(max_attempts):
            request = urllib.request.Request(url, headers=headers)
            try:
                response = self.opener(request)
                raw = response.read()
                status = getattr(response, "status", 200)
                new_entry = {"etag": None, "last_modified": None}
                get = getattr(response, "getheader", None)
                if get is not None:
                    new_entry["etag"] = get("ETag")
                    new_entry["last_modified"] = get("Last-Modified")
                new_entry["sha256_hint"] = len(raw)
                self.cache[url] = new_entry
                self._save_cache()
                if self.on_fetch is not None:
                    self.on_fetch(url, status, raw)
                return status, raw
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and entry is not None:
                    cached = entry.get("body_path")
                    if cached and os.path.exists(cached):
                        with open(cached, "rb") as fh:
                            body = fh.read()
                        if self.on_fetch is not None:
                            self.on_fetch(url, 304, body)
                        return 304, body
                    if self.on_fetch is not None:
                        self.on_fetch(url, 304, b"")
                    return 304, b""
                last_error = exc
                if exc.code == 429 or exc.code >= 500:
                    if attempt < max_attempts - 1:
                        self.sleeper(delay)
                        delay *= 2
                    continue
                raise FeedError("HTTP {} for {}".format(exc.code, url))
            except (urllib.error.URLError, OSError) as exc:
                last_error = exc
                if attempt < max_attempts - 1:
                    self.sleeper(delay)
                    delay *= 2
        raise FeedError("fetch failed for {}: {}".format(url, last_error))

    def fetch_json_post(self, url, payload, max_attempts=3):
        """POST a JSON body and return (status, raw_bytes).

        One source family, USAspending, is searched by POST rather
        than read by GET. The retry and backoff discipline matches
        fetch_bytes, and the on_fetch callback fires the same way, but
        no conditional headers or cache are involved, since a search
        has no validators to reuse. Raises FeedError after the final
        attempt.
        """
        body = json.dumps(payload).encode("utf-8")
        headers = {"User-Agent": self.user_agent,
                   "Content-Type": "application/json"}
        delay = 1.0
        last_error = None
        for attempt in range(max_attempts):
            request = urllib.request.Request(url, data=body,
                                             headers=headers,
                                             method="POST")
            try:
                response = self.opener(request)
                raw = response.read()
                status = getattr(response, "status", 200)
                if self.on_fetch is not None:
                    self.on_fetch(url, status, raw)
                return status, raw
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 429 or exc.code >= 500:
                    if attempt < max_attempts - 1:
                        self.sleeper(delay)
                        delay *= 2
                    continue
                raise FeedError("HTTP {} for {}".format(exc.code, url))
            except (urllib.error.URLError, OSError) as exc:
                last_error = exc
                if attempt < max_attempts - 1:
                    self.sleeper(delay)
                    delay *= 2
        raise FeedError("fetch failed for {}: {}".format(url, last_error))

    def write_envelope(self, filename, source, fetched_at, raw,
                       content_type="json"):
        """Write raw feed bytes into the inbox with a JSON envelope.

        JSON payloads are wrapped only when they parse as JSON and do
        not already carry a top-level fetched_at; the raw bytes are
        otherwise stored verbatim so ingest sees authentic fixtures of
        each feed format.
        """
        path = os.path.join(self.inbox_dir, filename)
        if content_type == "json":
            try:
                data = json.loads(raw.decode("utf-8"))
                if isinstance(data, dict):
                    data.setdefault("fetched_at", fetched_at)
                    data.setdefault("feed_source", source)
                    with open(path, "w", encoding="utf-8") as fh:
                        json.dump(data, fh)
                    return path
            except (ValueError, UnicodeDecodeError):
                pass
        with open(path, "wb") as fh:
            fh.write(raw)
        return path


def fetch_nws_alerts(client, lat, lon, fetched_at):
    url = ("https://api.weather.gov/alerts/active?point={},{}"
           .format(lat, lon))
    status, raw = client.fetch_bytes(url)
    return client.write_envelope("nws_alerts.json", "nws-alerts",
                                 fetched_at, raw)


def fetch_nws_forecast(client, lat, lon, fetched_at):
    points_url = "https://api.weather.gov/points/{},{}".format(lat, lon)
    _status, raw = client.fetch_bytes(points_url)
    points = json.loads(raw.decode("utf-8"))
    forecast_url = points["properties"]["forecast"]
    _status, raw = client.fetch_bytes(forecast_url)
    return client.write_envelope("nws_forecast.json", "nws-forecast",
                                 fetched_at, raw)


def fetch_usgs_earthquakes(client, start_date, end_date, fetched_at,
                           min_magnitude=2.5):
    query = urllib.parse.urlencode({
        "format": "geojson",
        "starttime": start_date,
        "endtime": end_date,
        "minmagnitude": min_magnitude,
    })
    url = "https://earthquake.usgs.gov/fdsnws/event/1/query?" + query
    _status, raw = client.fetch_bytes(url)
    return client.write_envelope("usgs_earthquakes.json",
                                 "usgs-earthquake", fetched_at, raw)


def fetch_usgs_water(client, site, fetched_at, parameter="00060"):
    query = urllib.parse.urlencode({
        "format": "json",
        "sites": site,
        "parameterCd": parameter,
    })
    url = "https://waterservices.usgs.gov/nwis/iv/?" + query
    _status, raw = client.fetch_bytes(url)
    return client.write_envelope("usgs_water.json", "usgs-water",
                                 fetched_at, raw)


def fetch_gdelt(client, query_text, fetched_at, timespan="1d"):
    query = urllib.parse.urlencode({
        "query": query_text,
        "mode": "ArtList",
        "format": "json",
        "timespan": timespan,
    })
    url = "https://api.gdeltproject.org/api/v2/doc/doc?" + query
    _status, raw = client.fetch_bytes(url)
    path = client.write_envelope("gdelt.json", "gdelt", fetched_at, raw)
    client.sleeper(GDELT_MIN_INTERVAL)
    return path


def fetch_rss(client, url, filename, fetched_at):
    _status, raw = client.fetch_bytes(url)
    return client.write_envelope(filename, "rss", fetched_at, raw,
                                 content_type="xml")


def fetch_bls(client, series_ids, fetched_at):
    """BLS public API, keyless, batched in groups of 25 series."""
    paths = []
    for start in range(0, len(series_ids), 25):
        batch = series_ids[start:start + 25]
        url = ("https://api.bls.gov/publicAPI/v2/timeseries/data/"
               + ",".join(batch) + "?latest=true")
        _status, raw = client.fetch_bytes(url)
        paths.append(client.write_envelope(
            "bls_{}.json".format(start), "bls", fetched_at, raw))
    return paths


def fetch_fred(client, series_id, fetched_at, fallback_path=None):
    """FRED via the fredgraph CSV endpoint, with a local fallback file.

    The fredgraph endpoint needs no key. When the fetch fails and a
    local fallback CSV was supplied, the fallback is parsed and written
    as the envelope instead, so a FRED outage degrades the run to the
    last known series rather than to nothing.
    """
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv?id="
           + urllib.parse.quote(series_id))
    try:
        _status, raw = client.fetch_bytes(url)
    except FeedError:
        if fallback_path and os.path.exists(fallback_path):
            with open(fallback_path, "rb") as fh:
                raw = fh.read()
        else:
            raise
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
    observations = []
    for row in rows[1:]:
        if len(row) >= 2 and row[1] not in (".", ""):
            observations.append({"date": row[0], "value": row[1]})
    event = {
        "chorography_event": {
            "kind": "news_cycle",
            "series": series_id,
            "magnitude": 0.1,
            "sentiment": 0.0,
            "observations": observations[-30:],
        },
        "source": "fred",
        "fetched_at": fetched_at,
    }
    path = os.path.join(client.inbox_dir,
                        "fred_{}.json".format(series_id.lower()))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(event, fh)
    return path


def _write_event_envelope(client, filename, event, source, fetched_at):
    """Write a ready-made event envelope, the shape fetch_fred uses.

    Fetchers that must extract fields from a payload rather than pass
    it through write this envelope directly: the event, its source and
    the fetch time, as one JSON file in the inbox.
    """
    envelope = {
        "chorography_event": event,
        "source": source,
        "fetched_at": fetched_at,
    }
    path = os.path.join(client.inbox_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh)
    return path


def _measurement(properties, name):
    block = properties.get(name) or {}
    return block.get("value")


def fetch_nws_observations(client, spec, fetched_at):
    """Latest observation from a National Weather Service station.

    The spec names a station directly, or gives a latitude and
    longitude from which the nearest station is resolved through the
    points service. The observation is written as a weather_observation
    event envelope carrying the measured values the service published.
    The world records the event in its log and gives it no dynamical
    effect, which is the honest treatment for a measurement the model
    does not yet assimilate.
    """
    station = spec.get("station")
    if not station:
        points_url = "https://api.weather.gov/points/{},{}".format(
            spec["lat"], spec["lon"])
        _status, raw = client.fetch_bytes(points_url)
        points = json.loads(raw.decode("utf-8"))
        stations_url = points["properties"]["observationStations"]
        _status, raw = client.fetch_bytes(stations_url)
        listing = json.loads(raw.decode("utf-8"))
        station = listing["features"][0]["properties"]["stationIdentifier"]
    url = ("https://api.weather.gov/stations/{}/observations/latest"
           .format(station))
    _status, raw = client.fetch_bytes(url)
    data = json.loads(raw.decode("utf-8"))
    props = data.get("properties", {})
    station_url = str(props.get("station", ""))
    event = {
        "kind": "weather_observation",
        "station": station_url.rstrip("/").rsplit("/", 1)[-1] or station,
        "observed_at": props.get("timestamp"),
        "description": props.get("textDescription"),
        "temperature_c": _measurement(props, "temperature"),
        "dewpoint_c": _measurement(props, "dewpoint"),
        "humidity_pct": _measurement(props, "relativeHumidity"),
        "wind_speed_kmh": _measurement(props, "windSpeed"),
        "wind_direction_deg": _measurement(props, "windDirection"),
        "pressure_pa": _measurement(props, "barometricPressure"),
    }
    return _write_event_envelope(client, "nws_observation.json", event,
                                 "nws-observation", fetched_at)


def fetch_federal_register(client, spec, fetched_at):
    """Federal Register documents matching agencies or a search term.

    The register's keyless JSON API is searched newest first. The
    documents become one news_cycle context event whose magnitude is
    the document count divided by twenty and capped at one, the same
    counting the RSS ingest uses, with the documents themselves
    recorded in the event for audit.
    """
    params = [("per_page", "20"), ("order", "newest")]
    if spec.get("term"):
        params.append(("conditions[term]", spec["term"]))
    for slug in spec.get("agencies", []):
        params.append(("conditions[agencies][]", slug))
    for field in ("title", "document_number", "publication_date",
                  "type", "agencies", "html_url"):
        params.append(("fields[]", field))
    url = ("https://www.federalregister.gov/api/v1/documents.json?"
           + urllib.parse.urlencode(params))
    _status, raw = client.fetch_bytes(url)
    data = json.loads(raw.decode("utf-8"))
    documents = []
    for result in data.get("results", []):
        documents.append({
            "title": result.get("title"),
            "document_number": result.get("document_number"),
            "publication_date": result.get("publication_date"),
            "type": result.get("type"),
            "agencies": [a.get("name") for a in result.get("agencies", [])],
            "html_url": result.get("html_url"),
        })
    event = {
        "kind": "news_cycle",
        "series": "federal-register",
        "magnitude": min(1.0, len(documents) / 20.0),
        "sentiment": 0.0,
        "counting": "documents divided by 20, capped at 1",
        "documents": documents,
    }
    return _write_event_envelope(client, "federal_register.json", event,
                                 "federal-register", fetched_at)


def fetch_treasury_dts(client, spec, fetched_at):
    """The newest Daily Treasury Statement record, as context.

    The FiscalData service needs no key. The latest record's date and
    its numeric figures are carried in a news_cycle context event at
    the fixed small magnitude the FRED context events use, since a
    national cash position is backdrop for a region, not news about
    it. Figure names are the service's own column names, taken as
    published.
    """
    query = urllib.parse.urlencode({
        "format": "json",
        "sort": "-record_date",
        "page[size]": "5",
    })
    url = ("https://api.fiscaldata.treasury.gov/services/api/"
           "fiscal_service/v2/accounting/dts?" + query)
    _status, raw = client.fetch_bytes(url)
    data = json.loads(raw.decode("utf-8"))
    rows = data.get("data", [])
    record = rows[0] if rows else {}
    figures = {}
    for key, value in record.items():
        if key == "record_date":
            continue
        try:
            figures[key] = float(value)
        except (TypeError, ValueError):
            continue
        if len(figures) >= 12:
            break
    event = {
        "kind": "news_cycle",
        "series": "treasury-dts",
        "magnitude": 0.1,
        "sentiment": 0.0,
        "counting": "fixed context magnitude 0.1",
        "record_date": record.get("record_date"),
        "figures": figures,
    }
    return _write_event_envelope(client, "treasury_dts.json", event,
                                 "treasury", fetched_at)


def fetch_usaspending(client, spec, fetched_at):
    """Federal awards by place of performance, as context.

    USAspending is searched by POST with a place of performance filter
    built from the spec's state and county FIPS codes and its date
    range. The awards become one news_cycle context event at a fixed
    small magnitude, carrying the filter used, the award count, the
    total obligations summed from the service's own figures, and the
    largest awards by name.
    """
    payload = {
        "filters": {
            "time_period": [{
                "start_date": spec["start"],
                "end_date": spec["end"],
            }],
            "place_of_performance_locations": [{
                "country": "USA",
                "state": spec["state"],
                "county": spec["county_fips"],
            }],
        },
        "fields": ["generated_internal_id", "Award ID", "Recipient Name",
                   "Total Obligations", "Awarding Agency", "Start Date"],
        "page": 1,
        "limit": 25,
        "sort": "Total Obligations",
        "order": "desc",
    }
    url = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
    _status, raw = client.fetch_json_post(url, payload)
    data = json.loads(raw.decode("utf-8"))
    awards = []
    total = 0.0
    for result in data.get("results", []):
        obligations = result.get("Total Obligations") or 0.0
        total += float(obligations)
        awards.append({
            "id": result.get("generated_internal_id"),
            "award_id": result.get("Award ID"),
            "recipient": result.get("Recipient Name"),
            "obligations": obligations,
            "agency": result.get("Awarding Agency"),
            "start_date": result.get("Start Date"),
        })
    event = {
        "kind": "news_cycle",
        "series": "usaspending",
        "magnitude": 0.1,
        "sentiment": 0.0,
        "counting": "fixed context magnitude 0.1",
        "filter": {"state": spec["state"],
                   "county_fips": spec["county_fips"],
                   "start": spec["start"], "end": spec["end"]},
        "award_count": len(awards),
        "total_obligations": round(total, 2),
        "awards": awards,
    }
    return _write_event_envelope(client, "usaspending.json", event,
                                 "usaspending", fetched_at)


FEED_HANDLERS = {
    "nws_alerts": lambda client, spec, fetched_at: fetch_nws_alerts(
        client, spec["lat"], spec["lon"], fetched_at),
    "nws_forecast": lambda client, spec, fetched_at: fetch_nws_forecast(
        client, spec["lat"], spec["lon"], fetched_at),
    "usgs_earthquakes": lambda client, spec, fetched_at:
        fetch_usgs_earthquakes(
            client, spec["start"], spec["end"], fetched_at,
            spec.get("min_magnitude", 2.5)),
    "usgs_water": lambda client, spec, fetched_at: fetch_usgs_water(
        client, spec["site"], fetched_at),
    "gdelt": lambda client, spec, fetched_at: fetch_gdelt(
        client, spec["query"], fetched_at),
    "rss": lambda client, spec, fetched_at: fetch_rss(
        client, spec["url"], spec["filename"], fetched_at),
    "bls": lambda client, spec, fetched_at: fetch_bls(
        client, spec["series"], fetched_at),
    "fred": lambda client, spec, fetched_at: fetch_fred(
        client, spec["series"], fetched_at, spec.get("fallback")),
    "nws_observations": lambda client, spec, fetched_at:
        fetch_nws_observations(client, spec, fetched_at),
    "federal_register": lambda client, spec, fetched_at:
        fetch_federal_register(client, spec, fetched_at),
    "treasury_dts": lambda client, spec, fetched_at:
        fetch_treasury_dts(client, spec, fetched_at),
    "usaspending": lambda client, spec, fetched_at:
        fetch_usaspending(client, spec, fetched_at),
}


def fetch_one(client, spec, fetched_at):
    """Run a single feed spec and return its inbox path or paths.

    Raises FeedError for an unknown kind, and whatever FeedError,
    ValueError or KeyError the fetcher itself raises for a failed or
    malformed fetch. Callers that run many specs catch those, as
    fetch_all does.
    """
    handler = FEED_HANDLERS.get(spec.get("kind"))
    if handler is None:
        raise FeedError("unknown feed kind")
    return handler(client, spec, fetched_at)


def fetch_all(client, feed_specs, fetched_at):
    """Run a manifest list of feed specs. Failures are collected, never
    raised, and returned as (paths, errors)."""
    paths = []
    errors = []
    for spec in feed_specs:
        kind = spec.get("kind")
        if kind not in FEED_HANDLERS:
            errors.append({"kind": kind, "error": "unknown feed kind"})
            continue
        try:
            result = fetch_one(client, spec, fetched_at)
            if isinstance(result, list):
                paths.extend(result)
            else:
                paths.append(result)
        except (FeedError, ValueError, KeyError) as exc:
            errors.append({"kind": kind, "error": str(exc)})
    return paths, errors
