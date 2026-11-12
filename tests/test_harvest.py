import hashlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography import harvest
from chorography.archive import Archive
from chorography.report import build_report, render_text
from chorography.world import build_world

ROOT = os.path.join(os.path.dirname(__file__), "..")
REGION = os.path.join(ROOT, "data", "sample-region")
START = datetime(2026, 3, 2, 0, 0, 0, tzinfo=timezone.utc)

RSS_FRESH = (
    '<?xml version="1.0"?><rss version="2.0"><channel>'
    "<title>County Wire</title>"
    "<item><title>Road crew schedules bridge work</title>"
    "<link>https://news.example/bridge</link>"
    "<pubDate>Mon, 02 Mar 2026 00:00:00 GMT</pubDate></item>"
    "</channel></rss>").encode("utf-8")

RSS_STALE = (
    '<?xml version="1.0"?><rss version="2.0"><channel>'
    "<title>Forgotten Feed</title>"
    "<item><title>Old news</title>"
    "<link>https://news.example/old</link>"
    "<pubDate>Wed, 01 Jan 2020 00:00:00 GMT</pubDate></item>"
    "</channel></rss>").encode("utf-8")


class FakeClock:
    def __init__(self, start):
        self.moment = start
        self.sleeps = []

    def now(self):
        return self.moment

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.moment += timedelta(seconds=seconds)


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status = status
        self._headers = headers or {}

    def read(self):
        return self._body

    def getheader(self, name):
        return self._headers.get(name)


