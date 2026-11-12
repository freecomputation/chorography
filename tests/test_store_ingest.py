import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography.ingest import parse_inbox
from chorography.store import Store, replay
from chorography.world import build_world, load_effects

REGION = os.path.join(os.path.dirname(__file__), "..",
                      "data", "sample-region")
INBOX = os.path.join(REGION, "inbox")


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.events, self.records = parse_inbox(INBOX, "2026-03-02")
        self.by_file = {r["file"]: r for r in self.records}

    def test_alert_fixture_becomes_weather_event(self):
        kinds = [e["kind"] for e in self.events]
        self.assertIn("weather_alert", kinds)
        alert = [e for e in self.events if e["kind"] == "weather_alert"][0]
        self.assertEqual(alert["severity"], "severe")
        self.assertAlmostEqual(alert["magnitude"], 0.7)

    def test_quake_fixture_becomes_earthquake_event(self):
        quakes = [e for e in self.events if e["kind"] == "earthquake"]
        self.assertEqual(len(quakes), 1)
        self.assertAlmostEqual(quakes[0]["magnitude_raw"], 5.4)

    def test_provenance_carried(self):
        for event in self.events:
            prov = event["provenance"]
            self.assertIn("source", prov)
            self.assertEqual(len(prov["raw_sha256"]), 64)

    def test_records_cover_every_file(self):
        self.assertEqual(len(self.records), 6)
        for name in ("nws_alert.json", "usgs_quake.json", "news.rss",
                     "stale.rss", "error_429.txt", "truncated.json"):
            self.assertIn(name, self.by_file)

    def test_error_fixtures_do_not_crash(self):
        self.assertEqual(self.by_file["error_429.txt"]["status"], "error")
        self.assertEqual(self.by_file["truncated.json"]["status"], "error")
        bad = {"error_429.txt", "truncated.json"}
        for event in self.events:
            self.assertNotIn(event["provenance"]["raw_sha256"],
                             {self.by_file[f]["raw_sha256"] for f in bad})

    def test_stale_feed_flagged(self):
        self.assertEqual(self.by_file["stale.rss"]["status"], "stale")
        stale_events = [e for e in self.events
                        if e["kind"] == "news_cycle" and e.get("stale")]
        self.assertEqual(len(stale_events), 1)

    def test_fresh_feed_not_stale(self):
        self.assertEqual(self.by_file["news.rss"]["status"], "ok")

    def test_missing_inbox_is_an_outage_not_a_crash(self):
        events, records = parse_inbox(
            os.path.join(REGION, "no-such-inbox"), "2026-03-02")
        self.assertEqual(events, [])
        self.assertEqual(records, [])

    def test_world_runs_on_error_fixtures_alone(self):
        world = build_world(REGION)
        world.tick(self.events)
        world.run_days(3)
        self.assertEqual(world.day, 4)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "world.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_roundtrip(self):
        world = build_world(REGION)
        store = Store(self.db)
        state = world.to_state()
        store.save_snapshot("initial", 0, state, world.state_hash())
        got = store.snapshot("initial")
        store.close()
        self.assertEqual(got["state_hash"], world.state_hash())
        self.assertEqual(got["state"]["day"], 0)

    def test_event_schema_version_guard(self):
        store = Store(self.db)
        with self.assertRaises(ValueError):
            store.append_event("exogenous", 0, {}, schema_version=99)
        store.close()

    def test_events_come_back_in_order(self):
        store = Store(self.db)
        store.append_event("exogenous", 0, {"kind": "a"})
        store.append_event("exogenous", 0, {"kind": "b"})
        kinds = [r["payload"]["kind"] for r in store.events("exogenous")]
        store.close()
        self.assertEqual(kinds, ["a", "b"])

    def test_replay_reproduces_final_hash(self):
        world = build_world(REGION)
        store = Store(self.db)
        state = world.to_state()
        store.save_snapshot("initial", world.day, state,
                            world.state_hash())
        events, _records = parse_inbox(INBOX, world.date.isoformat())
        for i in range(6):
            todays = events if i == 0 else []
            for event in todays:
                store.append_event("exogenous", world.day, event)
            world.tick(todays)
        state = world.to_state()
        store.save_snapshot("latest", world.day, state,
                            world.state_hash())
        _world, replayed, stored = replay(store, load_effects())
        store.close()
        self.assertEqual(replayed, stored)
        self.assertEqual(replayed, world.state_hash())


if __name__ == "__main__":
    unittest.main()
