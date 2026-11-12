"""Source registry: loading and validating region feed entries.

The region manifest carries a feeds list. In version 0.1.0 each entry
named a feed kind and the parameters that kind's fetcher needs, and
the run command's live flag handed the list to the fetch layer as it
stood. The harvester needs more from each entry: how often the source
may be fetched, what geography it speaks for, which PMESII categories
it may feed, how far its word is trusted, and whether it is enabled at
all. This module is where those fields are given defaults and checked,
so the scheduler runs on validated data and a malformed registry entry
fails at load time with a named problem instead of at fetch time with
a stack trace.

Validation is backward compatible with the 0.1.0 shape. An entry that
carries only a kind and its fetcher parameters validates unchanged,
with the cadence tier, scope, categories and trust weight filled from
per kind defaults recorded here. Those defaults are assumptions about
each source family's nature, held in one table where they can be read
and argued with, and any entry may override any of them.

Each validated entry gains a stable identifier used as its key in the
harvest state file and the archive index. An entry may name its own
identifier with an id field. Otherwise the identifier is the kind,
with a numeric suffix when a registry carries the same kind twice.
"""

import json
import os

from . import feeds
from .world import PMESII_CATEGORIES

TIER_INTERVALS = {
    "fifteen_minute": 15 * 60,
    "hourly": 60 * 60,
    "daily": 24 * 60 * 60,
    "release": 7 * 24 * 60 * 60,
    "annual": 365 * 24 * 60 * 60,
}

SCOPES = ("local", "state", "national", "global")

KIND_DEFAULTS = {
    "nws_alerts": {
        "tier": "fifteen_minute", "scope": "local",
        "categories": ["military", "infrastructure"], "trust": 0.9,
    },
    "nws_forecast": {
        "tier": "hourly", "scope": "local",
        "categories": [], "trust": 0.8,
    },
    "usgs_earthquakes": {
        "tier": "fifteen_minute", "scope": "local",
        "categories": ["infrastructure", "social"], "trust": 0.9,
    },
    "usgs_water": {
        "tier": "hourly", "scope": "local",
        "categories": ["infrastructure"], "trust": 0.8,
    },
    "gdelt": {
        "tier": "fifteen_minute", "scope": "global",
        "categories": ["information", "political"], "trust": 0.5,
    },
    "rss": {
        "tier": "hourly", "scope": "local",
        "categories": ["information"], "trust": 0.6,
    },
    "bls": {
        "tier": "release", "scope": "national",
        "categories": ["economic"], "trust": 0.9,
    },
    "fred": {
        "tier": "release", "scope": "national",
        "categories": ["economic"], "trust": 0.8,
    },
}

REQUIRED_PARAMS = {
    "nws_alerts": ("lat", "lon"),
    "nws_forecast": ("lat", "lon"),
    "usgs_earthquakes": ("start", "end"),
    "usgs_water": ("site",),
    "gdelt": ("query",),
    "rss": ("url", "filename"),
    "bls": ("series",),
    "fred": ("series",),
}


class RegistryError(Exception):
    pass


def validate_spec(spec, position=None):
    """Validate one feed entry and return its normalised form.

    The normalised form is a new dictionary carrying every original
    field plus id, tier, interval_seconds, scope, categories, trust
    and enabled with defaults applied. Raises RegistryError naming the
    problem when the entry cannot be run as written.
    """
    where = "entry {}".format(position) if position is not None else "entry"
    if not isinstance(spec, dict):
        raise RegistryError("{} is not a JSON object".format(where))
    kind = spec.get("kind")
    if kind not in feeds.FEED_HANDLERS:
        raise RegistryError(
            "{} has unknown feed kind {!r}".format(where, kind))
    defaults = KIND_DEFAULTS[kind]
    for param in REQUIRED_PARAMS[kind]:
        if param not in spec:
            raise RegistryError(
                "{} of kind {} is missing required parameter {}".format(
                    where, kind, param))
    entry = dict(spec)
    tier = spec.get("tier", defaults["tier"])
    if tier not in TIER_INTERVALS:
        raise RegistryError(
            "{} names unknown cadence tier {!r}".format(where, tier))
    interval = spec.get("interval_seconds", TIER_INTERVALS[tier])
    if not isinstance(interval, (int, float)) or interval <= 0:
        raise RegistryError(
            "{} has a non-positive interval_seconds".format(where))
    scope = spec.get("scope", defaults["scope"])
    if scope not in SCOPES:
        raise RegistryError(
            "{} names unknown geography scope {!r}".format(where, scope))
    categories = spec.get("categories", list(defaults["categories"]))
    if not isinstance(categories, list):
        raise RegistryError("{} has non-list categories".format(where))
    for category in categories:
        if category not in PMESII_CATEGORIES:
            raise RegistryError(
                "{} names unknown PMESII category {!r}".format(
                    where, category))
    trust = spec.get("trust", defaults["trust"])
    if not isinstance(trust, (int, float)) or not 0.0 <= trust <= 1.0:
        raise RegistryError(
            "{} has trust outside the range 0 to 1".format(where))
    enabled = spec.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RegistryError("{} has a non-boolean enabled flag".format(where))
    entry.update({
        "tier": tier,
        "interval_seconds": interval,
        "scope": scope,
        "categories": list(categories),
        "trust": float(trust),
        "enabled": enabled,
    })
    return entry


def assign_ids(entries):
    """Give each entry a stable unique id, in registry order."""
    used = set()
    for entry in entries:
        base = entry.get("id") or entry["kind"]
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = "{}-{}".format(base, suffix)
            suffix += 1
        entry["id"] = candidate
        used.add(candidate)
    return entries


def load_registry(manifest):
    """Validate the feeds list of a region manifest dictionary."""
    specs = manifest.get("feeds", [])
    if not isinstance(specs, list):
        raise RegistryError("manifest feeds field is not a list")
    entries = [validate_spec(spec, position=i)
               for i, spec in enumerate(specs)]
    return assign_ids(entries)


def load_region_registry(region_dir):
    """Load and validate the registry of the region in a directory."""
    path = os.path.join(region_dir, "manifest.json")
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    return load_registry(manifest)


def interval_seconds(entry):
    return entry["interval_seconds"]
