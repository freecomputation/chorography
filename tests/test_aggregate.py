import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography import aggregate, harvest
from chorography.ingest import parse_inbox

ROOT = os.path.join(os.path.dirname(__file__), "..")
REGION = os.path.join(ROOT, "data", "sample-region")
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
NOW = datetime(2026, 3, 2, 7, 0, 0, tzinfo=timezone.utc)


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


class TestCanonicalUrl(unittest.TestCase):
    def test_tracking_fragment_and_case_removed(self):
        self.assertEqual(
            aggregate.canonical_url(
                "HTTP://Example.TEST/news/story/?utm_source=wire"
                "&b=2&a=1#top"),
            "http://example.test/news/story?a=1&b=2")

    def test_root_path_keeps_slash(self):
        self.assertEqual(aggregate.canonical_url("http://x.test/"),
                         "http://x.test/")


class TestDedupe(unittest.TestCase):
    def test_canonical_url_and_title_copies_collapse(self):
        items = [
            {"title": "Council approves budget",
             "url": "http://x.test/a?utm_source=wire"},
            {"title": "Council approves budget",
             "url": "http://x.test/a"},
            {"title": "Council Approves Budget!",
             "url": "http://x.test/other"},
            {"title": "School board meets",
             "url": "http://x.test/b"},
        ]
        kept, removed = aggregate.dedupe_items(items)
        self.assertEqual(len(kept), 2)
        self.assertEqual(removed, 2)
        self.assertEqual(kept[0]["url"], items[0]["url"])

    def test_near_duplicate_titles_collapse(self):
        items = [
            {"title": "Council approves demonstration budget for "
                      "Sample Region",
             "url": "http://x.test/1"},
            {"title": "Council approves demonstration budget for "
                      "Sample Region today",
             "url": "http://x.test/2"},
            {"title": "Bridge inspection closes one lane",
             "url": "http://x.test/3"},
        ]
        kept, removed = aggregate.dedupe_items(items)
        self.assertEqual(len(kept), 2)
        self.assertEqual(removed, 1)


class TestSaturation(unittest.TestCase):
    def test_half_at_unit_weight(self):
        self.assertEqual(aggregate.saturate(1, 1.0, 1.0), 0.5)

    def test_cap_is_never_reached(self):
        value = aggregate.saturate(100000, 1.0, 0.4)
        self.assertLess(value, 0.4)
        self.assertGreater(value, 0.39)

    def test_monotonic_in_count(self):
        values = [aggregate.saturate(n, 0.6, 1.0)
                  for n in (1, 2, 5, 20)]
        self.assertEqual(values, sorted(values))


class TestAggregateEvents(unittest.TestCase):
    def test_grouping_caps_and_sentiment(self):
        items = [
            {"title": "One", "tier": "local",
             "categories": ["information"], "tone": 0.4},
            {"title": "Two", "tier": "local",
             "categories": ["information"], "tone": -0.2},
            {"title": "Three", "tier": "national",
             "categories": ["information"], "tone": None},
        ]
        events = aggregate.aggregate_events(items, 3, "rss", 0.6,
                                            "2026-03-02")
        by_tier = {e["tier"]: e for e in events}
        self.assertEqual(by_tier["local"]["item_count"], 2)
        self.assertEqual(by_tier["local"]["sentiment"], 0.1)
        self.assertEqual(by_tier["local"]["magnitude"], 0.5455)
        self.assertEqual(by_tier["national"]["sentiment"], 0.0)
        self.assertLessEqual(by_tier["national"]["magnitude"], 0.4)
        self.assertEqual(by_tier["local"]["duplicates_removed"], 3)
        self.assertIn("saturating count", by_tier["local"]["counting"])

    def test_items_from_gdelt(self):
        raw = json.dumps({"articles": [
            {"title": "A", "url": "http://x.test/a",
             "seendate": "20260302T060000"},
            {"title": "B", "url": "http://x.test/b",
             "seendate": "20260302T061500"},
        ]}).encode("utf-8")
        items = aggregate.items_from_gdelt(raw)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["published"], "20260302T060000")

    def test_items_from_rss_fixture(self):
        items = aggregate.items_from_rss(
            fixture_bytes("aggregate_feed.rss"))
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["title"],
                         "Sample Region Council Approves "
                         "Demonstration Budget")


class HarvestAggregateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.region = os.path.join(self.tmp.name, "region")
        os.makedirs(self.region)
        with open(os.path.join(REGION, "tracts.geojson"), "rb") as fh:
            tracts = fh.read()
        with open(os.path.join(self.region, "tracts.geojson"), "wb") as fh:
            fh.write(tracts)
        self.inbox = os.path.join(self.tmp.name, "inbox")
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.archive_root = os.path.join(self.tmp.name, "archive")

    def tearDown(self):
        self.tmp.cleanup()

    def write_manifest(self, feeds_list):
        manifest = {"name": "Sample Region (Demonstration)",
                    "epoch_date": "2026-03-02", "seed": 1,
                    "feeds": feeds_list}
        with open(os.path.join(self.region, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh)

    def make_harvester(self, opener):
        return harvest.Harvester(
            self.region, self.inbox, self.state_path, self.archive_root,
            opener=opener, sleeper=lambda seconds: None,
            now_fn=lambda: NOW)

    def inbox_names(self):
        return sorted(os.listdir(self.inbox))

    def test_rss_harvest_counts_duplicated_story_once(self):
        self.write_manifest([{
            "kind": "rss", "url": "http://example.test/feed.rss",
            "filename": "feed.rss",
        }])
        body = fixture_bytes("aggregate_feed.rss")

        def opener(request):
            return FakeResponse(body)

        summary = self.make_harvester(opener).run_once()
        self.assertEqual(summary["succeeded"], ["rss"])
        names = self.inbox_names()
        self.assertNotIn("feed.rss", names)
        self.assertIn("aggregate-rss-information-local.json", names)
        with open(os.path.join(
                self.inbox,
                "aggregate-rss-information-local.json"),
                encoding="utf-8") as fh:
            envelope = json.load(fh)
        event = envelope["chorography_event"]
        self.assertEqual(event["item_count"], 2)
        self.assertEqual(event["duplicates_removed"], 1)
        self.assertEqual(event["magnitude"], 0.5455)
        self.assertEqual(event["tier"], "local")
        events, _records = parse_inbox(self.inbox, "2026-03-02")
        news = [e for e in events if e["kind"] == "news_cycle"]
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]["item_count"], 2)

    def test_gdelt_harvest_aggregates(self):
        self.write_manifest([{
            "kind": "gdelt", "query": "demonstration",
        }])
        payload = json.dumps({"articles": [
            {"title": "Sample Region Council Approves Budget",
             "url": "http://x.test/a?utm_medium=syndication",
             "seendate": "20260302T060000"},
            {"title": "Sample Region Council Approves Budget",
             "url": "http://x.test/a",
             "seendate": "20260302T060500"},
            {"title": "Sample Region Fair Announces Dates",
             "url": "http://x.test/b", "seendate": "20260302T061000"},
        ]}).encode("utf-8")

        def opener(request):
            return FakeResponse(payload)

        summary = self.make_harvester(opener).run_once()
        self.assertEqual(summary["succeeded"], ["gdelt"])
        names = self.inbox_names()
        self.assertNotIn("gdelt.json", names)
        aggregates = [n for n in names if n.startswith("aggregate-")]
        self.assertEqual(len(aggregates), 2)
        counts = set()
        for name in aggregates:
            with open(os.path.join(self.inbox, name),
                      encoding="utf-8") as fh:
                counts.add(json.load(fh)["chorography_event"]
                           ["item_count"])
        self.assertEqual(counts, {2})

    def test_hazard_feed_bypasses_aggregation(self):
        self.write_manifest([{
            "kind": "nws_alerts", "lat": 32.0, "lon": -86.0,
        }])
        with open(os.path.join(REGION, "inbox", "nws_alert.json"),
                  "rb") as fh:
            body = fh.read()

        def opener(request):
            return FakeResponse(body)

        summary = self.make_harvester(opener).run_once()
        self.assertEqual(summary["succeeded"], ["nws_alerts"])
        names = self.inbox_names()
        self.assertIn("nws_alerts.json", names)
        self.assertFalse(any(n.startswith("aggregate-")
                             for n in names))


if __name__ == "__main__":
    unittest.main()
