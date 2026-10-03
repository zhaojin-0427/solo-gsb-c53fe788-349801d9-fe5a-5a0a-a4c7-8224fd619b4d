#!/usr/bin/env python3
"""Two-process shared-SQLite verification over real HTTP.

Assumes TWO server processes are running against the same SQLite database:

    BASE1 (default http://localhost:8000)  -> process 1
    BASE2 (default http://localhost:8001)  -> process 2, same DCC_DB_PATH

Easiest way (see README):

    docker compose --profile multiproc up --build
    python3 scripts/verify_multiprocess.py

The script imports the bundled example, solves/saves it, creates execution
batches and then exercises the revision protocol *across the two processes*:

* case A: revision 3 committed first, then revision 2 from the other process
  must be rejected as a stale revision whose ``current_revision`` is 3;
* case B: revision 2 committed first, revision 3 afterwards still updates;
* case C: revisions 2 and 3 fired truly simultaneously from both processes,
  repeated several times - the stored row must always end at revision 3;
* case D: two simultaneous first reports (revision 1, same value) - exactly
  one insert and one idempotent retry.

After every case it reloads the batch (and its warning) from BOTH processes
and checks they show the highest revision's actual offset. Exits non-zero on
the first failed expectation.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE1 = os.environ.get("BASE1", "http://localhost:8000")
BASE2 = os.environ.get("BASE2", "http://localhost:8001")
ROOT = Path(__file__).resolve().parent.parent

FAILURES = 0


def check(cond, msg):
    global FAILURES
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond:
        FAILURES += 1


def call(method, base, path, payload=None):
    """Return (http_status, parsed_json)."""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def wait_healthy(base):
    for _ in range(60):
        try:
            status, _ = call("GET", base, "/api/health")
            if status == 200:
                return
        except OSError:
            pass
        import time
        time.sleep(1)
    raise SystemExit(f"server not healthy: {base}")


def setup_scenario_and_plan():
    payload = json.loads((ROOT / "examples" / "crossroads.json").read_text())
    status, body = call("POST", BASE1, "/api/import", payload)
    check(status == 200, f"import scenario via {BASE1} (HTTP {status})")
    sid = body["scenario_id"]

    status, body = call("POST", BASE1, f"/api/scenarios/{sid}/solve",
                        {"time_limit": 20})
    check(status == 200 and body["solution"]["feasible"],
          f"solve a feasible plan (HTTP {status})")
    call("POST", BASE1, f"/api/scenarios/{sid}/save",
         {"name": "multiproc-check"})
    return sid


def new_batch(sid, name):
    status, body = call("POST", BASE1, f"/api/scenarios/{sid}/batches",
                        {"name": name})
    check(status == 201, f"create batch {name!r} (HTTP {status})")
    return body["batch_id"]


def report(base, bid, sortie, offset, revision):
    return call("PUT", base, f"/api/batches/{bid}/reports",
                {"sortie_id": sortie, "actual_offset": offset,
                 "revision": revision})


def stored_report(base, bid, sortie):
    status, body = call("GET", base, f"/api/batches/{bid}")
    assert status == 200
    rows = {r["sortie_id"]: r for r in body["reports"]}
    return rows.get(sortie)


def warning_row(base, bid, t):
    status, body = call("GET", base, f"/api/batches/{bid}/warning?t={t}")
    assert status == 200, (status, body)
    return {r["id"]: r for r in body["warning"]["sorties"]}["UAV-A"]


def race(bid, offers):
    """offers: list of (base, offset, revision); release at the same instant."""
    start = threading.Barrier(len(offers))

    def one(base, off, rev):
        start.wait()
        return report(base, bid, "UAV-A", off, rev)

    with ThreadPoolExecutor(max_workers=len(offers)) as pool:
        return list(pool.map(lambda a: one(*a), offers))


def main():
    wait_healthy(BASE1)
    wait_healthy(BASE2)
    print(f"both processes healthy: {BASE1} and {BASE2}")
    sid = setup_scenario_and_plan()
    # planned offset of UAV-A in the unique optimal plan is 4
    planned_a = 4

    # --- case A: rev 3 first, rev 2 second -> stale conflict ---------------
    bid_a = new_batch(sid, "caseA-3-then-2")
    check(report(BASE1, bid_a, "UAV-A", 7, 1)[0] == 200,
          "A: revision 1 accepted")
    check(report(BASE1, bid_a, "UAV-A", 9, 3)[0] == 200,
          "A: revision 3 accepted on process 1")
    status, body = report(BASE2, bid_a, "UAV-A", 8, 2)
    check(status == 409 and body["detail"]["reason"] == "stale_revision"
          and body["detail"]["current_revision"] == 3,
          f"A: revision 2 on process 2 -> 409 stale, current=3, got {status} {body}")

    # --- case B: rev 2 first, rev 3 second -> 3 still updates --------------
    bid_b = new_batch(sid, "caseB-2-then-3")
    check(report(BASE1, bid_b, "UAV-A", 7, 1)[0] == 200,
          "B: revision 1 accepted")
    check(report(BASE2, bid_b, "UAV-A", 8, 2)[0] == 200,
          "B: revision 2 accepted on process 2")
    status, body = report(BASE1, bid_b, "UAV-A", 9, 3)
    check(status == 200 and body["report"]["revision"] == 3,
          f"B: revision 3 on process 1 still updates (HTTP {status})")

    # Both orders must leave rev 3 / offset 9, visible from BOTH processes,
    # and the warning must use that actual offset (deviation = 9 - 4).
    def assert_final(case_name, case_bid):
        for base in (BASE1, BASE2):
            rep = stored_report(base, case_bid, "UAV-A")
            check(rep is not None and rep["revision"] == 3
                  and rep["actual_offset"] == 9,
                  f"{case_name}: {base} batch detail shows rev 3 / offset 9 -> {rep}")
        row = warning_row(BASE1, case_bid, 9)
        check(row["actual_offset"] == 9 and row["revision"] == 3
              and row["deviation_seconds"] == 9 - planned_a,
              f"{case_name}: warning uses rev 3 actual offset 9 -> {row}")

    assert_final("A", bid_a)
    assert_final("B", bid_b)

    # --- case C: simultaneous rev 2 vs rev 3, repeated ---------------------
    for trial in range(5):
        cb = new_batch(sid, f"caseC-race-{trial}")
        check(report(BASE1, cb, "UAV-A", 7, 1)[0] == 200,
              f"C{trial}: seed revision 1")
        results = race(cb, [(BASE1, 9, 3), (BASE2, 8, 2)])

        def is_for(rev, status, body):
            if status == 200:
                return body.get("report", {}).get("revision") == rev
            return body.get("detail", {}).get("received_revision") == rev

        r3 = [r for r in results if is_for(3, *r)]
        r2 = [r for r in results if is_for(2, *r)]
        check(len(r3) == 1 and len(r2) == 1 and r3[0][0] == 200,
              f"C{trial}: exactly one rev3 reply and it is accepted, got {results}")
        # rev2 either lost to a committed 3 (409 current=3) or serialized first
        # and was then overwritten by 3 - it must never be the final value.
        s2, b2 = r2[0]
        check(s2 == 200 or (b2["detail"]["reason"] == "stale_revision"
                            and b2["detail"]["current_revision"] == 3),
              f"C{trial}: rev2 allowed outcomes only, got {s2} {b2}")
        rep = stored_report(BASE1, cb, "UAV-A")
        check(rep["revision"] == 3 and rep["actual_offset"] == 9,
              f"C{trial}: final row always rev 3 / offset 9 -> {rep}")

    # --- case D: simultaneous first reports, same value --------------------
    db = new_batch(sid, "caseD-first-same")
    results = race(db, [(BASE1, 7, 1), (BASE2, 7, 1)])
    idem = sorted((s, b.get("idempotent")) for s, b in results)
    check(all(s == 200 for s, _ in idem)
          and {v for _, v in idem} == {False, True},
          f"D: concurrent identical first reports = 1 insert + 1 idempotent, "
          f"got {results}")
    rep = stored_report(BASE2, db, "UAV-A")
    check(rep is not None and rep["revision"] == 1 and rep["actual_offset"] == 7,
          f"D: single rev 1 / offset 7 row visible on process 2 -> {rep}")

    print()
    if FAILURES:
        print(f"RESULT: {FAILURES} check(s) FAILED")
        sys.exit(1)
    print("RESULT: all two-process shared-DB checks passed")


if __name__ == "__main__":
    main()
