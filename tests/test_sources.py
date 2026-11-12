import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography import feeds, registry
from chorography.ingest import parse_inbox

ROOT = os.path.join(os.path.dirname(__file__), "..")
REGION = os.path.join(ROOT, "data", "sample-region")
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
FETCHED_AT = "2026-03-02T07:00:00+00:00"


def fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status = status
        self._headers = headers or {}

    def read(self):
        return self._body

    def getheader(self, name):
        return self._headers.get(name)


class SourceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox")
        self.sleeps = []
        self.requests = []

    def tearDown(self):
        self.tmp.cleanup()

    def client_for(self, routes):
        def opener(request):
            self.requests.append(request)
            for marker, body in routes:
                if marker in request.full_url:
                    return FakeResponse(body)
            raise AssertionError("unexpected URL " + request.full_url)

        return feeds.FeedClient(self.inbox, opener=opener,
                                sleeper=self.sleeps.append)

    def read_envelope(self, filename):
        with open(os.path.join(self.inbox, filename),
                  encoding="utf-8") as fh:
            return json.load(fh)


class TestNwsObservations(SourceCase):
    def test_station_observation_becomes_event_envelope(self):
        client = self.client_for([
            ("observations/latest", fixture_bytes("nws_observation.json")),
        ])
        path = feeds.fetch_one(client, {"kind": "nws_observations",
                                        "station": "KDME"}, FETCHED_AT)
        envelope = self.read_envelope(os.path.basename(path))
        self.assertEqual(envelope["source"], "nws-observation")
        event = envelope["chorography_event"]
        self.assertEqual(event["kind"], "weather_observation")
        self.assertEqual(event["station"], "KDME")
        self.assertEqual(event["observed_at"],
                         "2026-03-02T06:53:00+00:00")
        self.assertEqual(event["temperature_c"], 12.2)
        self.assertEqual(event["humidity_pct"], 58.3)
        self.assertIsNone(event.get("wind_gust_kmh"))

    def test_station_resolved_from_lat_lon(self):
        points = json.dumps({"properties": {
            "observationStations":
                "https://api.weather.gov/gridpoints/DME/32,86/stations"},
        }).encode("utf-8")
        listing = json.dumps({"features": [{"properties": {
            "stationIdentifier": "KDME"}}]}).encode("utf-8")
        client = self.client_for([
            ("observations/latest", fixture_bytes("nws_observation.json")),
            ("api.weather.gov/points/", points),
            ("gridpoints", listing),
        ])
        path = feeds.fetch_one(client, {"kind": "nws_observations",
                                        "lat": 32.0, "lon": -86.0},
                               FETCHED_AT)
        event = self.read_envelope(
            os.path.basename(path))["chorography_event"]
        self.assertEqual(event["station"], "KDME")
        self.assertEqual(len(self.requests), 3)

    def test_observation_event_passes_through_ingest(self):
        client = self.client_for([
            ("observations/latest", fixture_bytes("nws_observation.json")),
        ])
        feeds.fetch_one(client, {"kind": "nws_observations",
                                 "station": "KDME"}, FETCHED_AT)
        events, records = parse_inbox(self.inbox, "2026-03-02")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "weather_observation")
        self.assertEqual(events[0]["provenance"]["source"],
                         "nws-observation")


class TestFederalRegister(SourceCase):
    def test_documents_become_context_event(self):
        client = self.client_for([
            ("federalregister.gov", fixture_bytes("federal_register.json")),
        ])
        path = feeds.fetch_one(client, {
            "kind": "federal_register",
            "agencies": ["environmental-protection-agency"],
            "term": "Demonstration",
        }, FETCHED_AT)
        envelope = self.read_envelope(os.path.basename(path))
        event = envelope["chorography_event"]
        self.assertEqual(event["series"], "federal-register")
        self.assertEqual(event["magnitude"], 0.1)
        self.assertEqual(len(event["documents"]), 2)
        self.assertEqual(event["documents"][0]["document_number"],
                         "2026-04567")
        self.assertIn("conditions%5Bagencies%5D%5B%5D",
                      self.requests[0].full_url)


class TestTreasuryDts(SourceCase):
    def test_latest_record_becomes_context_event(self):
        client = self.client_for([
            ("fiscaldata.treasury.gov", fixture_bytes("treasury_dts.json")),
        ])
        path = feeds.fetch_one(client, {"kind": "treasury_dts"},
                               FETCHED_AT)
        event = self.read_envelope(
            os.path.basename(path))["chorography_event"]
        self.assertEqual(event["series"], "treasury-dts")
        self.assertEqual(event["record_date"], "2026-02-27")
        self.assertEqual(event["figures"]["tot_cash_bal"], 812345.0)
        self.assertEqual(event["magnitude"], 0.1)


