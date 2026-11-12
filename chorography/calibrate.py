"""Calibration: pattern scorecards, divergence flags and grid search.

Calibration here is pattern-oriented. The scorecard checks named
patterns, each a history metric with an expected band and a tolerance,
rather than fitting one series point by point. A divergence flag is
raised when an observed series sits outside a band around an expected
series for several consecutive ticks, which separates a sustained
departure from one noisy day.

The grid search is deliberately small: it ranges over the few free
parameters of the engine and runs each point as a batch of short
excursions on deep copies. A held-out check scores the second part of
the history separately, so a parameter set that only fits the part it
was tuned on is visible in the report. Reports state plainly that a
good scorecard fit does not make outputs predictions: different
parameter sets can fit the same patterns, which is equifinality, and
the model form itself differs from the real region, which is model
discrepancy.
"""

import copy
import itertools

DEFAULT_PATTERNS = [
    {"name": "morale in a plausible band", "key": "morale_mean",
     "low": 0.30, "high": 0.85},
    {"name": "health in a plausible band", "key": "health_mean",
     "low": 0.30, "high": 0.90},
    {"name": "employment in a plausible band", "key": "employment_mean",
     "low": 0.25, "high": 0.95},
    {"name": "outage stays limited", "key": "outage_mean",
     "low": 0.0, "high": 0.40},
    {"name": "infrastructure condition holds", "key": "condition_mean",
     "low": 0.40, "high": 1.0},
]


def score_patterns(history, patterns=None):
    """Score a run history against named patterns.

    Each pattern is scored on the mean of its metric over the history.
    Returns a list of per-pattern results and an overall pass count.
    """
    patterns = patterns or DEFAULT_PATTERNS
    results = []
    for pattern in patterns:
        values = [entry[pattern["key"]] for entry in history
                  if pattern["key"] in entry]
        if not values:
            results.append({"name": pattern["name"], "key": pattern["key"],
                            "observed": None, "passed": False,
                            "note": "metric absent from history"})
            continue
        observed = sum(values) / len(values)
        passed = pattern["low"] <= observed <= pattern["high"]
        results.append({
            "name": pattern["name"],
            "key": pattern["key"],
            "observed": observed,
            "low": pattern["low"],
            "high": pattern["high"],
            "passed": passed,
            "days": len(values),
        })
    return {
        "patterns": results,
        "passed": sum(1 for r in results if r["passed"]),
        "total": len(results),
    }


def divergence_flags(observed, expected, band, consecutive=3):
    """Flag sustained departures of observed from expected.

    Returns a list of flags, each with the start index, the length of
    the run outside the band and the largest deviation in the run.
    """
    flags = []
    run_start = None
    run_max = 0.0
    for i, (obs, exp) in enumerate(zip(observed, expected)):
        deviation = abs(obs - exp)
        if deviation > band:
            if run_start is None:
                run_start = i
                run_max = deviation
            else:
                run_max = max(run_max, deviation)
        else:
            if run_start is not None and i - run_start >= consecutive:
                flags.append({"start": run_start, "end": i - 1,
                              "length": i - run_start,
                              "max_deviation": run_max})
            run_start = None
            run_max = 0.0
    if run_start is not None and len(observed) - run_start >= consecutive:
        flags.append({"start": run_start, "end": len(observed) - 1,
                      "length": len(observed) - run_start,
                      "max_deviation": run_max})
    return flags


def held_out_check(history, patterns=None, holdout_fraction=0.3):
    """Score the fit part and the held-out part of a history separately."""
    if len(history) < 4:
        return {"error": "history too short for a held-out check",
                "days": len(history)}
    cut = int(len(history) * (1.0 - holdout_fraction))
    fit = score_patterns(history[:cut], patterns)
    held = score_patterns(history[cut:], patterns)
    return {
        "fit_days": cut,
        "held_out_days": len(history) - cut,
        "fit": {"passed": fit["passed"], "total": fit["total"]},
        "held_out": {"passed": held["passed"], "total": held["total"]},
        "held_out_patterns": held["patterns"],
    }


def grid_search(world, param_grid, days=14, patterns=None):
    """Search a small grid of free parameters by batch excursions.

    world is the template reference world; every grid point runs on a
    deep copy, so the template is unchanged. Returns ranked results,
    best first, scored by patterns passed and then by distance of the
    tracked means from the centres of their bands.
    """
    keys = sorted(param_grid.keys())
    results = []
    for combo in itertools.product(*(param_grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        branch = copy.deepcopy(world)
        branch.params.update(params)
        branch.run_days(days)
        card = score_patterns(branch.history[-days:], patterns)
        distance = 0.0
        for result in card["patterns"]:
            if result.get("observed") is not None:
                centre = (result["low"] + result["high"]) / 2.0
                distance += abs(result["observed"] - centre)
        results.append({
            "params": params,
            "passed": card["passed"],
            "total": card["total"],
            "distance": distance,
        })
    results.sort(key=lambda r: (-r["passed"], r["distance"]))
    return {
        "ranked": results,
        "best": results[0] if results else None,
        "days_per_run": days,
        "note": ("A best grid point is the best fit to the stated "
                 "patterns only. Other parameter sets may fit the same "
                 "patterns (equifinality), and the model form differs "
                 "from the real region (model discrepancy), so a fit "
                 "is not a prediction."),
    }
