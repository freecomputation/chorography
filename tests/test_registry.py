import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography import feeds, registry

ROOT = os.path.join(os.path.dirname(__file__), "..")
REGION = os.path.join(ROOT, "data", "sample-region")


class TestRegistryValidation(unittest.TestCase):
    def test_old_style_spec_validates_with_defaults(self):
        entries = registry.load_registry({"feeds": [
            {"kind": "nws_alerts", "lat": 32.0, "lon": -86.0},
        ]})
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["id"], "nws_alerts")
        self.assertEqual(entry["tier"], "fifteen_minute")
        self.assertEqual(entry["interval_seconds"], 900)
        self.assertEqual(entry["scope"], "local")
        self.assertEqual(entry["trust"], 0.9)
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["lat"], 32.0)

    def test_sample_region_manifest_loads(self):
        entries = registry.load_region_registry(REGION)
        self.assertEqual(entries, [])

    def test_unknown_kind_rejected(self):
        with self.assertRaises(registry.RegistryError):
            registry.load_registry({"feeds": [{"kind": "mystery"}]})

    def test_missing_required_parameter_rejected(self):
        with self.assertRaises(registry.RegistryError):
            registry.load_registry({"feeds": [
                {"kind": "usgs_water"},
            ]})

    def test_unknown_tier_rejected(self):
        with self.assertRaises(registry.RegistryError):
            registry.load_registry({"feeds": [
                {"kind": "rss", "url": "https://x.example/f",
                 "filename": "f.rss", "tier": "whenever"},
            ]})

    def test_non_positive_interval_rejected(self):
        with self.assertRaises(registry.RegistryError):
            registry.load_registry({"feeds": [
                {"kind": "rss", "url": "https://x.example/f",
                 "filename": "f.rss", "interval_seconds": 0},
            ]})

    def test_unknown_category_rejected(self):
        with self.assertRaises(registry.RegistryError):
            registry.load_registry({"feeds": [
                {"kind": "rss", "url": "https://x.example/f",
                 "filename": "f.rss", "categories": ["weather"]},
            ]})

    def test_trust_out_of_range_rejected(self):
        with self.assertRaises(registry.RegistryError):
            registry.load_registry({"feeds": [
                {"kind": "rss", "url": "https://x.example/f",
                 "filename": "f.rss", "trust": 1.5},
            ]})

    def test_unknown_scope_rejected(self):
        with self.assertRaises(registry.RegistryError):
            registry.load_registry({"feeds": [
                {"kind": "rss", "url": "https://x.example/f",
                 "filename": "f.rss", "scope": "nearby"},
            ]})

    def test_duplicate_kinds_get_unique_ids(self):
        entries = registry.load_registry({"feeds": [
            {"kind": "rss", "url": "https://a.example/f",
             "filename": "a.rss"},
            {"kind": "rss", "url": "https://b.example/f",
             "filename": "b.rss"},
        ]})
        self.assertEqual([e["id"] for e in entries], ["rss", "rss-2"])

    def test_explicit_id_is_kept(self):
        entries = registry.load_registry({"feeds": [
            {"kind": "rss", "url": "https://a.example/f",
             "filename": "a.rss", "id": "county-paper"},
        ]})
        self.assertEqual(entries[0]["id"], "county-paper")

    def test_interval_override_wins_over_tier(self):
        entries = registry.load_registry({"feeds": [
            {"kind": "rss", "url": "https://a.example/f",
             "filename": "a.rss", "interval_seconds": 123},
        ]})
        self.assertEqual(entries[0]["interval_seconds"], 123)

    def test_kind_defaults_cover_every_feed_handler(self):
        self.assertEqual(set(registry.KIND_DEFAULTS),
                         set(feeds.FEED_HANDLERS))
        self.assertEqual(set(registry.REQUIRED_PARAMS),
                         set(feeds.FEED_HANDLERS))


if __name__ == "__main__":
    unittest.main()