def write_region(directory, feeds_list):
    region = os.path.join(directory, "region")
    os.makedirs(region, exist_ok=True)
    with open(os.path.join(region, "manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"name": "Harvest Test Region", "feeds": feeds_list}, fh)
    return region


class HarvestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(START)
        self.inbox = os.path.join(self.tmp.name, "inbox")
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.archive_root = os.path.join(self.tmp.name, "archive")

    def tearDown(self):
        self.tmp.cleanup()

    def make_harvester(self, region, opener, **kwargs):
        return harvest.Harvester(
            region, self.inbox, self.state_path, self.archive_root,
            opener=opener, sleeper=self.clock.sleep,
            now_fn=self.clock.now, **kwargs)

    def read_state(self):
        with open(self.state_path, encoding="utf-8") as fh:
            return json.load(fh)


class TestScheduler(HarvestCase):
    def rss_region(self, **spec_kwargs):
        spec = {"kind": "rss", "url": "https://news.example/feed.rss",
                "filename": "feed.rss"}
        spec.update(spec_kwargs)
        return write_region(self.tmp.name, [spec])

    def test_due_then_not_due_then_due_again(self):
        region = self.rss_region()
        calls = []

        def opener(request):
            calls.append(request.full_url)
            return FakeResponse(RSS_FRESH)

        hv = self.make_harvester(region, opener)
        summary = hv.run_once()
        self.assertEqual(summary["succeeded"], ["rss"])
        state = self.read_state()
        record = state["sources"]["rss"]
        self.assertEqual(record["last_success"], "2026-03-02T00:00:00+00:00")
        self.assertEqual(record["next_due"], "2026-03-02T01:00:00+00:00")
        summary = hv.run_once()
        self.assertEqual(summary["skipped"], ["rss"])
        self.assertEqual(len(calls), 1)
        self.clock.moment += timedelta(seconds=3601)
        summary = hv.run_once()
        self.assertEqual(summary["succeeded"], ["rss"])
        self.assertEqual(len(calls), 2)

    def test_lock_blocks_pass_and_stale_lock_is_broken(self):
        region = self.rss_region()
        calls = []

        def opener(request):
            calls.append(request.full_url)
            return FakeResponse(RSS_FRESH)

        hv = self.make_harvester(region, opener)
        lock_path = self.state_path + ".lock"
        with open(lock_path, "w", encoding="utf-8") as fh:
            fh.write("999 {}\n".format(harvest._iso(START)))
        summary = hv.run_once()
        self.assertTrue(summary["locked"])
        self.assertEqual(calls, [])
        with open(lock_path, "w", encoding="utf-8") as fh:
            old = START - timedelta(seconds=harvest.LOCK_STALE_SECONDS + 60)
            fh.write("999 {}\n".format(harvest._iso(old)))
        summary = hv.run_once()
        self.assertFalse(summary["locked"])
        self.assertEqual(summary["succeeded"], ["rss"])
        self.assertFalse(os.path.exists(lock_path))

    def test_request_budget_defers_remaining_sources(self):
        specs = [
            {"kind": "rss", "url": "https://a.example/f", "filename": "a.rss"},
            {"kind": "rss", "url": "https://b.example/f", "filename": "b.rss"},
            {"kind": "rss", "url": "https://c.example/f", "filename": "c.rss"},
        ]
        region = write_region(self.tmp.name, specs)

        def opener(request):
            return FakeResponse(RSS_FRESH)

        hv = self.make_harvester(region, opener, max_requests=1)
        summary = hv.run_once()
        self.assertEqual(len(summary["attempted"]), 1)
        self.assertEqual(len(summary["deferred"]), 2)

    def test_byte_budget_defers_remaining_sources(self):
        specs = [
            {"kind": "rss", "url": "https://a.example/f", "filename": "a.rss"},
            {"kind": "rss", "url": "https://b.example/f", "filename": "b.rss"},
        ]
        region = write_region(self.tmp.name, specs)

        def opener(request):
            return FakeResponse(RSS_FRESH)

        hv = self.make_harvester(region, opener,
                                 max_bytes=len(RSS_FRESH) - 1)
        summary = hv.run_once()
        self.assertEqual(len(summary["attempted"]), 1)
        self.assertEqual(len(summary["deferred"]), 1)

    def test_failure_records_error_and_backs_off(self):
        region = self.rss_region()

        def opener(request):
            raise urllib.error.URLError("down")

        hv = self.make_harvester(region, opener)
        summary = hv.run_once()
        self.assertEqual(len(summary["failed"]), 1)
        state = self.read_state()
        record = state["sources"]["rss"]
        self.assertEqual(record["consecutive_errors"], 1)
        self.assertIsNone(record["last_success"])
        self.assertEqual(record["next_due"], "2026-03-02T02:00:00+00:00")
        rows = harvest.summarize_health(state, reference=START)
        self.assertEqual(rows[0]["errors_last_day"], 1)
        self.assertFalse(rows[0]["stale"])

    def test_stale_feed_is_flagged(self):
        region = self.rss_region()

        def opener(request):
            return FakeResponse(RSS_STALE)

        hv = self.make_harvester(region, opener)
        summary = hv.run_once()
        self.assertEqual(summary["succeeded"], ["rss"])
        state = self.read_state()
        self.assertTrue(state["sources"]["rss"]["stale"])
        rows = harvest.summarize_health(state, reference=START)
        self.assertTrue(rows[0]["stale"])
        self.assertGreater(rows[0]["newest_item_age_seconds"], 1000000)

    def test_disabled_source_is_never_fetched(self):
        region = self.rss_region(enabled=False)
        calls = []

        def opener(request):
            calls.append(request.full_url)
            return FakeResponse(RSS_FRESH)

        hv = self.make_harvester(region, opener)
        summary = hv.run_once()
        self.assertEqual(summary["disabled"], ["rss"])
        self.assertEqual(calls, [])

    def test_run_loop_passes_and_polls(self):
        region = self.rss_region()

        def opener(request):
            return FakeResponse(RSS_FRESH)

        hv = self.make_harvester(region, opener)
        summaries = hv.run_loop(max_iterations=2, poll_seconds=15)
        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[0]["succeeded"], ["rss"])
        self.assertEqual(summaries[1]["skipped"], ["rss"])


class TestArchive(HarvestCase):
    def test_payload_stored_by_hash_and_indexed(self):
        region = write_region(self.tmp.name, [
            {"kind": "rss", "url": "https://news.example/feed.rss",
             "filename": "feed.rss"},
        ])

        def opener(request):
            return FakeResponse(RSS_FRESH)

        hv = self.make_harvester(region, opener)
        hv.run_once()
        digest = hashlib.sha256(RSS_FRESH).hexdigest()
        archive = Archive(self.archive_root)
        path = archive.payload_path("rss", "2026-03-02T00:00:00+00:00",
                                    digest)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), RSS_FRESH)
        records = archive.read_index()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source"], "rss")
        self.assertEqual(records[0]["status"], 200)
        self.assertEqual(records[0]["sha256"], digest)
        self.assertEqual(records[0]["inbox_files"], ["feed.rss"])

    def test_store_is_idempotent(self):
        archive = Archive(self.archive_root)
        first = archive.store("rss", "2026-03-02T00:00:00+00:00", b"abc")
        second = archive.store("rss", "2026-03-02T00:00:00+00:00", b"abc")
        self.assertEqual(first, second)


