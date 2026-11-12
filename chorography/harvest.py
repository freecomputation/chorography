"""The harvest scheduler: the world's sources, kept current politely.

Version 0.1.0 filled the inbox by hand, when a person ran the run
command with its live flag. The harvester removes the person. It walks
the region's source registry, fetches only the sources that are due,
archives every payload before anything parses it, writes the same
inbox envelopes the fetchers have always written, and records the
health of every source in a state file beside the world store. It is
built to be woken by an external clock, a cron entry or a timer, run
one pass with the once flag, and exit. A loop mode exists for
development and is not the intended way to run it.

The scheduler's clock and sleeper are injectable, as they are in the
fetch client, so the whole pass can be tested against a fake clock
with no real waiting and no network. Politeness is treated as a
correctness property: requests to one host are paced, the per run
request and byte budgets stop a pass that has done enough, failures
push a source's next due time out with a bounded backoff, and a lock
file keeps two passes from ever running over each other.

A source that fails is not an error in the world. Its failure is
written into the state file, surfaced in the harvest health section of
the report, and the world advances on what arrived, exactly as it has
since the first version.
"""

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from . import feeds, registry as registry_module
from .archive import Archive, sha256_hex

STATE_VERSION = 1
LOCK_STALE_SECONDS = 6 * 60 * 60
ERROR_WINDOW_SECONDS = 24 * 60 * 60
MAX_BACKOFF_FACTOR = 8

HOST_MIN_INTERVAL = {
    "api.gdeltproject.org": feeds.GDELT_MIN_INTERVAL,
}

KIND_HOSTS = {
    "nws_alerts": "api.weather.gov",
    "nws_forecast": "api.weather.gov",
    "usgs_earthquakes": "earthquake.usgs.gov",
    "usgs_water": "waterservices.usgs.gov",
    "gdelt": "api.gdeltproject.org",
    "bls": "api.bls.gov",
    "fred": "fred.stlouisfed.org",
}

DEFAULT_MAX_REQUESTS = 50
DEFAULT_MAX_BYTES = 20 * 1000 * 1000


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def _parse_iso(text):
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def spec_host(spec):
    if spec["kind"] == "rss":
        return urlparse(spec.get("url", "")).hostname or ""
    return KIND_HOSTS.get(spec["kind"], "")


def newest_item_time(raw):
    """Best effort newest item timestamp in a raw feed payload.

    Returns an aware datetime or None when the payload carries no
    recognisable item dates. The shapes recognised are the ones the
    harvester's sources actually publish: GDELT article seen dates,
    GeoJSON feature times, USGS water observation times, and RSS or
    Atom item dates. A payload with no dates is not evidence of
    staleness, so None is an honest answer, not a failure.
    """
    candidates = []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        data = None
    if isinstance(data, dict):
        articles = data.get("articles")
        if isinstance(articles, list):
            for article in articles:
                seen = str(article.get("seendate", ""))
                for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d%H%M%S"):
                    try:
                        candidates.append(datetime.strptime(
                            seen, fmt).replace(tzinfo=timezone.utc))
                        break
                    except ValueError:
                        continue
        features = data.get("features")
        if isinstance(features, list):
            for feature in features:
                props = feature.get("properties", {})
                for key in ("updated", "time", "issued"):
                    parsed = _parse_iso(props.get(key))
                    if parsed is not None:
                        candidates.append(parsed)
        value = data.get("value")
        if isinstance(value, dict):
            for series in value.get("timeSeries", []):
                for block in series.get("values", []):
                    for point in block.get("value", []):
                        parsed = _parse_iso(point.get("dateTime"))
                        if parsed is not None:
                            candidates.append(parsed)
    if not candidates:
        try:
            root = ET.fromstring(raw.decode("utf-8", errors="replace"))
        except ET.ParseError:
            root = None
        if root is not None:
            nodes = root.findall(".//item")
            if not nodes:
                nodes = root.findall(
                    ".//{http://www.w3.org/2005/Atom}entry")
            for node in nodes:
                for tag in ("pubDate", "published", "updated"):
                    child = node.find(tag)
                    if child is None:
                        child = node.find(
                            "{http://www.w3.org/2005/Atom}" + tag)
                    if child is not None and child.text:
                        try:
                            parsed = parsedate_to_datetime(child.text)
                        except (TypeError, ValueError):
                            parsed = _parse_iso(child.text)
                        if parsed is not None:
                            if parsed.tzinfo is None:
                                parsed = parsed.replace(
                                    tzinfo=timezone.utc)
                            candidates.append(parsed)
                        break
    if not candidates:
        return None
    return max(candidates)


