import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography import relevance

ROOT = os.path.join(os.path.dirname(__file__), "..")
REGION = os.path.join(ROOT, "data", "sample-region")

MANUAL = {
    "region_name": "Manual Region",
    "local_names": ["Demonstration County", "Sample Region"],
    "state_names": ["Demonstration State"],
    "codes": ["ALZ029"],
    "bbox": [-86.0, 32.0, -85.85, 32.05],
    "empty": False,
}


class TestGazetteerFromSampleRegion(unittest.TestCase):
    def test_sample_region_gazetteer(self):
        gazetteer = relevance.build_gazetteer(REGION)
        for got, want in zip(gazetteer["bbox"],
                             [-86.0, 32.0, -85.85, 32.05]):
            self.assertAlmostEqual(got, want, places=6)
        self.assertIn("Sample Region", gazetteer["local_names"])
        self.assertIn("tract-001", gazetteer["codes"])
        self.assertFalse(gazetteer["empty"])

    def test_census_style_tract_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracts = {"type": "FeatureCollection", "features": [{
                "type": "Feature",
                "properties": {
                    "id": "tract-01-003-020100",
                    "name": ("Census Tract 201, Autauga County, "
                             "Alabama"),
                },
                "geometry": None,
            }]}
            with open(os.path.join(tmp, "tracts.geojson"), "w",
                      encoding="utf-8") as fh:
                json.dump(tracts, fh)
            with open(os.path.join(tmp, "manifest.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"name": "Autauga", "feeds": []}, fh)
            gazetteer = relevance.build_gazetteer(tmp)
        self.assertIn("Autauga County", gazetteer["local_names"])
        self.assertIn("Alabama", gazetteer["state_names"])
        self.assertIn("01003", gazetteer["codes"])
        self.assertIsNone(gazetteer["bbox"])


class TestClassify(unittest.TestCase):
    def test_local_by_name(self):
        item = {"title": "Demonstration County fair opens Saturday"}
        self.assertEqual(relevance.classify(item, MANUAL), "local")

    def test_state_by_name(self):
        item = {"title": "Demonstration State legislature adjourns"}
        self.assertEqual(relevance.classify(item, MANUAL), "state")

    def test_local_by_code(self):
        item = {"title": "Zone forecast issued for ALZ029 tonight"}
        self.assertEqual(relevance.classify(item, MANUAL), "local")

    def test_local_by_coordinates(self):
        item = {"title": "Storm damage reported", "lat": 32.02,
                "lon": -85.97}
        self.assertEqual(relevance.classify(item, MANUAL), "local")

    def test_coordinates_outside_box_fall_through(self):
        item = {"title": "Storm damage reported", "lat": 40.0,
                "lon": -85.97}
        self.assertEqual(relevance.classify(item, MANUAL), "discarded")

    def test_discarded_without_fallback(self):
        item = {"title": "Far away team wins far away game"}
        self.assertEqual(relevance.classify(item, MANUAL), "discarded")

    def test_fallback_applies_when_no_signal(self):
        item = {"title": "Markets move on the day"}
        self.assertEqual(
            relevance.classify(item, MANUAL, fallback="national"),
            "national")

    def test_word_boundaries_prevent_partial_matches(self):
        item = {"title": "Samples Regionally gathered for study"}
        self.assertEqual(relevance.classify(item, MANUAL), "discarded")


if __name__ == "__main__":
    unittest.main()
