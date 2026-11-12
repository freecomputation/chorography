"""File level checks for the bundled United States region package.

These tests read the shipped data files only. They do not build the
national world; that is a benchmark, not a unit test. What they pin
is the package contract: the county count, the split layout, the
population reconciliation the manifest claims, registry validity,
and that one state on its own loads and builds through the same
code path the whole nation uses.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography.registry import load_region_registry
from chorography.world import build_world, load_region

REGION = os.path.join(os.path.dirname(__file__), "..", "data",
                      "usa-region")


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class TestUsaRegionPackage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = _load(os.path.join(REGION, "manifest.json"))
        cls.state_files = sorted(
            name for name in os.listdir(os.path.join(REGION, "states"))
            if name.endswith(".json"))
        cls.tracts = []
        for name in cls.state_files:
            data = _load(os.path.join(REGION, "states", name))
            cls.tracts.extend(data["tracts"])

    def test_manifest_identity(self):
        self.assertEqual(self.manifest["name"], "United States")
        self.assertEqual(self.manifest["counties"], 3222)
        self.assertIn("synthesis", self.manifest)
        self.assertFalse(self.manifest["demonstration"])

    def test_split_layout_complete(self):
        self.assertEqual(len(self.state_files), 52)
        self.assertEqual(len(self.tracts), 3222)
        for tract in self.tracts:
            self.assertIn("state", tract)
            self.assertIn("population", tract)
            for dim in ("size", "income", "tenure"):
                self.assertIn(dim, tract["marginals"])

    def test_population_reconciliation(self):
        size_categories = _load(os.path.join(
            REGION, "common.json"))["size_categories"]
        people = 0
        for tract in self.tracts:
            for category, count in tract["marginals"]["size"].items():
                people += count * size_categories[category]
        self.assertEqual(people,
                         self.manifest["people_from_size_marginals"])
        total = self.manifest["acs_national_population"]
        self.assertLess(abs(people - total) / total, 0.06)

    def test_centroid_geometry(self):
        geojson = _load(os.path.join(REGION, "tracts.geojson"))
        self.assertEqual(len(geojson["features"]), 3222)
        for feature in geojson["features"]:
            self.assertEqual(feature["geometry"]["type"], "Point")

    def test_registry_validates(self):
        entries = load_region_registry(REGION)
        self.assertEqual(len(entries), len(self.manifest["feeds"]))
        self.assertTrue(any(entry["enabled"] for entry in entries))

    def test_manifest_file_hashes(self):
        import hashlib
        for rel, digest in self.manifest["files"].items():
            path = os.path.join(REGION, rel)
            with open(path, "rb") as fh:
                actual = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(actual, digest, rel)

    def test_single_state_builds(self):
        tmp = tempfile.mkdtemp()
        try:
            shutil.copy(os.path.join(REGION, "manifest.json"), tmp)
            shutil.copy(os.path.join(REGION, "common.json"), tmp)
            os.mkdir(os.path.join(tmp, "states"))
            shutil.copy(os.path.join(REGION, "states", "wy.json"),
                        os.path.join(tmp, "states", "wy.json"))
            _manifest, aggregates, _infra = load_region(tmp)
            self.assertEqual(len(aggregates["tracts"]), 23)
            county_ids = {tract["id"] for tract in aggregates["tracts"]}
            world = build_world(tmp)
            self.assertTrue(world.households)
            for household in world.households:
                self.assertIn(household.tract, county_ids)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
