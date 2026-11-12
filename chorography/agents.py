"""Agent types and behaviour for Chorography.

Three agent families are modelled: household composites, institutions and
infrastructure nodes. Each family has a small number of types with rich
state. Behaviour is expressed as intent functions. An intent function reads
the state at tick t and returns a dict of proposed changes. Intents are
collected for every agent before any of them are applied, which is the
two-phase commit rule of the tick loop: no agent sees another agent's
tick t+1 state while deciding its own move.
"""

from dataclasses import dataclass, field, asdict


def clamp(value, low=0.0, high=1.0):
    if value < low:
        return low
    if value > high:
        return high
    return value


@dataclass
class Household:
    """A composite household agent.

    A composite represents weight people grouped in one synthesis cell. The
    weight is fractional state in its own right: the cell stands for weight
    households of the given size, income band and tenure, and fractional
    fields such as employment are shares of the cell, not all-or-nothing
    states of one family.
    """

    id: int
    tract: str
    cell_key: str
    weight: float
    size: int
    income_band: str
    tenure: str
    income_index: float
    employment: float = 0.7
    health: float = 0.7
    morale: float = 0.6
    savings_months: float = 3.0
    individual: bool = False

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass
class Institution:
    """An institution agent: school, clinic, employer, agency or outlet."""

    id: int
    tract: str
    kind: str
    name: str
    capacity: float
    service: float = 0.7
    stress: float = 0.3
    trust: float = 0.6
    budget_months: float = 12.0

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass
class Infrastructure:
    """An infrastructure node: power, water, road, hospital plant, network."""

    id: int
    tract: str
    kind: str
    name: str
    condition: float = 0.9
    outage: float = 0.0
    repair_capacity: float = 0.05

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


def household_intents(household, ctx):
    """Propose one day of change for a household composite.

    ctx carries the environment, the tract PMESII indices, the run
    parameters and a deterministic random stream for this tick.
    """
    env = ctx["env"]
    idx = ctx["tract_index"]
    params = ctx["params"]
    rng = ctx["rng"]
    hazard = env.get("hazard", 0.0)
    outage = ctx["tract_outage"]
    health_target = (
        0.55 * idx.get("health_status", 0.6)
        + 0.45 * idx.get("water_reliability", 0.6)
        - 0.35 * hazard
        - 0.20 * outage
    )
    morale_target = (
        0.40 * idx.get("employment_rate", 0.6)
        + 0.30 * idx.get("public_safety", 0.6)
        + 0.30 * idx.get("media_trust", 0.5)
        - 0.30 * hazard
        - 0.15 * outage
    )
    employment_target = (
        0.60 * idx.get("business_activity", 0.6)
        + 0.40 * idx.get("transport_access", 0.6)
        - 0.25 * hazard
    )
    jitter = (rng.random() - 0.5) * 0.004
    return {
        "health": (health_target - household.health) * 0.08
        + params["health_recovery"] * (0.7 - household.health),
        "morale": (morale_target - household.morale) * 0.10
        + params["morale_recovery"] * (0.6 - household.morale)
        + jitter,
        "employment": (employment_target - household.employment)
        * params["employment_adjustment"],
        "savings_months": (
            (household.income_index - 0.45) * 0.01
            - hazard * 0.05
            - outage * 0.03
        ),
    }


def institution_intents(institution, ctx):
    """Propose one day of change for an institution."""
    env = ctx["env"]
    idx = ctx["tract_index"]
    hazard = env.get("hazard", 0.0)
    demand = hazard * 0.6 + ctx["tract_outage"] * 0.4
    if institution.kind in ("hospital", "clinic"):
        demand += (1.0 - idx.get("health_status", 0.6)) * 0.3
    if institution.kind in ("school",):
        demand *= 0.5
    stress_target = clamp(0.25 + demand - institution.capacity * 0.15)
    service_target = clamp(
        institution.capacity * 0.55
        + idx.get("trust_institutions", 0.5) * 0.25
        - stress_target * 0.35
        + 0.20
    )
    return {
        "stress": (stress_target - institution.stress) * 0.20,
        "service": (service_target - institution.service) * 0.15,
        "trust": (idx.get("trust_institutions", 0.5) - institution.trust) * 0.03
        + ctx["params"]["trust_recovery"] * (0.6 - institution.trust),
        "budget_months": -0.01 - institution.stress * 0.01 + institution.service * 0.008,
    }


def infrastructure_intents(node, ctx):
    """Propose one day of change for an infrastructure node."""
    env = ctx["env"]
    hazard = env.get("hazard", 0.0)
    wear = 0.0008 + hazard * 0.01 * (1.0 - node.condition)
    repair = node.repair_capacity * ctx["params"]["repair_rate"] * 20.0 * node.outage
    outage_pressure = hazard * 0.03 * (1.1 - node.condition)
    return {
        "condition": -wear + node.outage * 0.002,
        "outage": outage_pressure - repair,
    }