class TestUsaspending(SourceCase):
    def test_awards_become_context_event_and_post_by_fips(self):
        client = self.client_for([
            ("api.usaspending.gov", fixture_bytes("usaspending.json")),
        ])
        path = feeds.fetch_one(client, {
            "kind": "usaspending", "state": "XX", "county_fips": "99999",
            "start": "2026-01-01", "end": "2026-03-02",
        }, FETCHED_AT)
        event = self.read_envelope(
            os.path.basename(path))["chorography_event"]
        self.assertEqual(event["award_count"], 2)
        self.assertEqual(event["total_obligations"], 200590.5)
        request = self.requests[0]
        self.assertEqual(request.get_method(), "POST")
        body = json.loads(request.data.decode("utf-8"))
        places = body["filters"]["place_of_performance_locations"]
        self.assertEqual(places[0]["county"], "99999")
        self.assertEqual(places[0]["state"], "XX")


class TestSourceFailures(SourceCase):
    def test_truncated_json_raises_value_error(self):
        truncated = fixture_bytes("federal_register.json")[:40]
        client = self.client_for([
            ("federalregister.gov", truncated),
        ])
        with self.assertRaises(ValueError):
            feeds.fetch_one(client, {
                "kind": "federal_register", "term": "Demonstration",
            }, FETCHED_AT)

    def test_http_429_raises_feed_error_after_retries(self):
        def opener(request):
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", {}, None)

        client = feeds.FeedClient(self.inbox, opener=opener,
                                  sleeper=self.sleeps.append)
        with self.assertRaises(feeds.FeedError):
            feeds.fetch_one(client, {"kind": "treasury_dts"}, FETCHED_AT)
        self.assertEqual(len(self.sleeps), 2)

    def test_fetch_all_collects_new_kind_failures(self):
        def opener(request):
            raise urllib.error.URLError("down")

        client = feeds.FeedClient(self.inbox, opener=opener,
                                  sleeper=self.sleeps.append)
        paths, errors = feeds.fetch_all(client, [
            {"kind": "nws_observations", "station": "KDME"},
            {"kind": "federal_register", "term": "Demonstration"},
            {"kind": "treasury_dts"},
            {"kind": "usaspending", "state": "XX",
             "county_fips": "99999", "start": "2026-01-01",
             "end": "2026-03-02"},
        ], FETCHED_AT)
        self.assertEqual(paths, [])
        self.assertEqual(len(errors), 4)


class TestNewKindRegistry(unittest.TestCase):
    def test_defaults_for_new_kinds(self):
        entries = registry.load_registry({"feeds": [
            {"kind": "nws_observations", "station": "KDME"},
            {"kind": "federal_register", "term": "Demonstration"},
            {"kind": "treasury_dts"},
            {"kind": "usaspending", "state": "XX",
             "county_fips": "99999", "start": "2026-01-01",
             "end": "2026-03-02"},
        ]})
        by_kind = {e["kind"]: e for e in entries}
        self.assertEqual(by_kind["nws_observations"]["tier"], "hourly")
        self.assertEqual(by_kind["federal_register"]["tier"], "daily")
        self.assertEqual(by_kind["treasury_dts"]["scope"], "national")
        self.assertEqual(by_kind["usaspending"]["categories"],
                         ["economic"])

    def test_either_parameter_rules(self):
        with self.assertRaises(registry.RegistryError):
            registry.load_registry({"feeds": [
                {"kind": "nws_observations"},
            ]})
        with self.assertRaises(registry.RegistryError):
            registry.load_registry({"feeds": [
                {"kind": "federal_register"},
            ]})
        entries = registry.load_registry({"feeds": [
            {"kind": "nws_observations", "lat": 32.0, "lon": -86.0},
        ]})
        self.assertEqual(len(entries), 1)

    def test_sample_region_carries_disabled_examples(self):
        entries = registry.load_region_registry(REGION)
        self.assertEqual(
            [e["kind"] for e in entries],
            ["nws_observations", "federal_register", "treasury_dts",
             "usaspending"])
        self.assertTrue(all(not e["enabled"] for e in entries))


if __name__ == "__main__":
    unittest.main()
