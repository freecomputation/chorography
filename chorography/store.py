"""Event-sourced storage for Chorography, on sqlite3.

The store is append-only at the level of events. Two tables carry the
record: events holds every exogenous event and run marker with a
versioned schema, and snapshots holds full canonical world states.
Restart is snapshot plus deterministic replay: the recorded exogenous
events are re-applied to the initial snapshot by the tick loop, and the
replayed final state hash must equal the stored final snapshot hash.
That equality is what the verify command checks; a mismatch means the
record and the engine disagree, and the run must not be trusted.
"""

import json
import os
import sqlite3

EVENT_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    day INTEGER NOT NULL,
    kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    day INTEGER NOT NULL,
    label TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))
        self.conn.commit()

    def get_meta(self, key, default=None):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def append_event(self, kind, day, payload, schema_version=None):
        if schema_version is None:
            schema_version = EVENT_SCHEMA_VERSION
        if schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError(
                "unsupported event schema version {}".format(schema_version))
        cur = self.conn.execute(
            "INSERT INTO events(day, kind, schema_version, payload) "
            "VALUES(?, ?, ?, ?)",
            (int(day), kind, schema_version,
             json.dumps(payload, sort_keys=True)))
        self.conn.commit()
        return cur.lastrowid

    def events(self, kind=None):
        if kind is None:
            rows = self.conn.execute(
                "SELECT seq, day, kind, schema_version, payload FROM events "
                "ORDER BY seq").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT seq, day, kind, schema_version, payload FROM events "
                "WHERE kind=? ORDER BY seq", (kind,)).fetchall()
        out = []
        for seq, day, k, version, payload in rows:
            if version != EVENT_SCHEMA_VERSION:
                raise ValueError(
                    "stored event {} has unsupported schema version {}".format(
                        seq, version))
            out.append({
                "seq": seq,
                "day": day,
                "kind": k,
                "schema_version": version,
                "payload": json.loads(payload),
            })
        return out

    def save_snapshot(self, label, day, state, state_hash):
        cur = self.conn.execute(
            "INSERT INTO snapshots(day, label, state_hash, state) "
            "VALUES(?, ?, ?, ?)",
            (int(day), label, state_hash,
             json.dumps(state, sort_keys=True)))
        self.conn.commit()
        return cur.lastrowid

    def snapshot(self, label):
        row = self.conn.execute(
            "SELECT day, state_hash, state FROM snapshots WHERE label=? "
            "ORDER BY seq DESC LIMIT 1", (label,)).fetchone()
        if row is None:
            return None
        return {"day": row[0], "state_hash": row[1],
                "state": json.loads(row[2])}

    def latest_snapshot(self):
        row = self.conn.execute(
            "SELECT day, label, state_hash, state FROM snapshots "
            "ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"day": row[0], "label": row[1], "state_hash": row[2],
                "state": json.loads(row[3])}


def replay(store, effects):
    """Rebuild the final state from the initial snapshot and the record.

    Returns (world, replayed_hash, stored_hash). The caller compares the
    two hashes. Exogenous events recorded under kind exogenous, keyed by
    the absolute day on which they were applied, are re-applied in
    order; deterministic streams make the replay exact.
    """
    from .world import World
    initial = store.snapshot("initial")
    latest = store.latest_snapshot()
    if initial is None or latest is None:
        raise ValueError("store holds no snapshots to replay")
    world = World.from_state(initial["state"], effects)
    by_day = {}
    for record in store.events(kind="exogenous"):
        by_day.setdefault(record["day"], []).append(record["payload"])
    target_day = latest["state"]["day"]
    while world.day < target_day:
        world.tick(by_day.get(world.day, []))
    return world, world.state_hash(), latest["state_hash"]
