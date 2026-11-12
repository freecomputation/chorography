"""Chorography command line entry point.

Commands:
  init       Build a region into a fresh store.
  run        Advance the reference world by N days. With --live, fetch
             the manifest feeds into the inbox first.
  excursion  Run a course-of-action script as an ensemble and report
             the contrast against reference branches.
  calibrate  Score the recorded history, run the held-out check and a
             small parameter grid search.
  report     Print the text or JSON report for the stored world.
  verify     Replay the recorded run from the initial snapshot and
             compare state hashes.
  harvest    Run one scheduled harvest pass over the region source
             registry, or loop passes in the foreground with --loop.

A world lives in one sqlite store file. The initial snapshot and every
exogenous event applied on the way are recorded, which is what makes
verify possible.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from . import __version__
from .calibrate import grid_search, held_out_check, score_patterns
from .excursion import load_script, run_excursion
from .ingest import parse_inbox
from .report import build_report, render_json, render_text
from .store import Store, replay
from .world import build_world, load_effects

DEFAULT_GRID = {
    "morale_recovery": [0.0, 0.02, 0.05],
    "effect_scale": [0.5, 1.0, 1.5],
}


def _open_world(store):
    snap = store.latest_snapshot()
    if snap is None:
        raise SystemExit("store holds no world; run init first")
    from .world import World
    return World.from_state(snap["state"], load_effects())


def cmd_init(args):
    if os.path.exists(args.db) and not args.force:
        raise SystemExit(
            "store already exists at {}; pass --force to rebuild".format(
                args.db))
    if args.force and os.path.exists(args.db):
        os.remove(args.db)
    world = build_world(args.region, seed=args.seed)
    store = Store(args.db)
    try:
        store.set_meta("region_dir", os.path.abspath(args.region))
        store.set_meta("chorography_version", __version__)
        state = world.to_state()
        store.save_snapshot("initial", world.day, state, world.state_hash())
        store.save_snapshot("latest", world.day, state, world.state_hash())
        store.append_event("run_marker", world.day,
                           {"action": "init", "region": world.region_name})
    finally:
        store.close()
    print("Initialised {} at day {} ({}).".format(
        world.region_name, world.day, world.date.isoformat()))
    print("State hash {}".format(world.state_hash()))
    return 0


def cmd_run(args):
    store = Store(args.db)
    try:
        world = _open_world(store)
        region_dir = store.get_meta("region_dir")
        inbox = args.inbox
        if args.live:
            from . import feeds
            manifest_path = os.path.join(region_dir, "manifest.json")
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
            inbox = inbox or os.path.join(region_dir, "inbox-live")
            client = feeds.FeedClient(inbox)
            fetched_at = datetime.now(timezone.utc).isoformat()
            _paths, errors = feeds.fetch_all(
                client, manifest.get("feeds", []), fetched_at)
            for error in errors:
                print("feed outage tolerated: {}".format(error),
                      file=sys.stderr)
        pending = []
        if inbox:
            events, records = parse_inbox(
                inbox, world.date.isoformat())
            for record in records:
                store.append_event("ingest_record", world.day, record)
            fresh = _unseen_events(store, events)
            pending = fresh
        for i in range(args.days):
            todays = pending if i == 0 else []
            for event in todays:
                store.append_event("exogenous", world.day, event)
            world.tick(todays)
        state = world.to_state()
        store.save_snapshot("latest", world.day, state, world.state_hash())
        print("Advanced to day {} ({}). State hash {}".format(
            world.day, world.date.isoformat(), world.state_hash()))
    finally:
        store.close()
    return 0


def _unseen_events(store, events):
    seen = set()
    for record in store.events(kind="exogenous"):
        prov = record["payload"].get("provenance", {})
        if prov.get("raw_sha256"):
            seen.add((record["payload"].get("kind"),
                      prov["raw_sha256"]))
    fresh = []
    for event in events:
        prov = event.get("provenance", {})
        key = (event.get("kind"), prov.get("raw_sha256"))
        if key not in seen:
            fresh.append(event)
    return fresh


def cmd_excursion(args):
    store = Store(args.db)
    try:
        world = _open_world(store)
    finally:
        store.close()
    script = load_script(args.script)
    result = run_excursion(world, script)
    report = build_report(world, result)
    text = render_json(report) if args.json else render_text(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text, end="")
    return 0


def cmd_calibrate(args):
    store = Store(args.db)
    try:
        world = _open_world(store)
    finally:
        store.close()
    output = {
        "scorecard": score_patterns(world.history),
        "held_out": held_out_check(world.history),
        "grid_search": grid_search(world, DEFAULT_GRID, days=args.days),
    }
    print(json.dumps(output, indent=1, sort_keys=True))
    print("Calibration scores pattern fit only. Equifinality and model "
          "discrepancy apply; a fit is not a prediction.")
    return 0


def _harvest_health_for(db_path, region_dir):
    """Load the harvest state ledger beside the store, when present.

    The report carries a harvest health section only when a state
    file exists, so worlds that have never been harvested report
    exactly as they did before the harvester existed.
    """
    candidates = [db_path + ".harvest.json"]
    if region_dir:
        candidates.append(os.path.join(region_dir, "harvest-state.json"))
    for path in candidates:
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    state_data = json.load(fh)
            except ValueError:
                return None
            from .harvest import summarize_health
            return {
                "as_of": state_data.get("updated_at"),
                "sources": summarize_health(state_data),
            }
    return None


def cmd_report(args):
    store = Store(args.db)
    try:
        world = _open_world(store)
        region_dir = store.get_meta("region_dir")
    finally:
        store.close()
    health = _harvest_health_for(args.db, region_dir)
    report = build_report(world, harvest_health=health)
    print(render_json(report) if args.json else render_text(report),
          end="")
    return 0


def cmd_harvest(args):
    from .harvest import Harvester
    region = args.region
    inbox = args.inbox or os.path.join(region, "inbox-live")
    if args.state:
        state_path = args.state
    elif args.db:
        state_path = args.db + ".harvest.json"
    else:
        state_path = os.path.join(region, "harvest-state.json")
    if args.archive:
        archive_root = args.archive
    elif args.db:
        archive_root = args.db + ".harvest-archive"
    else:
        archive_root = os.path.join(region, "harvest-archive")
    harvester = Harvester(region, inbox, state_path, archive_root,
                          max_requests=args.max_requests,
                          max_bytes=args.max_bytes)
    if args.loop:
        try:
            harvester.run_loop()
        except KeyboardInterrupt:
            print("harvest loop stopped")
        return 0
    summary = harvester.run_once()
    if summary["locked"]:
        print("Harvest pass skipped: another pass holds the lock.")
        return 0
    print("Harvest pass: {} attempted, {} succeeded, {} failed, "
          "{} deferred, {} not due, {} disabled.".format(
              len(summary["attempted"]), len(summary["succeeded"]),
              len(summary["failed"]), len(summary["deferred"]),
              len(summary["skipped"]), len(summary["disabled"])))
    for failure in summary["failed"]:
        print("source {} failed: {}".format(failure["id"],
                                            failure["error"]),
              file=sys.stderr)
    return 0


def cmd_verify(args):
    store = Store(args.db)
    try:
        _world, replayed, stored = replay(store, load_effects())
    finally:
        store.close()
    print("Replayed hash {}".format(replayed))
    print("Stored hash   {}".format(stored))
    if replayed == stored:
        print("verify: PASS")
        return 0
    print("verify: FAIL")
    return 1


def build_parser():
    parser = argparse.ArgumentParser(
        prog="chorography",
        description="A continuously running synthetic mirror of a "
                    "region, in pure software.")
    parser.add_argument("--version", action="version",
                        version="chorography {}".format(__version__))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="build a region into a fresh store")
    p.add_argument("--region", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("run", help="advance the reference world")
    p.add_argument("--db", required=True)
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--inbox", default=None)
    p.add_argument("--live", action="store_true",
                   help="fetch manifest feeds into the inbox first")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("excursion", help="run a COA script ensemble")
    p.add_argument("--db", required=True)
    p.add_argument("--script", required=True)
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_excursion)

    p = sub.add_parser("calibrate", help="scorecard, held-out, grid search")
    p.add_argument("--db", required=True)
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("report", help="summarise the stored world")
    p.add_argument("--db", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("verify", help="replay and hash check")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("harvest", help="run scheduled harvest passes")
    p.add_argument("--region", required=True)
    p.add_argument("--db", default=None,
                   help="world store the state and archive sit beside")
    p.add_argument("--inbox", default=None)
    p.add_argument("--state", default=None)
    p.add_argument("--archive", default=None)
    p.add_argument("--once", action="store_true",
                   help="run a single pass and exit (the default)")
    p.add_argument("--loop", action="store_true",
                   help="run passes in the foreground (development)")
    p.add_argument("--max-requests", type=int, default=50)
    p.add_argument("--max-bytes", type=int, default=20000000)
    p.set_defaults(func=cmd_harvest)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
