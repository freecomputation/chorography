"""The reference world: state, PMESII graph and the tick loop.

One tick is one day. Every tick runs the same named stages in the same
order: ingest, environment, institutions, population, interaction,
bookkeeping. Stages that move agents work in two phases: intents are
computed from the tick t state for all agents first, then applied
together to produce tick t+1. Agents are always iterated sorted by
integer id.

Determinism is a load-bearing feature. Randomness comes only from
streams derived from the run seed, the stream purpose and the day
index, so two runs with the same seed and the same exogenous events
produce byte-identical canonical state, and branched excursions share
common random numbers with the reference world day for day. No
wall-clock reading and no hash-order iteration appears in model logic.
"""

import copy
import hashlib
import json
import os
from datetime import date, timedelta
from random import Random

from .agents import (Household, Infrastructure, Institution, clamp,
                     household_intents, infrastructure_intents,
                     institution_intents)
from . import synthpop

PMESII_CATEGORIES = (
    "political",
    "military",
    "economic",
    "social",
    "information",
    "infrastructure",
)

DEFAULT_PARAMS = {
    "effect_scale": 1.0,
    "morale_recovery": 0.02,
    "health_recovery": 0.01,
    "repair_rate": 0.05,
    "employment_adjustment": 0.10,
    "trust_recovery": 0.01,
}

STAGES = (
    "ingest",
    "environment",
    "institutions",
    "population",
    "interaction",
    "bookkeeping",
)


def scalar(value, ref, units, source):
    """A named PMESII scalar: raw value, normalized index, units, source."""
    index = clamp(value / ref) if ref else 0.0
    return {
        "value": float(value),
        "index": index,
        "ref": float(ref),
        "units": units,
        "source": source,
    }


def load_effects(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "data", "effects.json")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data["effects"]


