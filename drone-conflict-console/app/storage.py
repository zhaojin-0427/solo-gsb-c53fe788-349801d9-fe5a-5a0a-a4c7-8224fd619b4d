"""SQLite persistence for imported scenarios and saved plans.

A *scenario* keeps the exact imported JSON (input snapshot) together with the
safety parameters used for the most recent solve, so saved plans can always be
reopened and compared against their original input.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# Serializes writes inside one process; cross-process arbitration is handled
# by SQLite itself (WAL + a ``BEGIN IMMEDIATE`` write transaction).
_LOCK = threading.Lock()

#: How long SQLite waits on another process' write lock before raising
#: "database is locked"; the submit path additionally retries as a safety net.
_BUSY_TIMEOUT_MS = 5000
_BUSY_ATTEMPTS = 60
_BUSY_WAIT_SECONDS = 0.05

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT '',
    input_json      TEXT NOT NULL,
    safety_radius   REAL NOT NULL,
    safety_height   REAL NOT NULL,
    created_at      REAL NOT NULL,
    solved_at       REAL,
    result_json     TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id     INTEGER NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    name            TEXT NOT NULL DEFAULT '',
    input_json      TEXT NOT NULL,
    safety_radius   REAL NOT NULL,
    safety_height   REAL NOT NULL,
    plan_json       TEXT NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_reports (
    batch_id        INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    sortie_id       TEXT NOT NULL,
    actual_offset   INTEGER NOT NULL CHECK (actual_offset >= 0),
    revision        INTEGER NOT NULL CHECK (revision >= 1),
    reported_at     REAL NOT NULL,
    PRIMARY KEY (batch_id, sortie_id)
);
"""


