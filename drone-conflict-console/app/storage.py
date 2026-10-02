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
from pathlib import Path

_LOCK = threading.Lock()

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
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def create_scenario(self, input_payload: dict, safety_radius: float,
                        safety_height: float, name: str = "") -> int:
        with _LOCK, self._conn() as c:
            cur = c.execute(
                "INSERT INTO scenarios(name,input_json,safety_radius,safety_height,created_at)"
                " VALUES (?,?,?,?,?)",
                (name, json.dumps(input_payload, ensure_ascii=False),
                 float(safety_radius), float(safety_height), time.time()),
            )
            return int(cur.lastrowid)

    def save_result(self, scenario_id: int, result: dict, safety_radius: float,
                    safety_height: float, name: str | None = None) -> None:
        with _LOCK, self._conn() as c:
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
        with _LOCK, self._conn() as c:
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
        with _LOCK, self._conn() as c:
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
        with _LOCK, self._conn() as c:
            cur = c.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            return cur.rowcount > 0

    def submit_report(self, batch_id: int, sortie_id: str,
                      actual_offset: int, revision: int) -> dict:
        """Atomically apply a revision-controlled report.

        The whole compare-and-set runs under the global store lock, so
        concurrent reports for one sortie resolve deterministically: the
        single stored row is the highest revision ever accepted.

        Returns ``(outcome, report)`` with outcome one of
        ``inserted`` / ``idempotent`` / ``invalid_first_revision`` /
        ``revision_value_conflict`` / ``stale_revision``.
        """
        with _LOCK, self._conn() as c:
            row = c.execute(
                "SELECT actual_offset,revision FROM batch_reports "
                "WHERE batch_id=? AND sortie_id=?",
                (batch_id, sortie_id),
            ).fetchone()
            now = time.time()
            if row is None:
                if revision != 1:
                    return "invalid_first_revision", None
                c.execute(
                    "INSERT INTO batch_reports(batch_id,sortie_id,actual_offset,"
                    "revision,reported_at) VALUES (?,?,?,?,?)",
                    (batch_id, sortie_id, int(actual_offset), int(revision), now),
                )
                return "inserted", {"sortie_id": sortie_id,
                                    "actual_offset": int(actual_offset),
                                    "revision": int(revision)}
            cur_off, cur_rev = row["actual_offset"], row["revision"]
            if revision == cur_rev:
                if actual_offset != cur_off:
                    return "revision_value_conflict", {
                        "stored_actual_offset": cur_off}
                return "idempotent", {"sortie_id": sortie_id,
                                      "actual_offset": cur_off,
                                      "revision": cur_rev}
            if revision < cur_rev:
                return "stale_revision", {"current_revision": cur_rev}
            c.execute(
                "UPDATE batch_reports SET actual_offset=?,revision=?,reported_at=? "
                "WHERE batch_id=? AND sortie_id=?",
                (int(actual_offset), int(revision), now, batch_id, sortie_id),
            )
            return "inserted", {"sortie_id": sortie_id,
                                "actual_offset": int(actual_offset),
                                "revision": int(revision)}