class TestAllEightKinds(HarvestCase):
    def test_all_eight_kinds_run_through_scheduler(self):
        specs = [
            {"kind": "nws_alerts", "lat": 32.0, "lon": -86.0},
            {"kind": "nws_forecast", "lat": 32.0, "lon": -86.0},
            {"kind": "usgs_earthquakes", "start": "2026-03-01",
             "end": "2026-03-02"},
            {"kind": "usgs_water", "site": "02420000"},
            {"kind": "gdelt", "query": "demonstration region"},
            {"kind": "rss", "url": "https://news.example/feed.rss",
             "filename": "feed.rss"},
            {"kind": "bls", "series": ["LNS14000000"]},
            {"kind": "fred", "series": "UNRATE"},
        ]
        region = write_region(self.tmp.name, specs)
        payloads = [
            ("alerts/active", json.dumps({
                "type": "FeatureCollection",
                "features": [{"properties": {"severity": "Moderate",
                                             "updated":
                                             "2026-03-02T00:00:00Z"}}],
            }).encode("utf-8")),
            ("points/", json.dumps({
                "properties": {
                    "forecast": "https://api.weather.gov/gridpoints/X/1/forecast"},
            }).encode("utf-8")),
            ("gridpoints", b'{"properties": {"periods": []}}'),
            ("earthquake.usgs.gov", json.dumps({
                "type": "FeatureCollection",
                "features": [{"properties": {"mag": 3.1,
                                             "time": 1772400000000}}],
            }).encode("utf-8")),
            ("waterservices.usgs.gov", json.dumps({
                "value": {"timeSeries": [{"values": [{"value": [
                    {"value": "12.3",
                     "dateTime": "2026-03-02T00:00:00.000-05:00"}]}]}]},
            }).encode("utf-8")),
            ("gdeltproject.org", json.dumps({
                "articles": [{"seendate": "20260302T000000",
                              "title": "News"}],
            }).encode("utf-8")),
            ("news.example", RSS_FRESH),
            ("api.bls.gov", b'{"Results": {"series": []}}'),
            ("fred.stlouisfed.org",
             b"DATE,UNRATE\n2026-03-01,4.2\n"),
        ]

        def opener(request):
            for marker, body in payloads:
                if marker in request.full_url:
                    return FakeResponse(body)
            raise AssertionError("unexpected URL " + request.full_url)

        hv = self.make_harvester(region, opener)
        summary = hv.run_once()
        self.assertEqual(summary["failed"], [])
        self.assertEqual(len(summary["succeeded"]), 8)
        records = Archive(self.archive_root).read_index()
        self.assertGreaterEqual(len(records), 8)


class TestReportHealthSection(HarvestCase):
    def test_render_text_includes_health_rows(self):
        world = build_world(REGION)
        health = {
            "as_of": "2026-03-02T01:00:00+00:00",
            "sources": [{
                "id": "rss", "kind": "rss",
                "last_attempt": "2026-03-02T00:00:00+00:00",
                "last_success": "2026-03-02T00:00:00+00:00",
                "next_due": "2026-03-02T01:00:00+00:00",
                "newest_item_age_seconds": 3600,
                "errors_last_day": 2, "stale": True,
            }],
        }
        text = render_text(build_report(world, harvest_health=health))
        self.assertIn("Harvest health", text)
        self.assertIn("STALE", text)


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/feed.rss":
            body = RSS_FRESH
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestHarvestCLIWithFixtureServer(unittest.TestCase):
    def run_cli(self, cwd, *argv):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        return subprocess.run(
            [sys.executable, "-m", "chorography"] + list(argv),
            cwd=cwd, env=env, capture_output=True, text=True, timeout=300)

    def test_harvest_once_against_local_server(self):
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with tempfile.TemporaryDirectory() as tmp:
                region = write_region(tmp, [
                    {"kind": "rss",
                     "url": "http://127.0.0.1:{}/feed.rss".format(port),
                     "filename": "local.rss"},
                ])
                inbox = os.path.join(tmp, "inbox")
                state = os.path.join(tmp, "state.json")
                archive = os.path.join(tmp, "archive")
                result = self.run_cli(
                    tmp, "harvest", "--region", region, "--once",
                    "--inbox", inbox, "--state", state,
                    "--archive", archive)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("1 attempted, 1 succeeded", result.stdout)
                self.assertTrue(os.path.exists(
                    os.path.join(inbox, "local.rss")))
                self.assertTrue(os.path.exists(state))
                self.assertTrue(os.path.exists(
                    os.path.join(archive, "index.jsonl")))
                db = os.path.join(tmp, "world.db")
                result = self.run_cli(tmp, "init", "--region", REGION,
                                      "--db", db)
                self.assertEqual(result.returncode, 0, result.stderr)
                with open(state, encoding="utf-8") as fh:
                    ledger = fh.read()
                with open(db + ".harvest.json", "w",
                          encoding="utf-8") as fh:
                    fh.write(ledger)
                result = self.run_cli(tmp, "report", "--db", db)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Harvest health", result.stdout)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