class Store:
    def __init__(self, path: str | Path = "data/console.db"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _init_database(self):
        """Enable WAL and create the schema, tolerating a peer that is doing
        the same initialization concurrently.

        Switching journal mode (and, on a fresh file, creating the tables)
        needs an exclusive lock, so a second process booting at the same time
        can briefly hit ``database is locked``; retry until it succeeds or
        confirms the schema already exists.
        """
        for attempt in range(_BUSY_ATTEMPTS):
            with self._conn() as c:
                try:
                    # WAL lets multiple processes read while one writes and
                    # keeps short write transactions from colliding.
                    c.execute("PRAGMA journal_mode=WAL")
                    c.execute("PRAGMA synchronous=NORMAL")
                    c.executescript(_SCHEMA)
                    return
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or attempt == _BUSY_ATTEMPTS - 1:
                        raise
            time.sleep(_BUSY_WAIT_SECONDS)

    def _conn(self):
        conn = sqlite3.connect(
            self.path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    @contextmanager
    def _write_tx(self):
        """A single ``BEGIN IMMEDIATE`` write transaction.

        This acquires SQLite's reserved lock up front, blocks on a writer that
        already holds it via ``busy_timeout``, and groups the conditional
        write + authoritative re-read in one commit.  It does *not* by itself
        make a read-then-write sequence safe across processes (WAL snapshots
        differ), so callers must make each write conditional in SQL.
        """
        with _LOCK, self._conn() as c:
            for attempt in range(_BUSY_ATTEMPTS):
                try:
                    c.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) or attempt == _BUSY_ATTEMPTS - 1:
                        raise
                    time.sleep(_BUSY_WAIT_SECONDS)
            try:
                yield c
            except Exception:
                c.rollback()
                raise
            else:
                c.commit()

    def create_scenario(self, input_payload: dict, safety_radius: float,
                        safety_height: float, name: str = "") -> int:
        with self._write_tx() as c:
            cur = c.execute(
                "INSERT INTO scenarios(name,input_json,safety_radius,safety_height,created_at)"
                " VALUES (?,?,?,?,?)",
                (name, json.dumps(input_payload, ensure_ascii=False),
                 float(safety_radius), float(safety_height), time.time()),
            )
            return int(cur.lastrowid)

    def save_result(self, scenario_id: int, result: dict, safety_radius: float,
                    safety_height: float, name: str | None = None) -> None:
        with self._write_tx() as c:
            if name is not None:
                c.execute("UPDATE scenarios SET name=? WHERE id=?", (name, scenario_id))
            c.execute(
                "UPDATE scenarios SET result_json=?, solved_at=?, safety_radius=?, safety_height=?"
                " WHERE id=?",
                (json.dumps(result, ensure_ascii=False), time.time(),
                 float(safety_radius), float(safety_height), scenario_id),
            )

    def list_scenarios(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id,name,safety_radius,safety_height,created_at,solved_at,"
                "CASE WHEN result_json IS NOT NULL THEN 1 ELSE 0 END AS has_result "
                "FROM scenarios ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_scenario(self, scenario_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["input"] = json.loads(d.pop("input_json"))
        d["result"] = json.loads(d["result_json"]) if d.get("result_json") else None
        return d

    def delete_scenario(self, scenario_id: int) -> bool:
        with self._write_tx() as c:
            # Remove dependent batches (and their reports) explicitly so the
            # cascade works even on databases created before the FK cascade.
            c.execute("DELETE FROM batch_reports WHERE batch_id IN "
                      "(SELECT id FROM batches WHERE scenario_id=?)",
                      (scenario_id,))
            c.execute("DELETE FROM batches WHERE scenario_id=?", (scenario_id,))
            cur = c.execute("DELETE FROM scenarios WHERE id=?", (scenario_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Execution batches (frozen plan) and actual-take-off reports
    # ------------------------------------------------------------------

    def create_batch(self, scenario_id: int, name: str, input_payload: dict,
                     safety_radius: float, safety_height: float,
                     plan: dict) -> int:
        with self._write_tx() as c:
            cur = c.execute(
                "INSERT INTO batches(scenario_id,name,input_json,safety_radius,"
                "safety_height,plan_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (scenario_id, name, json.dumps(input_payload, ensure_ascii=False),
                 float(safety_radius), float(safety_height),
                 json.dumps(plan, ensure_ascii=False), time.time()),
            )
            return int(cur.lastrowid)

    def list_batches(self, scenario_id: int | None = None) -> list[dict]:
        sql = ("SELECT b.id,b.scenario_id,b.name,b.safety_radius,b.safety_height,"
               "b.created_at, COUNT(r.sortie_id) AS reported_count "
               "FROM batches b LEFT JOIN batch_reports r "
               "ON r.batch_id = b.id ")
        args: tuple = ()
        if scenario_id is not None:
            sql += "WHERE b.scenario_id=? "
            args = (scenario_id,)
        sql += "GROUP BY b.id ORDER BY b.id DESC"
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def get_batch(self, batch_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["input"] = json.loads(d.pop("input_json"))
            d["plan"] = json.loads(d.pop("plan_json"))
            reports = c.execute(
                "SELECT sortie_id,actual_offset,revision,reported_at "
                "FROM batch_reports WHERE batch_id=? ORDER BY sortie_id",
                (batch_id,),
            ).fetchall()
        d["reports"] = [dict(r) for r in reports]
        return d

    def delete_batch(self, batch_id: int) -> bool:
        with self._write_tx() as c:
            cur = c.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            return cur.rowcount > 0

    def submit_report(self, batch_id: int, sortie_id: str,
                      actual_offset: int, revision: int) -> tuple[str, dict | None]:
        """Atomically apply a revision-controlled report.

        The decision is enforced by the database itself rather than by a
        read-then-write sequence, so it stays correct when several *processes*
        share the SQLite file.  Inside one ``BEGIN IMMEDIATE`` transaction two
        guarded statements are issued:

        1. an ``INSERT ... SELECT ... WHERE NOT EXISTS(...) AND revision = 1``
           that creates the row only for a first report at revision 1;
        2. an ``UPDATE ... WHERE incoming_revision > stored_revision`` that
           replaces the row only for a strictly higher revision.

        Because every write is conditional on the row committed at write
        time, a lower revision can never overwrite a higher one and a
        same-revision write can never change an existing value, no matter how
        overlapping transactions commit.  The row is then re-read inside the
        same transaction to classify the outcome against the authoritative
        state (so a loser reports the winner's ``current_revision``):

        * no row + revision 1          -> ``inserted``
        * no row + revision != 1       -> ``invalid_first_revision``
        * revision == stored revision  -> same value ``idempotent`` /
          different value ``revision_value_conflict``
        * revision  < stored revision  -> ``stale_revision``
        * revision  > stored revision  -> ``inserted``

        Returns ``(outcome, info)``.
        """
        with self._write_tx() as c:
            now = time.time()
            # 1) First report only: create the row when none exists AND the
            #    incoming revision is 1.
            cur = c.execute(
                "INSERT INTO batch_reports(batch_id,sortie_id,actual_offset,"
                "revision,reported_at) "
                "SELECT ?,?,?,?,? WHERE ? = 1 AND NOT EXISTS ("
                "SELECT 1 FROM batch_reports WHERE batch_id=? AND sortie_id=?)",
                (batch_id, sortie_id, int(actual_offset), int(revision), now,
                 int(revision), batch_id, sortie_id),
            )
            wrote = cur.rowcount > 0
            # 2) Higher revision only: replace an existing strictly-lower row.
            if not wrote:
                cur = c.execute(
                    "UPDATE batch_reports SET actual_offset=?,revision=?,"
                    "reported_at=? WHERE batch_id=? AND sortie_id=? AND revision < ?",
                    (int(actual_offset), int(revision), now,
                     batch_id, sortie_id, int(revision)),
                )
                wrote = cur.rowcount > 0
            if wrote:
                return "inserted", {"sortie_id": sortie_id,
                                    "actual_offset": int(actual_offset),
                                    "revision": int(revision)}
            row = c.execute(
                "SELECT actual_offset,revision FROM batch_reports "
                "WHERE batch_id=? AND sortie_id=?",
                (batch_id, sortie_id),
            ).fetchone()
            if row is None:
                # No row exists, so the only legal write is a first report at
                # revision 1; anything else is rejected unchanged.
                return "invalid_first_revision", None
            cur_off, cur_rev = row["actual_offset"], row["revision"]
            if revision == cur_rev:
                if actual_offset != cur_off:
                    return "revision_value_conflict", {
                        "stored_actual_offset": cur_off}
                return "idempotent", {"sortie_id": sortie_id,
                                      "actual_offset": cur_off,
                                      "revision": cur_rev}
            return "stale_revision", {"current_revision": cur_rev}
