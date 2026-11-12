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
