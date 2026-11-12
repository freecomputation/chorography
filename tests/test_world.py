import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography import world as world_mod
from chorography.ingest import parse_inbox
from chorography.world import build_world

REGION = os.path.join(os.path.dirname(__file__), "..",
                      "data", "sample-region")

GOLDEN_HASH = "f18ddc7f2832d8d9e7c09d005ae422360f97dbd4d1b3badcdd8706b52f2c32f8"


def golden_world():
    world = build_world(REGION)
    events, _records = parse_inbox(os.path.join(REGION, "inbox"),
                                   world.date.isoformat())
    world.tick(events)
    world.run_days(9)
    return world


class TestDeterminism(unittest.TestCase):
    def test_same_seed_identical_hash(self):
        first = build_world(REGION)
        second = build_world(REGION)
        first.run_days(8)
        second.run_days(8)
        self.assertEqual(first.state_hash(), second.state_hash())

    def test_different_seed_different_hash(self):
        first = build_world(REGION, seed=1)
        second = build_world(REGION, seed=2)
        first.run_days(3)
        second.run_days(3)
        self.assertNotEqual(first.state_hash(), second.state_hash())

    def test_golden_run(self):
        self.assertEqual(golden_world().state_hash(), GOLDEN_HASH)

    def test_state_roundtrip_preserves_hash(self):
        world = build_world(REGION)
        world.run_days(4)
        clone = world_mod.World.from_state(world.to_state(),
                                           world.effects)
        self.assertEqual(clone.state_hash(), world.state_hash())
        clone.run_days(3)
        world.run_days(3)
        self.assertEqual(clone.state_hash(), world.state_hash())


class TestTickSemantics(unittest.TestCase):
    def test_stage_names_fixed(self):
        self.assertEqual(world_mod.STAGES,
                         ("ingest", "environment", "institutions",
                          "population", "interaction", "bookkeeping"))

    def test_household_intents_see_tick_t_state(self):
        world = build_world(REGION)
        seen = []
        original = world_mod.household_intents

        def spy(household, ctx):
            seen.append(household.employment)
            return original(household, ctx)

        world_mod.household_intents = spy
        try:
            before = [h.employment for h in world.households]
            world.stage_population()
        finally:
            world_mod.household_intents = original
        self.assertEqual(seen, before)

    def test_institution_intents_see_tick_t_state(self):
        world = build_world(REGION)
        seen = []
        original = world_mod.institution_intents

        def spy(inst, ctx):
            seen.append(inst.stress)
            return original(inst, ctx)

        world_mod.institution_intents = spy
        try:
            before = [i.stress for i in world.institutions]
            world.stage_institutions()
        finally:
            world_mod.institution_intents = original
        self.assertEqual(seen, before)

    def test_one_tick_advances_one_day(self):
        world = build_world(REGION)
        start_date = world.date
        world.tick()
        self.assertEqual(world.day, 1)
        self.assertEqual((world.date - start_date).days, 1)
        self.assertEqual(len(world.history), 1)

    def test_history_records_tracked_metrics(self):
        world = build_world(REGION)
        world.run_days(3)
        for entry in world.history:
            for key in ("morale_mean", "health_mean", "employment_mean",
                        "outage_mean", "pmesii"):
                self.assertIn(key, entry)

    def test_values_stay_in_bounds(self):
        world = build_world(REGION)
        world.tick([{"kind": "earthquake", "magnitude": 1.0,
                     "duration_days": 5}])
        world.run_days(20)
        for h in world.households:
            for value in (h.health, h.morale, h.employment):
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)
        for node in world.infrastructure:
            self.assertGreaterEqual(node.condition, 0.05)
            self.assertLessEqual(node.outage, 1.0)


class TestEffectsAndEvents(unittest.TestCase):
    def test_effect_table_relaxes_target_toward_source(self):
        effects = [{
            "source_category": "infrastructure",
            "source_metric": "power_reliability",
            "target_category": "economic",
            "target_metric": "business_activity",
            "coefficient": 0.1,
        }]
        world = build_world(REGION, effects=effects)
        geo = world.geographies["tract-001"]
        geo["pmesii"]["infrastructure"]["power_reliability"]["index"] = 0.9
        geo["pmesii"]["economic"]["business_activity"]["index"] = 0.5
        world.apply_effects()
        got = geo["pmesii"]["economic"]["business_activity"]["index"]
        self.assertAlmostEqual(got, 0.54, places=6)
        self.assertAlmostEqual(
            geo["pmesii"]["economic"]["business_activity"]["value"],
            0.54 * 100.0, places=4)

    def test_effect_scale_zero_disables_effects(self):
        effects = [{
            "source_category": "infrastructure",
            "source_metric": "power_reliability",
            "target_category": "economic",
            "target_metric": "business_activity",
            "coefficient": 0.5,
        }]
        world = build_world(REGION, effects=effects,
                            params={"effect_scale": 0.0})
        geo = world.geographies["tract-001"]
        geo["pmesii"]["infrastructure"]["power_reliability"]["index"] = 0.9
        geo["pmesii"]["economic"]["business_activity"]["index"] = 0.5
        world.apply_effects()
        self.assertEqual(
            geo["pmesii"]["economic"]["business_activity"]["index"], 0.5)

    def test_weather_alert_raises_hazard(self):
        world = build_world(REGION)
        self.assertEqual(world.env["hazard"], 0.0)
        world.tick([{"kind": "weather_alert", "magnitude": 0.7,
                     "duration_days": 2}])
        self.assertGreater(world.history[-1]["hazard"], 0.5)

    def test_outage_event_hits_nodes(self):
        world = build_world(REGION)
        world.tick([{"kind": "outage", "magnitude": 0.5,
                     "node_kind": "power", "tract": "tract-001"}])
        power = [n for n in world.infrastructure
                 if n.kind == "power" and n.tract == "tract-001"]
        self.assertGreater(power[0].outage, 0.2)
        other = [n for n in world.infrastructure
                 if n.kind == "power" and n.tract == "tract-002"]
        self.assertLess(other[0].outage, 0.1)

    def test_intervention_shifts_metric(self):
        world = build_world(REGION)
        geo = world.geographies["tract-001"]
        before = geo["pmesii"]["information"]["media_trust"]["index"]
        world.stage_ingest([{
            "kind": "intervention", "category": "information",
            "metric": "media_trust", "delta_index": 0.1,
            "tract": "tract-001", "duration_days": 1}])
        after = geo["pmesii"]["information"]["media_trust"]["index"]
        self.assertAlmostEqual(after, before + 0.1, places=6)

    def test_pmesii_categories_complete(self):
        world = build_world(REGION)
        for tract in world.tract_ids():
            self.assertEqual(
                sorted(world.geographies[tract]["pmesii"].keys()),
                sorted(world_mod.PMESII_CATEGORIES))

    def test_scalars_carry_units_and_source(self):
        world = build_world(REGION)
        sc = world.geographies["tract-001"]["pmesii"]["economic"][
            "income_level"]
        self.assertEqual(sc["units"], "dollars per year")
        self.assertTrue(sc["source"])
        self.assertAlmostEqual(sc["index"], sc["value"] / sc["ref"],
                               places=6)


if __name__ == "__main__":
    unittest.main()
