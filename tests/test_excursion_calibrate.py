import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography.calibrate import (divergence_flags, grid_search,
                                   held_out_check, score_patterns)
from chorography.excursion import run_excursion
from chorography.report import build_report, render_json, render_text
from chorography.world import build_world

REGION = os.path.join(os.path.dirname(__file__), "..",
                      "data", "sample-region")

SCRIPT = {
    "name": "grid support",
    "days": 8,
    "seeds": [20260302, 11],
    "interventions": [
        {"day_offset": 0, "kind": "outage", "node_kind": "power",
         "magnitude": 0.5},
    ],
}


class TestExcursion(unittest.TestCase):
    def setUp(self):
        self.world = build_world(REGION)
        self.world.run_days(2)

    def test_reference_world_immutable(self):
        before = self.world.state_hash()
        run_excursion(self.world, SCRIPT)
        self.assertEqual(self.world.state_hash(), before)

    def test_common_random_numbers_fairness(self):
        reference = build_world(REGION)
        reference.run_days(6)
        world = build_world(REGION)
        result = run_excursion(world, {
            "name": "empty", "days": 6, "seeds": [20260302],
            "interventions": []})
        branch = result["branch_metrics"][0]
        self.assertEqual(branch["morale_mean"],
                         reference.history[-1]["morale_mean"])
        self.assertEqual(branch["health_mean"],
                         reference.history[-1]["health_mean"])
        self.assertEqual(result["contrast"]["morale_mean"], 0.0)

    def test_outage_intervention_moves_contrast(self):
        result = run_excursion(self.world, SCRIPT)
        self.assertGreater(result["contrast"]["outage_mean"], 0.01)

    def test_ensemble_shape(self):
        result = run_excursion(self.world, SCRIPT)
        self.assertEqual(len(result["branch_metrics"]), 2)
        summary = result["branch_summary"]["morale_mean"]
        self.assertEqual(summary["n"], 2)
        self.assertLessEqual(summary["min"], summary["mean"])
        self.assertLessEqual(summary["mean"], summary["max"])
        self.assertIn("not predictions", result["note"])

    def test_parameter_override_applies_to_branch_only(self):
        result = run_excursion(self.world, {
            "name": "no effects", "days": 5, "seeds": [20260302],
            "interventions": [],
            "parameters": {"effect_scale": 0.0}})
        self.assertEqual(self.world.params["effect_scale"], 1.0)
        self.assertIn("morale_mean", result["branch_summary"])


class TestCalibrate(unittest.TestCase):
    def setUp(self):
        self.world = build_world(REGION)
        self.world.run_days(12)

    def test_scorecard_scores(self):
        card = score_patterns(self.world.history)
        self.assertEqual(card["total"], 5)
        self.assertGreaterEqual(card["passed"], 3)
        for result in card["patterns"]:
            self.assertIn("observed", result)

    def test_scorecard_fails_out_of_band_pattern(self):
        card = score_patterns(self.world.history, patterns=[
            {"name": "impossible morale", "key": "morale_mean",
             "low": 0.99, "high": 1.0}])
        self.assertEqual(card["passed"], 0)

    def test_divergence_flags_sustained_departure(self):
        observed = [0.5] * 5 + [0.9] * 4 + [0.5] * 3
        expected = [0.5] * 12
        flags = divergence_flags(observed, expected, band=0.1,
                                 consecutive=3)
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["start"], 5)
        self.assertEqual(flags[0]["length"], 4)

    def test_divergence_ignores_single_spike(self):
        observed = [0.5, 0.95, 0.5, 0.5]
        expected = [0.5] * 4
        self.assertEqual(
            divergence_flags(observed, expected, band=0.1), [])

    def test_held_out_check(self):
        result = held_out_check(self.world.history)
        self.assertEqual(result["fit_days"] + result["held_out_days"], 12)
        self.assertEqual(result["held_out"]["total"], 5)

    def test_grid_search_ranks_and_preserves_world(self):
        before = self.world.state_hash()
        result = grid_search(self.world,
                             {"morale_recovery": [0.0, 0.05]}, days=5)
        self.assertEqual(self.world.state_hash(), before)
        self.assertEqual(len(result["ranked"]), 2)
        self.assertIsNotNone(result["best"])
        self.assertIn("equifinality", result["note"])


class TestReport(unittest.TestCase):
    def test_text_report_contents(self):
        world = build_world(REGION)
        world.run_days(3)
        text = render_text(build_report(world))
        self.assertIn("PMESII dashboard", text)
        self.assertIn("Demonstration region", text)
        self.assertIn("never predictions", text)
        for category in ("political", "military", "economic", "social",
                         "information", "infrastructure"):
            self.assertIn(category, text)

    def test_json_report_parses(self):
        world = build_world(REGION)
        world.run_days(2)
        result = run_excursion(world, SCRIPT)
        parsed = json.loads(render_json(build_report(world, result)))
        self.assertIn("excursion", parsed)
        self.assertIn("contrast", parsed["excursion"])
        self.assertEqual(parsed["region"], world.region_name)


if __name__ == "__main__":
    unittest.main()
