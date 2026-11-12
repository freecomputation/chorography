"""Excursions: branched copies of the reference world.

An excursion snapshots the reference world, branches it by deep copy,
applies a course-of-action script, and runs the branch faster than real
time. The reference world is never the branch, so running excursions
cannot move it; a test in the suite proves its hash is unchanged.

Scripts are JSON data, not code. A script names dated interventions
(day offsets from the branch point, or absolute world days) and
parameter overrides. Ensembles run the same script under several
branch seeds. Common random numbers are the fairness rule: streams are
derived from seed, purpose and day, so a branch and the reference
world draw the same numbers on the same day whenever their seeds
match, and an intervention-free branch with the reference seed
reproduces the reference run exactly.

Results are distributions and contrasts, never single point outcomes:
for each tracked metric the branch ensemble and the reference ensemble
are summarised, and the contrast is branch minus reference.
"""

import copy
import json
import statistics

from .world import World

TRACKED_METRICS = (
    "morale_mean",
    "health_mean",
    "employment_mean",
    "outage_mean",
    "condition_mean",
)


def load_script(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _interventions_by_day(script, start_day):
    by_day = {}
    for item in script.get("interventions", []):
        if "day" in item:
            day = int(item["day"])
        else:
            day = start_day + int(item.get("day_offset", 0))
        event = {k: v for k, v in item.items()
                 if k not in ("day", "day_offset")}
        by_day.setdefault(day, []).append(event)
    return by_day


def _summarise(values):
    if not values:
        return {"n": 0}
    summary = {
        "n": len(values),
        "mean": statistics.fmean(values),
        "min": min(values),
        "max": max(values),
    }
    if len(values) > 1:
        summary["stdev"] = statistics.pstdev(values)
    else:
        summary["stdev"] = 0.0
    return summary


def _run_branch(world, seed, days, interventions_by_day, params):
    branch = copy.deepcopy(world)
    branch.seed = int(seed)
    if params:
        branch.params.update(params)
    for _ in range(days):
        branch.tick(interventions_by_day.get(branch.day, []))
    return branch


def final_metrics(world):
    if not world.history:
        return {}
    last = world.history[-1]
    out = {k: last[k] for k in TRACKED_METRICS if k in last}
    for category, value in last.get("pmesii", {}).items():
        out["pmesii_" + category] = value
    return out


def run_excursion(world, script, days=None, seeds=None):
    """Run a COA script as an ensemble against reference branches.

    Returns a dict with the script name, the per-seed branch metrics,
    the reference ensemble summary, the branch ensemble summary and
    the contrast of means. The input world is not modified.
    """
    if days is None:
        days = int(script.get("days", 30))
    if seeds is None:
        seeds = script.get("seeds", [world.seed])
    reference_hash_before = world.state_hash()
    interventions = _interventions_by_day(script, world.day)
    params = script.get("parameters", {})
    branch_metrics = []
    reference_metrics = []
    for seed in seeds:
        branch = _run_branch(world, seed, days, interventions, params)
        branch_metrics.append(final_metrics(branch))
        reference = _run_branch(world, seed, days, {}, {})
        reference_metrics.append(final_metrics(reference))
    keys = sorted({k for m in branch_metrics for k in m})
    branch_summary = {}
    reference_summary = {}
    contrast = {}
    for key in keys:
        bvals = [m[key] for m in branch_metrics if key in m]
        rvals = [m[key] for m in reference_metrics if key in m]
        branch_summary[key] = _summarise(bvals)
        reference_summary[key] = _summarise(rvals)
        if bvals and rvals:
            contrast[key] = (statistics.fmean(bvals)
                             - statistics.fmean(rvals))
    return {
        "script": script.get("name", "unnamed"),
        "days": days,
        "seeds": [int(s) for s in seeds],
        "branch_metrics": branch_metrics,
        "reference_metrics": reference_metrics,
        "branch_summary": branch_summary,
        "reference_summary": reference_summary,
        "contrast": contrast,
        "reference_hash_before": reference_hash_before,
        "reference_hash_after": world.state_hash(),
        "note": ("Contrasts are differences between branched scenario "
                 "ensembles under stated assumptions. They are not "
                 "predictions of the real region."),
    }
