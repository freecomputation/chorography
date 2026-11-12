import os
import sys
import unittest
from random import Random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chorography import synthpop
from chorography.world import build_world

REGION = os.path.join(os.path.dirname(__file__), "..",
                      "data", "sample-region")

MARGINALS = {
    "size": {"1": 100, "2": 150, "3": 50},
    "income": {"low": 120, "mid": 130, "high": 50},
    "tenure": {"owner": 180, "renter": 120},
}


class TestIPF(unittest.TestCase):
    def test_ipf_converges(self):
        table, iterations, err = synthpop.ipf(MARGINALS)
        self.assertLess(iterations, 1000)
        self.assertLess(err, 1e-6)

    def test_ipf_matches_marginals(self):
        table, _it, _err = synthpop.ipf(MARGINALS)
        dims = list(MARGINALS.keys())
        grand = sum(MARGINALS["size"].values())
        for di, dim in enumerate(dims):
            scale = grand / sum(MARGINALS[dim].values())
            for cat, target in MARGINALS[dim].items():
                got = sum(v for k, v in table.items() if k[di] == cat)
                self.assertAlmostEqual(got, target * scale, places=4)

    def test_ipf_total_is_grand_total(self):
        table, _it, _err = synthpop.ipf(MARGINALS)
        self.assertAlmostEqual(sum(table.values()), 300.0, places=4)

    def test_ipf_deterministic(self):
        first, _a, _b = synthpop.ipf(MARGINALS)
        second, _a, _b = synthpop.ipf(MARGINALS)
        self.assertEqual(first, second)


class TestSynthesis(unittest.TestCase):
    def setUp(self):
        self.world = build_world(REGION)

    def test_household_count_positive(self):
        self.assertGreater(len(self.world.households), 50)

    def test_synthesis_totals_match_households(self):
        total = synthpop.total_weight(self.world.households)
        expected = 420 + 350 + 280
        self.assertAlmostEqual(total, expected, delta=2.0)

    def test_people_total(self):
        people = synthpop.total_people(self.world.households)
        self.assertGreater(people, 2000)
        self.assertLess(people, 3000)

    def test_every_tract_represented(self):
        tracts = {h.tract for h in self.world.households}
        self.assertEqual(tracts, {"tract-001", "tract-002", "tract-003"})

    def test_ids_sorted_and_unique(self):
        ids = [h.id for h in self.world.households]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))


class TestExpansionCollapse(unittest.TestCase):
    def setUp(self):
        self.world = build_world(REGION)
        self.composite = self.world.households[0]

    def test_expansion_count(self):
        people = synthpop.expand_composite(
            self.composite, Random(42), start_id=9000)
        self.assertEqual(len(people), max(1, round(self.composite.weight)))
        self.assertTrue(all(p.individual for p in people))

    def test_expansion_weight_conserved(self):
        people = synthpop.expand_composite(
            self.composite, Random(42), start_id=9000)
        self.assertAlmostEqual(sum(p.weight for p in people),
                               self.composite.weight, places=9)

    def test_collapse_recovers_weight(self):
        people = synthpop.expand_composite(
            self.composite, Random(42), start_id=9000)
        collapsed = synthpop.collapse_households(people, start_id=1)
        self.assertEqual(len(collapsed), 1)
        self.assertAlmostEqual(collapsed[0].weight,
                               self.composite.weight, places=9)
        self.assertFalse(collapsed[0].individual)

    def test_full_population_conservation(self):
        rng = Random(7)
        everyone = []
        next_id = 100000
        for comp in self.world.households:
            made = synthpop.expand_composite(comp, rng, start_id=next_id)
            next_id += len(made)
            everyone.extend(made)
        before = synthpop.total_weight(self.world.households)
        after = sum(p.weight for p in everyone)
        self.assertAlmostEqual(before, after, places=6)
        collapsed = synthpop.collapse_households(everyone, start_id=1)
        self.assertAlmostEqual(synthpop.total_weight(collapsed), before,
                               places=6)
        self.assertEqual(len(collapsed), len(self.world.households))

    def test_expansion_deterministic_under_seed(self):
        a = synthpop.expand_composite(self.composite, Random(3), 5000)
        b = synthpop.expand_composite(self.composite, Random(3), 5000)
        self.assertEqual([p.morale for p in a], [p.morale for p in b])

    def test_expansion_varies_under_other_seed(self):
        a = synthpop.expand_composite(self.composite, Random(3), 5000)
        b = synthpop.expand_composite(self.composite, Random(4), 5000)
        self.assertNotEqual([p.morale for p in a], [p.morale for p in b])


if __name__ == "__main__":
    unittest.main()
