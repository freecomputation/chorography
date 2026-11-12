import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography import feeds, regionfetch

ROOT = os.path.join(os.path.dirname(__file__), "..")
REGION = os.path.join(ROOT, "data", "sample-region")


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status = status
        self._headers = headers or {}

    def read(self):
        return self._body

    def getheader(self, name):
        return self._headers.get(name)


class TestFeeds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox")
        self.sleeps = []

    def tearDown(self):
        self.tmp.cleanup()

    def _client(self, opener):
        return feeds.FeedClient(self.inbox, opener=opener,
                                sleeper=self.sleeps.append)

    def test_nws_alert_fetch_writes_inbox_file(self):
        payload = json.dumps({
            "type": "FeatureCollection",
            "features": [{"properties": {"severity": "Moderate"}}],
        }).encode("utf-8")

        def opener(request):
            self.assert_user_agent(request)
            return FakeResponse(payload, headers={"ETag": '"abc"'})

        client = self._client(opener)
        path = feeds.fetch_nws_alerts(client, 32.0, -86.0,
                                      "2026-03-02T00:00:00Z")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["fetched_at"], "2026-03-02T00:00:00Z")

    def assert_user_agent(self, request):
        ua = request.get_header("User-agent")
        if ua is None:
            ua = request.headers.get("User-Agent")
        self.assertTrue(ua and "Chorography" in ua)

    def test_conditional_get_uses_cache_on_304(self):
        payload = b'{"type": "FeatureCollection", "features": []}'

        def opener(request):
            return FakeResponse(payload, headers={"ETag": '"abc"'})

        client = self._client(opener)
        status, raw = client.fetch_bytes("https://example.invalid/a")
        self.assertEqual(status, 200)

        def opener304(request):
            raise urllib.error.HTTPError(
                request.full_url, 304, "Not Modified", {}, None)

        client2 = self._client(opener304)
        status, raw = client2.fetch_bytes("https://example.invalid/a")
        self.assertEqual(status, 304)

    def test_gdelt_pacing(self):
        def opener(request):
            return FakeResponse(b'{"articles": []}')

        client = self._client(opener)
        feeds.fetch_gdelt(client, "demonstration region",
                          "2026-03-02T00:00:00Z")
        self.assertIn(5.0, self.sleeps)

    def test_fred_fallback_on_outage(self):
        fallback = os.path.join(self.tmp.name, "fred.csv")
        with open(fallback, "w", encoding="utf-8") as fh:
            fh.write("DATE,UNRATE\n2026-02-01,4.1\n2026-03-01,4.2\n")

        def opener(request):
            raise urllib.error.URLError("down")

        client = self._client(opener)
        path = feeds.fetch_fred(client, "UNRATE", "2026-03-02T00:00:00Z",
                                fallback_path=fallback)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        obs = data["chorography_event"]["observations"]
        self.assertEqual(len(obs), 2)
        self.assertEqual(obs[-1]["value"], "4.2")

    def test_fetch_all_tolerates_failures(self):
        def opener(request):
            raise urllib.error.URLError("down")

        client = self._client(opener)
        paths, errors = feeds.fetch_all(client, [
            {"kind": "nws_alerts", "lat": 32.0, "lon": -86.0},
            {"kind": "mystery"},
        ], "2026-03-02T00:00:00Z")
        self.assertEqual(paths, [])
        self.assertEqual(len(errors), 2)


class TestRegionFetch(unittest.TestCase):
    def test_build_region_from_fake_acs(self):
        header = regionfetch.VARIABLES + ["state", "county", "tract"]
        row = {code: "0" for code in header}
        row.update({
            "NAME": "Census Tract 1, Demonstration County, XX",
            "B01003_001E": "2400", "B11001_001E": "1000",
            "B19013_001E": "52000", "B25003_002E": "600",
            "B25003_003E": "400",
            "B11016_010E": "300", "B11016_003E": "200",
            "B11016_011E": "100",
            "state": "99", "county": "999", "tract": "000100",
        })
        for i in range(1, 18):
            row["B19001_{:03d}E".format(i)] = "10"
        payload = json.dumps([header, [row[c] for c in header]]
                             ).encode("utf-8")

        def opener(request):
            return FakeResponse(payload)

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "region")
            manifest = regionfetch.build_region(
                2024, "99", "999", "Fake County", out, "2026-03-02",
                opener=opener)
            self.assertFalse(manifest["demonstration"])
            from chorography.world import build_world
            world = build_world(out)
            self.assertEqual(len(world.tract_ids()), 1)
            self.assertGreater(len(world.households), 0)


class TestCLI(unittest.TestCase):
    def run_cli(self, cwd, *argv):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        return subprocess.run(
            [sys.executable, "-m", "chorography"] + list(argv),
            cwd=cwd, env=env, capture_output=True, text=True,
            timeout=300)

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "world.db")
            result = self.run_cli(tmp, "init", "--region", REGION,
                                  "--db", db)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("State hash", result.stdout)
            result = self.run_cli(tmp, "run", "--db", db, "--days", "5",
                                  "--inbox",
                                  os.path.join(REGION, "inbox"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("day 5", result.stdout)
            result = self.run_cli(tmp, "verify", "--db", db)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PASS", result.stdout)
            result = self.run_cli(tmp, "report", "--db", db)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PMESII dashboard", result.stdout)
            script = os.path.join(tmp, "coa.json")
            with open(script, "w", encoding="utf-8") as fh:
                json.dump({
                    "name": "smoke", "days": 4, "seeds": [20260302],
                    "interventions": [
                        {"day_offset": 0, "kind": "outage",
                         "node_kind": "power", "magnitude": 0.4}],
                }, fh)
            result = self.run_cli(tmp, "excursion", "--db", db,
                                  "--script", script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("contrast", result.stdout)
            result = self.run_cli(tmp, "calibrate", "--db", db,
                                  "--days", "4")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("scorecard", result.stdout)


if __name__ == "__main__":
    unittest.main()