class HarvestState:
    """The state file: per source health, persisted as JSON."""

    def __init__(self, path):
        self.path = path
        self.data = {"version": STATE_VERSION, "updated_at": None,
                     "sources": {}}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict) and "sources" in loaded:
                    self.data = loaded
            except ValueError:
                pass

    def source(self, source_id):
        return self.data["sources"].setdefault(source_id, {
            "kind": None,
            "last_attempt": None,
            "last_success": None,
            "last_item_time": None,
            "next_due": None,
            "errors": [],
            "consecutive_errors": 0,
            "last_error": None,
            "stale": False,
        })

    def save(self, now):
        self.data["updated_at"] = _iso(now)
        keep_after = now - timedelta(seconds=7 * ERROR_WINDOW_SECONDS)
        for entry in self.data["sources"].values():
            entry["errors"] = [
                stamp for stamp in entry.get("errors", [])
                if (_parse_iso(stamp) or now) >= keep_after]
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)


class HarvestLocked(Exception):
    pass


class Harvester:
    def __init__(self, region_dir, inbox_dir, state_path, archive_root,
                 opener=None, sleeper=None, now_fn=None,
                 max_requests=DEFAULT_MAX_REQUESTS,
                 max_bytes=DEFAULT_MAX_BYTES):
        self.region_dir = region_dir
        self.inbox_dir = inbox_dir
        self.sleeper = sleeper or time.sleep
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.state = HarvestState(state_path)
        self.archive = Archive(archive_root)
        self.client = feeds.FeedClient(inbox_dir, opener=opener,
                                       sleeper=self.sleeper)
        self.client.on_fetch = self._on_fetch
        self._capture = []
        self._current = None
        self._host_last = {}
        self.entries = registry_module.load_region_registry(region_dir)

    def _on_fetch(self, url, status, raw):
        if raw and status == 200 and self._current is not None:
            entry, fetched_at = self._current
            digest = self.archive.store(entry["id"], fetched_at, raw)
        else:
            digest = sha256_hex(raw) if raw else None
        self._capture.append({
            "url": url, "status": status, "bytes": len(raw),
            "sha256": digest,
        })

    def _lock_path(self):
        return self.state.path + ".lock"

    def _acquire_lock(self, now):
        path = self._lock_path()
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            created = None
            try:
                with open(path, encoding="utf-8") as fh:
                    created = _parse_iso(fh.read().strip().split()[-1])
            except (OSError, IndexError):
                created = None
            if created is not None and (
                    now - created).total_seconds() > LOCK_STALE_SECONDS:
                os.remove(path)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                raise HarvestLocked(path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("{} {}\n".format(os.getpid(), _iso(now)))
        return path

    def _release_lock(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _inbox_snapshot(self):
        snapshot = {}
        if os.path.isdir(self.inbox_dir):
            for name in os.listdir(self.inbox_dir):
                if name.startswith("."):
                    continue
                full = os.path.join(self.inbox_dir, name)
                if os.path.isfile(full):
                    stat = os.stat(full)
                    snapshot[name] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    def _produced(self, before):
        after = self._inbox_snapshot()
        return sorted(name for name, stamp in after.items()
                      if before.get(name) != stamp)

    def is_due(self, entry, now):
        record = self.state.data["sources"].get(entry["id"])
        if record is None:
            return True
        due = _parse_iso(record.get("next_due"))
        return due is None or now >= due

    def run_once(self):
        """Run one harvest pass and return its summary dictionary."""
        now = self.now_fn()
        try:
            lock = self._acquire_lock(now)
        except HarvestLocked:
            return {"locked": True, "attempted": [], "succeeded": [],
                    "failed": [], "deferred": [], "skipped": [],
                    "disabled": []}
        summary = {"locked": False, "attempted": [], "succeeded": [],
                   "failed": [], "deferred": [], "skipped": [],
                   "disabled": []}
        try:
            requests_used = 0
            bytes_used = 0
            for entry in self.entries:
                if not entry["enabled"]:
                    summary["disabled"].append(entry["id"])
                    continue
                if not self.is_due(entry, self.now_fn()):
                    summary["skipped"].append(entry["id"])
                    continue
                if (requests_used >= self.max_requests
                        or bytes_used >= self.max_bytes):
                    summary["deferred"].append(entry["id"])
                    continue
                outcome = self._run_entry(entry)
                summary["attempted"].append(entry["id"])
                requests_used += outcome["requests"]
                bytes_used += outcome["bytes"]
                if outcome["ok"]:
                    summary["succeeded"].append(entry["id"])
                else:
                    summary["failed"].append(
                        {"id": entry["id"], "error": outcome["error"]})
            self.state.save(self.now_fn())
        finally:
            self._release_lock(lock)
        return summary

    def _pace(self, entry):
        host = spec_host(entry)
        if not host:
            return
        minimum = HOST_MIN_INTERVAL.get(host, 0.0)
        last = self._host_last.get(host)
        if minimum > 0 and last is not None:
            elapsed = (self.now_fn() - last).total_seconds()
            if elapsed < minimum:
                self.sleeper(minimum - elapsed)
        self._host_last[host] = self.now_fn()

    def _run_entry(self, entry):
        self._pace(entry)
        now = self.now_fn()
        fetched_at = _iso(now)
        record = self.state.source(entry["id"])
        record["kind"] = entry["kind"]
        record["last_attempt"] = fetched_at
        before = self._inbox_snapshot()
        self._capture = []
        self._current = (entry, fetched_at)
        error = None
        try:
            feeds.fetch_one(self.client, entry, fetched_at)
        except (feeds.FeedError, ValueError, KeyError) as exc:
            error = str(exc)
        finally:
            self._current = None
        produced = self._produced(before)
        requests = len(self._capture)
        byte_count = sum(cap["bytes"] for cap in self._capture)
        if self._capture:
            for cap in self._capture:
                self.archive.append_index({
                    "source": entry["id"],
                    "kind": entry["kind"],
                    "url": cap["url"],
                    "fetched_at": fetched_at,
                    "status": cap["status"],
                    "bytes": cap["bytes"],
                    "sha256": cap["sha256"],
                    "inbox_files": produced,
                })
        else:
            self.archive.append_index({
                "source": entry["id"],
                "kind": entry["kind"],
                "url": None,
                "fetched_at": fetched_at,
                "status": "error" if error else "no_fetch",
                "bytes": 0,
                "sha256": None,
                "inbox_files": produced,
                "note": error or "no HTTP response was captured",
            })
        interval = entry["interval_seconds"]
        if error is None:
            record["last_success"] = fetched_at
            record["consecutive_errors"] = 0
            record["last_error"] = None
            record["next_due"] = _iso(now + timedelta(seconds=interval))
            item_time = None
            for name in produced:
                full = os.path.join(self.inbox_dir, name)
                with open(full, "rb") as fh:
                    found = newest_item_time(fh.read())
                if found is not None and (
                        item_time is None or found > item_time):
                    item_time = found
            if item_time is not None:
                record["last_item_time"] = _iso(item_time)
                age = (now - item_time).total_seconds()
                record["stale"] = age > 2 * interval
            else:
                record["stale"] = False
        else:
            record["consecutive_errors"] = int(
                record.get("consecutive_errors", 0)) + 1
            record["errors"] = list(record.get("errors", [])) + [fetched_at]
            record["last_error"] = error
            factor = min(2 ** record["consecutive_errors"],
                         MAX_BACKOFF_FACTOR)
            record["next_due"] = _iso(
                now + timedelta(seconds=interval * factor))
        self.state.save(now)
        return {"ok": error is None, "error": error,
                "requests": requests, "bytes": byte_count}

    def run_loop(self, max_iterations=None, poll_seconds=15):
        """Run passes in the foreground. For development use.

        Between passes the loop sleeps until the earliest next due
        time among enabled sources, capped by the poll interval, so a
        development loop behaves like the timer it stands in for.
        """
        summaries = []
        iteration = 0
        while max_iterations is None or iteration < max_iterations:
            summaries.append(self.run_once())
            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break
            wait = poll_seconds
            dues = []
            for entry in self.entries:
                if not entry["enabled"]:
                    continue
                record = self.state.data["sources"].get(entry["id"], {})
                due = _parse_iso(record.get("next_due"))
                if due is not None:
                    dues.append(due)
            if dues:
                until = (min(dues) - self.now_fn()).total_seconds()
                wait = max(1.0, min(float(poll_seconds), until))
            self.sleeper(wait)
        return summaries


def summarize_health(state_data, reference=None):
    """Per source health rows from a loaded harvest state dictionary.

    Ages are measured against the state's own updated_at stamp unless
    a reference moment is given, so a report printed later does not
    pretend the ledger was checked at print time.
    """
    if reference is None:
        reference = _parse_iso(state_data.get("updated_at"))
    if reference is None:
        reference = datetime.now(timezone.utc)
    rows = []
    sources = state_data.get("sources", {})
    for source_id in sorted(sources):
        record = sources[source_id]
        item_time = _parse_iso(record.get("last_item_time"))
        age = None
        if item_time is not None:
            age = int((reference - item_time).total_seconds())
        cutoff = reference - timedelta(seconds=ERROR_WINDOW_SECONDS)
        errors_last_day = sum(
            1 for stamp in record.get("errors", [])
            if (_parse_iso(stamp) or reference) >= cutoff)
        rows.append({
            "id": source_id,
            "kind": record.get("kind"),
            "last_attempt": record.get("last_attempt"),
            "last_success": record.get("last_success"),
            "next_due": record.get("next_due"),
            "newest_item_age_seconds": age,
            "errors_last_day": errors_last_day,
            "stale": bool(record.get("stale")),
        })
    return rows