def _canonical(obj):
    if isinstance(obj, float):
        return round(obj, 8)
    if isinstance(obj, dict):
        return {k: _canonical(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, (list, tuple)):
        return [_canonical(v) for v in obj]
    return obj


def canonical_json(state):
    return json.dumps(_canonical(state), sort_keys=True,
                      separators=(",", ":"))


def hash_state(state):
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


class World:
    def __init__(self, region_name, geographies, households, institutions,
                 infrastructure, effects, seed, start_date, params=None,
                 demonstration=False):
        self.region_name = region_name
        self.geographies = geographies
        self.households = sorted(households, key=lambda a: a.id)
        self.institutions = sorted(institutions, key=lambda a: a.id)
        self.infrastructure = sorted(infrastructure, key=lambda a: a.id)
        self.effects = effects
        self.seed = int(seed)
        self.day = 0
        self.date = start_date
        self.params = dict(DEFAULT_PARAMS)
        if params:
            self.params.update(params)
        self.demonstration = bool(demonstration)
        self.env = {
            "temp_anomaly": 0.0,
            "precip_index": 0.5,
            "hazard": 0.0,
            "hazard_kind": "none",
        }
        self.active_shocks = []
        self.pending_events = []
        self.event_log = []
        self.history = []

    def rng_for(self, purpose):
        """A deterministic stream for one purpose on the current day."""
        return Random("chorography:{}:{}:{}".format(self.seed, purpose,
                                                     self.day))

    def tract_ids(self):
        return sorted(self.geographies.keys())

    def tract_index(self, tract):
        geo = self.geographies[tract]
        out = {}
        for category in PMESII_CATEGORIES:
            for metric, sc in geo["pmesii"].get(category, {}).items():
                out[metric] = sc["index"]
        return out

    def tract_outage(self, tract):
        nodes = [n for n in self.infrastructure if n.tract == tract]
        if not nodes:
            return 0.0
        return sum(n.outage for n in nodes) / len(nodes)

    def to_state(self):
        return {
            "region_name": self.region_name,
            "demonstration": self.demonstration,
            "seed": self.seed,
            "day": self.day,
            "date": self.date.isoformat(),
            "params": dict(self.params),
            "env": dict(self.env),
            "geographies": copy.deepcopy(self.geographies),
            "households": [h.to_dict() for h in self.households],
            "institutions": [i.to_dict() for i in self.institutions],
            "infrastructure": [n.to_dict() for n in self.infrastructure],
            "active_shocks": copy.deepcopy(self.active_shocks),
            "event_log": copy.deepcopy(self.event_log),
            "history": copy.deepcopy(self.history),
        }

    @classmethod
    def from_state(cls, state, effects):
        world = cls(
            region_name=state["region_name"],
            geographies=copy.deepcopy(state["geographies"]),
            households=[Household.from_dict(d) for d in state["households"]],
            institutions=[Institution.from_dict(d)
                          for d in state["institutions"]],
            infrastructure=[Infrastructure.from_dict(d)
                            for d in state["infrastructure"]],
            effects=effects,
            seed=state["seed"],
            start_date=date.fromisoformat(state["date"]),
            params=state.get("params"),
            demonstration=state.get("demonstration", False),
        )
        world.day = state["day"]
        world.env = dict(state["env"])
        world.active_shocks = copy.deepcopy(state.get("active_shocks", []))
        world.event_log = copy.deepcopy(state.get("event_log", []))
        world.history = copy.deepcopy(state.get("history", []))
        return world

    def state_hash(self):
        return hash_state(self.to_state())

    # Stage 1: ingest. Exogenous events enter here and nowhere else.
    def stage_ingest(self, events):
        for event in events or []:
            record = dict(event)
            record["applied_day"] = self.day
            self.event_log.append(record)
            kind = record.get("kind", "")
            if kind in ("weather_alert", "earthquake", "disease_outbreak"):
                self.active_shocks.append({
                    "kind": kind,
                    "magnitude": float(record.get("magnitude", 0.3)),
                    "remaining": int(record.get("duration_days", 2)),
                    "tract": record.get("tract"),
                })
            elif kind == "outage":
                tract = record.get("tract")
                for node in self.infrastructure:
                    if tract is None or node.tract == tract:
                        if record.get("node_kind") in (None, node.kind):
                            node.outage = clamp(
                                node.outage + float(record.get("magnitude", 0.4)))
            elif kind == "intervention":
                self._apply_intervention(record)
            elif kind == "news_cycle":
                sentiment = float(record.get("sentiment", 0.0))
                for tract in self.tract_ids():
                    self._shift_metric(tract, "information", "media_trust",
                                       sentiment * 0.02)

    def _apply_intervention(self, record):
        category = record.get("category")
        metric = record.get("metric")
        delta = float(record.get("delta_index", 0.0))
        tracts = [record["tract"]] if record.get("tract") else self.tract_ids()
        for tract in tracts:
            self._shift_metric(tract, category, metric, delta)
        duration = int(record.get("duration_days", 1))
        if duration > 1:
            self.active_shocks.append({
                "kind": "intervention",
                "magnitude": delta,
                "remaining": duration - 1,
                "tract": record.get("tract"),
                "category": category,
                "metric": metric,
                "per_day": delta,
            })

    def _shift_metric(self, tract, category, metric, delta_index):
        geo = self.geographies.get(tract)
        if not geo:
            return
        sc = geo["pmesii"].get(category, {}).get(metric)
        if sc is None:
            return
        sc["index"] = clamp(sc["index"] + delta_index)
        sc["value"] = sc["index"] * sc["ref"]

    # Stage 2: environment. Hazard decays, shocks act, weather turns.
    def stage_environment(self):
        rng = self.rng_for("environment")
        hazard = 0.0
        hazard_kind = "none"
        still_active = []
        for shock in self.active_shocks:
            if shock["kind"] == "intervention":
                tracts = ([shock["tract"]] if shock.get("tract")
                          else self.tract_ids())
                for tract in tracts:
                    self._shift_metric(tract, shock["category"],
                                       shock["metric"], shock["per_day"])
            else:
                mag = shock["magnitude"] * (0.6 + 0.4 * shock["remaining"]
                                            / max(shock["remaining"], 1))
                if shock["kind"] == "weather_alert":
                    mag = shock["magnitude"]
                if mag > hazard:
                    hazard = mag
                    hazard_kind = shock["kind"]
                if shock["kind"] in ("weather_alert", "earthquake"):
                    tract = shock.get("tract")
                    for node in self.infrastructure:
                        if tract is None or node.tract == tract:
                            if shock["kind"] == "earthquake":
                                node.condition = clamp(
                                    node.condition - shock["magnitude"] * 0.02,
                                    0.05, 1.0)
                                node.outage = clamp(
                                    node.outage + shock["magnitude"] * 0.05)
                            else:
                                node.outage = clamp(
                                    node.outage + shock["magnitude"] * 0.02)
            shock["remaining"] -= 1
            if shock["remaining"] > 0:
                still_active.append(shock)
        self.active_shocks = still_active
        seasonal = 0.5 + 0.5 * _seasonal(self.date)
        noise = (rng.random() - 0.5) * 0.1
        self.env["precip_index"] = clamp(seasonal * 0.6 + 0.4 * 0.5 + noise)
        self.env["temp_anomaly"] = round(
            _seasonal(self.date) * 6.0 + (rng.random() - 0.5) * 1.5, 4)
        self.env["hazard"] = clamp(hazard)
        self.env["hazard_kind"] = hazard_kind

    def _tract_maps(self):
        """Per tract PMESII index dicts and mean outages, computed once.

        The two-phase stages read tract context for every agent, and
        the direct scans behind tract_index and tract_outage rescan
        the geography and the infrastructure list on every call. At
        sample scale that is trivial. At national scale it is the
        whole cost of a tick. Computing both maps once per stage
        hands every agent in the stage exactly the values the per
        agent scans would have returned, because neither the PMESII
        graph nor node outages change while a stage computes its
        intents: changes land only in the apply half of a stage or in
        a later stage. The outage half groups the infrastructure list
        once instead of rescanning it per tract, summing each tract's
        nodes in list order exactly as tract_outage does.
        """
        index = {}
        for tract in self.tract_ids():
            index[tract] = self.tract_index(tract)
        grouped = {}
        for node in self.infrastructure:
            grouped.setdefault(node.tract, []).append(node)
        outage = {}
        for tract in self.tract_ids():
            nodes = grouped.get(tract, [])
            if nodes:
                outage[tract] = (sum(n.outage for n in nodes)
                                 / len(nodes))
            else:
                outage[tract] = 0.0
        return index, outage

    # Stage 3: institutions, two-phase.
    def stage_institutions(self):
        rng = self.rng_for("institutions")
        index, outage = self._tract_maps()
        intents = []
        for inst in self.institutions:
            ctx = {
                "env": self.env,
                "tract_index": index[inst.tract],
                "tract_outage": outage[inst.tract],
                "params": self.params,
                "rng": rng,
            }
            intents.append((inst, institution_intents(inst, ctx)))
        for inst, delta in intents:
            inst.stress = clamp(inst.stress + delta["stress"])
            inst.service = clamp(inst.service + delta["service"])
            inst.trust = clamp(inst.trust + delta["trust"])
            inst.budget_months = max(0.0, inst.budget_months
                                     + delta["budget_months"])

    # Stage 4: population, two-phase.
    def stage_population(self):
        rng = self.rng_for("behaviour")
        index, outage = self._tract_maps()
        prepared = []
        for household in self.households:
            ctx = {
                "env": self.env,
                "tract_index": index[household.tract],
                "tract_outage": outage[household.tract],
                "params": self.params,
                "rng": rng,
            }
            prepared.append((household, household_intents(household, ctx)))
        for household, delta in prepared:
            household.health = clamp(household.health + delta["health"])
            household.morale = clamp(household.morale + delta["morale"])
            household.employment = clamp(
                household.employment + delta["employment"])
            household.savings_months = max(
                0.0, household.savings_months + delta["savings_months"])

    # Stage 5: interaction. Infrastructure moves, agent aggregates feed
    # the PMESII graph, and the sparse effect table couples categories.
    def stage_interaction(self):
        rng = self.rng_for("interaction")
        prepared = []
        for node in self.infrastructure:
            ctx = {"env": self.env, "params": self.params, "rng": rng}
            prepared.append((node, infrastructure_intents(node, ctx)))
        for node, delta in prepared:
            node.condition = clamp(node.condition + delta["condition"],
                                   0.05, 1.0)
            node.outage = clamp(node.outage + delta["outage"])
        self._feedback_from_agents()
        self.apply_effects()

    def _feedback_from_agents(self):
        # Agents are grouped by tract in one pass each. Grouping
        # preserves the per tract member order of the agent lists,
        # which are sorted by id, so every weighted sum below adds
        # the same values in the same order as a per tract scan of
        # the full lists would. The grouping is what makes a tick
        # over thousands of tracts cost one pass instead of one pass
        # per tract.
        households_by_tract = {}
        for household in self.households:
            households_by_tract.setdefault(household.tract,
                                           []).append(household)
        nodes_by_tract = {}
        for node in self.infrastructure:
            nodes_by_tract.setdefault(node.tract, []).append(node)
        institutions_by_tract = {}
        for inst in self.institutions:
            institutions_by_tract.setdefault(inst.tract, []).append(inst)
        for tract in self.tract_ids():
            members = households_by_tract.get(tract, [])
            if members:
                wsum = sum(h.weight for h in members)
                morale = sum(h.morale * h.weight for h in members) / wsum
                health = sum(h.health * h.weight for h in members) / wsum
                employment = (sum(h.employment * h.weight for h in members)
                              / wsum)
                self._set_index(tract, "social", "morale", morale)
                self._set_index(tract, "social", "health_status", health)
                self._set_index(tract, "economic", "employment_rate",
                                employment)
            nodes = nodes_by_tract.get(tract, [])
            by_kind = {}
            for node in nodes:
                by_kind.setdefault(node.kind, []).append(node)
            mapping = {
                "power": ("infrastructure", "power_reliability"),
                "water": ("infrastructure", "water_reliability"),
                "road": ("infrastructure", "transport_access"),
                "hospital": ("infrastructure", "hospital_capacity"),
            }
            for kind, (category, metric) in mapping.items():
                if kind in by_kind:
                    group = by_kind[kind]
                    level = sum(n.condition * (1.0 - n.outage)
                                for n in group) / len(group)
                    self._set_index(tract, category, metric, level)
            local = institutions_by_tract.get(tract, [])
            if local:
                trust = sum(i.trust for i in local) / len(local)
                self._set_index(tract, "political", "trust_institutions",
                                trust)

    def _set_index(self, tract, category, metric, index):
        sc = self.geographies[tract]["pmesii"][category][metric]
        sc["index"] = clamp(index)
        sc["value"] = sc["index"] * sc["ref"]

    def apply_effects(self):
        scale = self.params.get("effect_scale", 1.0)
        for effect in self.effects:
            for tract in self.tract_ids():
                geo = self.geographies[tract]
                src = geo["pmesii"].get(effect["source_category"], {}).get(
                    effect["source_metric"])
                tgt = geo["pmesii"].get(effect["target_category"], {}).get(
                    effect["target_metric"])
                if src is None or tgt is None:
                    continue
                delta = (effect["coefficient"]
                         * (src["index"] - tgt["index"]) * scale)
                tgt["index"] = clamp(tgt["index"] + delta)
                tgt["value"] = tgt["index"] * tgt["ref"]

    # Stage 6: bookkeeping. Aggregates are recorded, then time advances.
    def stage_bookkeeping(self):
        entry = {
            "day": self.day,
            "date": self.date.isoformat(),
            "hazard": self.env["hazard"],
            "hazard_kind": self.env["hazard_kind"],
        }
        if self.households:
            wsum = sum(h.weight for h in self.households)
            entry["morale_mean"] = (sum(h.morale * h.weight
                                         for h in self.households) / wsum)
            entry["health_mean"] = (sum(h.health * h.weight
                                        for h in self.households) / wsum)
            entry["employment_mean"] = (
                sum(h.employment * h.weight for h in self.households) / wsum)
            entry["people"] = sum(h.weight * h.size for h in self.households)
        if self.infrastructure:
            entry["outage_mean"] = (
                sum(n.outage for n in self.infrastructure)
                / len(self.infrastructure))
            entry["condition_mean"] = (
                sum(n.condition for n in self.infrastructure)
                / len(self.infrastructure))
        means = {}
        for category in PMESII_CATEGORIES:
            values = []
            for tract in self.tract_ids():
                for sc in self.geographies[tract]["pmesii"].get(
                        category, {}).values():
                    values.append(sc["index"])
            if values:
                means[category] = sum(values) / len(values)
        entry["pmesii"] = means
        self.history.append(entry)
        self.day += 1
        self.date = self.date + timedelta(days=1)

    def tick(self, events=None):
        """Advance one day through the fixed named stages."""
        self.stage_ingest(events)
        self.stage_environment()
        self.stage_institutions()
        self.stage_population()
        self.stage_interaction()
        self.stage_bookkeeping()

    def run_days(self, days, events_by_day=None):
        events_by_day = events_by_day or {}
        for _ in range(days):
            self.tick(events_by_day.get(self.day, []))


def _seasonal(day):
    """A smooth annual cycle from the day of year, in minus one to one."""
    import math
    return math.sin(2.0 * math.pi * (day.timetuple().tm_yday - 80) / 365.0)


def load_region(region_dir):
    """Load a region directory into plain data structures.

    Two layouts are supported. The simple layout carries one
    aggregates.json and, when present, one infrastructure.json, as
    the sample region does. The split layout, for regions too large
    for single files, carries a common.json with the shared
    synthesis fields and a states directory whose per state files
    each hold that state's tracts, institutions and infrastructure
    nodes. Both layouts return the same three structures. Split
    files are merged in sorted file name order so loading is
    deterministic.
    """
    with open(os.path.join(region_dir, "manifest.json"),
              encoding="utf-8") as fh:
        manifest = json.load(fh)
    agg_path = os.path.join(region_dir, "aggregates.json")
    if os.path.exists(agg_path):
        with open(agg_path, encoding="utf-8") as fh:
            aggregates = json.load(fh)
        infra_path = os.path.join(region_dir, "infrastructure.json")
        infrastructure = {"nodes": [], "institutions": []}
        if os.path.exists(infra_path):
            with open(infra_path, encoding="utf-8") as fh:
                infrastructure = json.load(fh)
        return manifest, aggregates, infrastructure
    with open(os.path.join(region_dir, "common.json"),
              encoding="utf-8") as fh:
        aggregates = json.load(fh)
    tracts = []
    institutions = []
    nodes = []
    states_dir = os.path.join(region_dir, "states")
    for name in sorted(os.listdir(states_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(states_dir, name), encoding="utf-8") as fh:
            part = json.load(fh)
        tracts.extend(part.get("tracts", []))
        institutions.extend(part.get("institutions", []))
        nodes.extend(part.get("nodes", []))
    aggregates["tracts"] = tracts
    infrastructure = {"institutions": institutions, "nodes": nodes}
    return manifest, aggregates, infrastructure


def _synthesis_target(tract, synthesis, size_categories):
    """The composite cell target for one tract, or None for no cap.

    The target is the tract's population divided by the configured
    people per composite, clamped between the configured floor and
    ceiling. A tract that carries no population figure is estimated
    from its size marginal, which counts households by size and so
    sums to the represented people up to the size categories'
    representative values.
    """
    people = tract.get("population")
    if people is None:
        size_marginal = tract.get("marginals", {}).get("size", {})
        people = sum(count * size_categories.get(cat, 1)
                     for cat, count in size_marginal.items())
    divisor = synthesis.get("people_per_composite", 10000)
    target = int(round(people / divisor))
    floor = synthesis.get("min_composites", 1)
    ceiling = synthesis.get("max_composites")
    if target < floor:
        target = floor
    if ceiling is not None and target > ceiling:
        target = ceiling
    return target


def build_world(region_dir, seed=None, effects=None, params=None):
    """Build a reference world from a region directory."""
    manifest, aggregates, infrastructure = load_region(region_dir)
    if seed is None:
        seed = manifest.get("seed", 1)
    if effects is None:
        effects = load_effects()
    geographies = {}
    metric_specs = aggregates["metric_specs"]
    for tract in aggregates["tracts"]:
        pmesii = {}
        for category in PMESII_CATEGORIES:
            pmesii[category] = {}
            for metric, spec in metric_specs.get(category, {}).items():
                raw = tract["pmesii"][category][metric]
                pmesii[category][metric] = scalar(
                    raw, spec["ref"], spec["units"], spec["source"])
        geography = {
            "name": tract["name"],
            "pmesii": pmesii,
        }
        if tract.get("state"):
            geography["state"] = tract["state"]
        geographies[tract["id"]] = geography
    households = []
    next_id = 1
    size_categories = aggregates.get("size_categories", {})
    income_bands = aggregates["income_bands"]
    synthesis = manifest.get("synthesis") or {}
    for tract in aggregates["tracts"]:
        target = None
        if synthesis:
            target = _synthesis_target(tract, synthesis, size_categories)
        built, next_id = synthpop.build_composites(
            tract["id"], tract["marginals"], next_id, size_categories,
            income_bands, target_cells=target)
        households.extend(built)
    institutions = []
    for i, node in enumerate(infrastructure.get("institutions", [])):
        institutions.append(Institution(
            id=10000 + i, tract=node["tract"], kind=node["kind"],
            name=node["name"], capacity=node.get("capacity", 0.6),
            service=node.get("service", 0.7), stress=node.get("stress", 0.3),
            trust=node.get("trust", 0.6),
            budget_months=node.get("budget_months", 12.0)))
    nodes = []
    for i, node in enumerate(infrastructure.get("nodes", [])):
        nodes.append(Infrastructure(
            id=20000 + i, tract=node["tract"], kind=node["kind"],
            name=node["name"], condition=node.get("condition", 0.9),
            outage=node.get("outage", 0.0),
            repair_capacity=node.get("repair_capacity", 0.05)))
    start = date.fromisoformat(manifest["epoch_date"])
    return World(
        region_name=manifest["name"],
        geographies=geographies,
        households=households,
        institutions=institutions,
        infrastructure=nodes,
        effects=effects,
        seed=seed,
        start_date=start,
        params=params,
        demonstration=manifest.get("demonstration", False),
    )
