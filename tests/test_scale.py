import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography import synthpop
from chorography.world import build_world, load_region

MARGINALS = {
    "size": {"1": 100, "2": 150, "3": 50},
    "income": {"low": 120, "mid": 130, "high": 50},
    "tenure": {"owner": 180, "renter": 120},
}
SIZE_CATEGORIES = {"1": 1, "2": 2, "3": 3}
INCOME_BANDS = ["low", "mid", "high"]


class TestMergeCells(unittest.TestCase):
    def test_merge_reaches_target_and_conserves_weight(self):
        table, _it, _err = synthpop.ipf(MARGINALS)
        before = sum(v for v in table.values() if v > 1e-9)
        merged = synthpop.merge_cells(
            {k: v for k, v in table.items() if v > 1e-9}, 5, size_pos=0)
        self.assertEqual(len(merged), 5)
        self.assertAlmostEqual(sum(merged.values()), before, places=6)

    def test_merge_noop_at_or_below_target(self):
        table, _it, _err = synthpop.ipf(MARGINALS)
        live = {k: v for k, v in table.items() if v > 1e-9}
        merged = synthpop.merge_cells(live, len(live), size_pos=0)
        self.assertEqual(merged, live)

    def test_merge_is_deterministic(self):
        table, _it, _err = synthpop.ipf(MARGINALS)
        live = {k: v for k, v in table.items() if v > 1e-9}
        first = synthpop.merge_cells(live, 4, size_pos=0)
        second = synthpop.merge_cells(dict(live), 4, size_pos=0)
        self.assertEqual(first, second)


class TestTargetedSynthesis(unittest.TestCase):
    def test_target_limits_cells_and_conserves_weight(self):
        households, _next = synthpop.build_composites(
            "tract-x", MARGINALS, 1, SIZE_CATEGORIES, INCOME_BANDS,
            target_cells=6)
        self.assertLessEqual(len(households), 6)
        self.assertAlmostEqual(synthpop.total_weight(households), 300.0,
                               delta=1.0)

    def test_merging_protects_household_sizes(self):
        marginals = json.loads(json.dumps(MARGINALS))
        full, _next = synthpop.build_composites(
            "tract-x", marginals, 1, SIZE_CATEGORIES, INCOME_BANDS)
        capped, _next = synthpop.build_composites(
            "tract-x", marginals, 1, SIZE_CATEGORIES, INCOME_BANDS,
            target_cells=4)
        self.assertLessEqual(len(capped), 4)
        self.assertAlmostEqual(synthpop.total_people(full),
                               synthpop.total_people(capped), places=6)

    def test_no_target_uses_full_grid(self):
        households, _next = synthpop.build_composites(
            "tract-x", MARGINALS, 1, SIZE_CATEGORIES, INCOME_BANDS)
        full, _next = synthpop.build_composites(
            "tract-x", MARGINALS, 1, SIZE_CATEGORIES, INCOME_BANDS,
            target_cells=10 ** 6)
        self.assertEqual(len(households), len(full))
        self.assertEqual([h.weight for h in households],
                         [h.weight for h in full])


def _write(path, payload):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _split_region(root):
    os.makedirs(os.path.join(root, "states"))
    tract = {
        "id": "county-001", "name": "Alpha County, Alphaland",
        "state": "AL",
        "marginals": MARGINALS,
        "pmesii": {
            "political": {"civic_participation": 47,
                          "trust_institutions": 61},
            "military": {"emergency_readiness": 55, "public_safety": 69},
            "economic": {"business_activity": 58, "employment_rate": 60,
                         "income_level": 50000},
            "social": {"education_access": 30, "health_status": 92,
                       "morale": 60},
            "information": {"connectivity": 85, "media_trust": 45},
            "infrastructure": {"hospital_capacity": 62,
                               "power_reliability": 95,
                               "transport_access": 58,
                               "water_reliability": 93},
        },
    }
    metric_specs = {}
    for category, metrics in tract["pmesii"].items():
        metric_specs[category] = {
            metric: {"ref": 100.0, "units": "index points",
                     "source": "test"}
            for metric in metrics
        }
    metric_specs["economic"]["income_level"] = {
        "ref": 100000.0, "units": "dollars per year", "source": "test"}
    _write(os.path.join(root, "manifest.json"), {
        "name": "Split Test Region", "epoch_date": "2026-01-01",
        "seed": 7, "feeds": []})
    _write(os.path.join(root, "common.json"), {
        "income_bands": INCOME_BANDS, "size_categories": SIZE_CATEGORIES,
        "metric_specs": metric_specs})
    _write(os.path.join(root, "states", "al.json"), {
        "state": "AL", "tracts": [tract],
        "institutions": [{
            "tract": "county-001", "kind": "school", "name": "Alpha school",
            "capacity": 0.7, "service": 0.72, "stress": 0.3, "trust": 0.62,
            "budget_months": 12}],
        "nodes": [{
            "tract": "county-001", "kind": "power", "name": "Alpha power",
            "condition": 0.94, "outage": 0.0, "repair_capacity": 0.06}]})


class TestSplitLayout(unittest.TestCase):
    def test_split_region_loads_and_builds(self):
        with tempfile.TemporaryDirectory() as root:
            _split_region(root)
            manifest, aggregates, infrastructure = load_region(root)
            self.assertEqual(manifest["name"], "Split Test Region")
            self.assertEqual(len(aggregates["tracts"]), 1)
            self.assertEqual(len(infrastructure["institutions"]), 1)
            self.assertEqual(len(infrastructure["nodes"]), 1)
            world = build_world(root)
            self.assertEqual(world.region_name, "Split Test Region")
            self.assertEqual(
                world.geographies["county-001"]["state"], "AL")
            self.assertGreater(len(world.households), 0)

    def test_synthesis_settings_come_from_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            _split_region(root)
            manifest_path = os.path.join(root, "manifest.json")
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
            manifest["synthesis"] = {
                "people_per_composite": 100, "min_composites": 2,
                "max_composites": 4}
            _write(manifest_path, manifest)
            world = build_world(root)
            mine = [h for h in world.households
                    if h.tract == "county-001"]
            self.assertLessEqual(len(mine), 4)
            self.assertAlmostEqual(synthpop.total_weight(mine), 300.0,
                                   delta=1.0)


if __name__ == "__main__":
    unittest.main()
