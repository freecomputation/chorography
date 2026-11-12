"""Reports: plain-text and JSON summaries of a world and its excursions.

Reports are deliberately plain. A text report carries the reference
world summary, a PMESII dashboard as fixed-width text and, when an
excursion result is supplied, the ensemble contrasts. The JSON form
carries the same content for other programs. Both forms repeat the
standing caution that excursion contrasts are scenario differences
under stated assumptions, not predictions.
"""

import json

from .world import PMESII_CATEGORIES

CAUTION = ("Outputs are scenario contrasts under stated assumptions, "
           "never predictions of the real region.")


def build_report(world, excursion_result=None):
    state = world.to_state()
    report = {
        "region": world.region_name,
        "demonstration_region": world.demonstration,
        "day": world.day,
        "date": state["date"],
        "seed": world.seed,
        "state_hash": world.state_hash(),
        "household_composites": len(world.households),
        "people_represented": round(
            sum(h.weight * h.size for h in world.households), 2),
        "institutions": len(world.institutions),
        "infrastructure_nodes": len(world.infrastructure),
        "environment": dict(world.env),
        "caution": CAUTION,
    }
    if world.history:
        report["latest"] = world.history[-1]
    dashboard = {}
    for tract in world.tract_ids():
        dashboard[tract] = {}
        for category in PMESII_CATEGORIES:
            metrics = world.geographies[tract]["pmesii"][category]
            dashboard[tract][category] = {
                metric: round(sc["index"], 4)
                for metric, sc in sorted(metrics.items())
            }
    report["pmesii_dashboard"] = dashboard
    if excursion_result is not None:
        report["excursion"] = {
            "script": excursion_result["script"],
            "days": excursion_result["days"],
            "seeds": excursion_result["seeds"],
            "contrast": excursion_result["contrast"],
            "branch_summary": excursion_result["branch_summary"],
            "reference_summary": excursion_result["reference_summary"],
        }
    return report


def render_text(report):
    lines = []
    lines.append("Chorography report: {}".format(report["region"]))
    if report.get("demonstration_region"):
        lines.append("Demonstration region: all aggregate numbers are "
                     "invented sample data, not census figures.")
    lines.append("Day {} on {}. Seed {}. State hash {}.".format(
        report["day"], report["date"], report["seed"],
        report["state_hash"]))
    lines.append(
        "Household composites: {}. People represented: {}. "
        "Institutions: {}. Infrastructure nodes: {}.".format(
            report["household_composites"], report["people_represented"],
            report["institutions"], report["infrastructure_nodes"]))
    env = report["environment"]
    lines.append("Environment: hazard {} ({}) , precipitation index "
                 "{:.3f}, temperature anomaly {:.2f}.".format(
                     round(env["hazard"], 3), env["hazard_kind"],
                     env["precip_index"], env["temp_anomaly"]))
    latest = report.get("latest")
    if latest:
        lines.append(
            "Latest day: morale {:.3f}, health {:.3f}, employment "
            "{:.3f}, mean outage {:.3f}, mean condition {:.3f}.".format(
                latest.get("morale_mean", 0.0), latest.get("health_mean", 0.0),
                latest.get("employment_mean", 0.0),
                latest.get("outage_mean", 0.0),
                latest.get("condition_mean", 0.0)))
    lines.append("")
    lines.append("PMESII dashboard (indices, 0 to 1)")
    for tract in sorted(report["pmesii_dashboard"].keys()):
        lines.append("Tract {}".format(tract))
        for category in PMESII_CATEGORIES:
            metrics = report["pmesii_dashboard"][tract][category]
            rendered = "  ".join("{} {:.3f}".format(m, v)
                                  for m, v in metrics.items())
            lines.append("  {:<14} {}".format(category, rendered))
    excursion = report.get("excursion")
    if excursion:
        lines.append("")
        lines.append("Excursion {} over {} days, seeds {}".format(
            excursion["script"], excursion["days"], excursion["seeds"]))
        for metric in sorted(excursion["contrast"].keys()):
            lines.append("  contrast {:<22} {:+.4f}".format(
                metric, excursion["contrast"][metric]))
    lines.append("")
    lines.append(report["caution"])
    return "\n".join(lines) + "\n"


def render_json(report):
    return json.dumps(report, indent=1, sort_keys=True) + "\n"
