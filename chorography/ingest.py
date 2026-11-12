"""Ingest: turning dropped files into dated exogenous events.

The ingest stage reads files from an inbox folder and from recorded
fixtures. Every file becomes zero or more exogenous events plus one
provenance record. Provenance carries the source, the fetch time when
the file envelope states one, and the SHA-256 of the raw payload bytes,
so any event in a run can be traced back to the exact bytes that caused
it.

A feed outage never blocks the world. A missing inbox, an HTTP error
body saved by a fetcher, truncated JSON and a stale feed each produce a
provenance record marked with the failure, no events, and no exception.
"""

import hashlib
import json
import os
import xml.etree.ElementTree as ET
from datetime import date

SEVERITY_MAGNITUDE = {
    "extreme": 0.9,
    "severe": 0.7,
    "moderate": 0.45,
    "minor": 0.2,
    "unknown": 0.3,
}


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _record(path, raw, status, source=None, fetched_at=None, note=""):
    return {
        "file": os.path.basename(path),
        "status": status,
        "source": source,
        "fetched_at": fetched_at,
        "raw_sha256": _sha256(raw),
        "bytes": len(raw),
        "note": note,
    }


def parse_inbox(inbox_dir, default_date):
    """Parse every file in an inbox. Returns (events, records).

    default_date is an ISO date string stamped on events whose payload
    carries no date of its own. Events are returned sorted by file name
    so the order is deterministic.
    """
    events = []
    records = []
    if not inbox_dir or not os.path.isdir(inbox_dir):
        return events, records
    for name in sorted(os.listdir(inbox_dir)):
        path = os.path.join(inbox_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            raw = fh.read()
        produced, record = _parse_file(path, raw, default_date)
        events.extend(produced)
        records.append(record)
    return events, records


def _parse_file(path, raw, default_date):
    name = os.path.basename(path)
    lower = name.lower()
    if lower.endswith(".txt"):
        text = raw.decode("utf-8", errors="replace")
        if "429" in text:
            return [], _record(path, raw, "error", source="http",
                               note="http 429 body, treated as feed outage")
        return [], _record(path, raw, "ignored", note="unrecognised text file")
    if lower.endswith((".rss", ".xml")):
        return _parse_feed(path, raw, default_date)
    if lower.endswith(".json"):
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return [], _record(path, raw, "error",
                               note="invalid or truncated JSON")
        return _parse_json(path, raw, data, default_date)
    return [], _record(path, raw, "ignored", note="unrecognised file type")


def _parse_json(path, raw, data, default_date):
    if isinstance(data, dict) and "chorography_event" in data:
        event = dict(data["chorography_event"])
        event.setdefault("date", default_date)
        event["provenance"] = {
            "source": data.get("source", "fixture"),
            "fetched_at": data.get("fetched_at"),
            "raw_sha256": _sha256(raw),
        }
        return [event], _record(path, raw, "ok",
                                source=event["provenance"]["source"],
                                fetched_at=event["provenance"]["fetched_at"])
    if isinstance(data, dict) and data.get("type") == "FeatureCollection":
        features = data.get("features", [])
        first = features[0] if features else {}
        props = first.get("properties", {})
        if "mag" in props:
            event = {
                "kind": "earthquake",
                "magnitude": min(1.0, float(props["mag"]) / 8.0),
                "magnitude_raw": props["mag"],
                "date": default_date,
                "duration_days": 3,
                "provenance": {
                    "source": "usgs-earthquake",
                    "fetched_at": data.get("fetched_at"),
                    "raw_sha256": _sha256(raw),
                },
            }
            return [event], _record(path, raw, "ok",
                                    source="usgs-earthquake")
        if "severity" in props or "event" in props:
            severity = str(props.get("severity", "unknown")).lower()
            event = {
                "kind": "weather_alert",
                "magnitude": SEVERITY_MAGNITUDE.get(severity, 0.3),
                "severity": severity,
                "headline": props.get("headline", props.get("event", "")),
                "date": default_date,
                "duration_days": 2,
                "provenance": {
                    "source": "nws-alerts",
                    "fetched_at": data.get("fetched_at"),
                    "raw_sha256": _sha256(raw),
                },
            }
            return [event], _record(path, raw, "ok", source="nws-alerts")
    return [], _record(path, raw, "ignored",
                       note="JSON without a recognised event shape")


def _parse_feed(path, raw, default_date):
    try:
        root = ET.fromstring(raw.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return [], _record(path, raw, "error", source="rss",
                           note="unparseable feed XML")
    items = root.findall(".//item")
    if not items:
        items = root.findall(
            ".//{http://www.w3.org/2005/Atom}entry")
    dates = []
    for item in items:
        for tag in ("pubDate", "published", "updated"):
            node = item.find(tag)
            if node is None:
                node = item.find(
                    "{http://www.w3.org/2005/Atom}" + tag)
            if node is not None and node.text:
                dates.append(node.text)
                break
    stale = False
    if dates:
        stale = _feed_is_stale(dates[0], default_date)
    event = {
        "kind": "news_cycle",
        "magnitude": min(1.0, len(items) / 20.0),
        "items": len(items),
        "sentiment": 0.0,
        "stale": stale,
        "date": default_date,
        "provenance": {
            "source": "rss",
            "fetched_at": None,
            "raw_sha256": _sha256(raw),
        },
    }
    note = "stale feed, newest item well before the world date" if stale else ""
    return [event], _record(path, raw, "stale" if stale else "ok",
                            source="rss", note=note)


def _feed_is_stale(date_text, default_date):
    """Best-effort staleness check for RFC 822 and ISO item dates."""
    from email.utils import parsedate_to_datetime
    try:
        parsed = parsedate_to_datetime(date_text)
        item_date = parsed.date()
    except (TypeError, ValueError):
        try:
            item_date = date.fromisoformat(date_text[:10])
        except ValueError:
            return False
    world_date = date.fromisoformat(default_date)
    return (world_date - item_date).days > 14
