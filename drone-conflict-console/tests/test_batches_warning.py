"""Execution-batch semantics and deviation early-warning validation.

Covers (without any HTTP layer):

* batch creation freezes input / thresholds / planned offsets;
* report revision protocol: first revision 1, strictly increasing,
  same-revision-same-value idempotence, same-revision-different-value and
  stale revision conflicts, concurrent submissions keep the highest revision;
* warning classification (reported / planned / overdue), the early-time
  rejection, incomplete flag, continuous-time conflict intervals projected
  onto ``t`` and first-conflict time;
* reloading a batch and re-evaluating gives identical results.

Run:  python tests/test_batches_warning.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.batch import EarlyTimeReject, Report, evaluate_warning  # noqa: E402
from app.model import Waypoint, make_sortie  # noqa: E402
from app.storage import Store  # noqa: E402

FAILS = 0


def check(cond, msg):
    global FAILS
    if not cond:
        FAILS += 1
        print("FAIL:", msg)


def two_crossing_sorties():
    """A eastbound, B northbound, crossing at (100, 0), same altitude."""
    a = make_sortie("A", 1, 20, [
        Waypoint(0, 0, 20, 0), Waypoint(100, 0, 20, 10), Waypoint(200, 0, 20, 20)])
    b = make_sortie("B", 1, 20, [
        Waypoint(100, -100, 20, 0), Waypoint(100, 100, 20, 20)])
    return [a, b]


def test_evaluation():
    sorties = two_crossing_sorties()
    plan = {"A": 0, "B": 10}  # staggered plan, conflict free
    w = evaluate_warning(sorties, plan, {}, 30, 10, 0)
    check(w["complete"] and not w["conflicts"], "plan itself should be conflict free")
    check(all(r["status"] == "planned" for r in w["sorties"]), "all planned at t=0")

    # B deviates to actual 0 (revision 1): simultaneous crossing conflict.
    reports = {"A": Report(0, 1), "B": Report(0, 1)}
    w = evaluate_warning(sorties, plan, reports, 30, 10, 0)
    check(len(w["conflicts"]) == 1, "expected one conflict interval after deviation")
    c = w["conflicts"][0]
    check(c["time_start"] < 10 < c["time_end"], "crossing instant inside interval")
    check(w["first_conflict_time"] == c["time_start"], "first conflict time matches")
    dev = {d["sortie_id"]: d["deviation_seconds"] for d in w["reported_deviations"]}
    check(dev == {"A": 0, "B": -10}, f"deviations wrong: {dev}")

    # Projection onto t clips the interval start and moves the witness.
    w8 = evaluate_warning(sorties, plan, reports, 30, 10, 8)
    c8 = w8["conflicts"][0]
    check(abs(c8["time_start"] - 8) < 1e-9, "interval start clipped at t")
    check(c8["time_end"] == c["time_end"], "interval end preserved")
    check(abs(c8["witness_time"] - 0.5 * (8 + c["time_end"])) < 1e-6,
          "witness recomputed at midpoint of clipped interval")
    check(abs(w8["first_conflict_time"] - 8) < 1e-9, "first conflict clipped to t")

    # After the interval ends there are no conflicts.
    check(not evaluate_warning(sorties, plan, reports, 30, 10, 13)["conflicts"],
          "no conflicts past the interval end")

    # t earlier than a reported actual take-off is rejected.
    try:
        evaluate_warning(sorties, plan,
                         {"A": Report(0, 1), "B": Report(9, 1)}, 30, 10, 3)
        check(False, "expected EarlyTimeReject")
    except EarlyTimeReject as exc:
        check([v["sortie_id"] for v in exc.violations] == ["B"],
              "violation names the future-reporting sortie")

    # Unreported with planned take-off == t is still planned; earlier is overdue.
    w10 = evaluate_warning(sorties, plan, {"A": Report(0, 1)}, 30, 10, 10)
    check({r["id"]: r["status"] for r in w10["sorties"]} ==
          {"A": "reported", "B": "planned"}, "take-off exactly at t counts as planned")
    check(w10["complete"], "prediction complete when nothing is overdue")
    w15 = evaluate_warning(sorties, plan, {}, 30, 10, 15)
    check(set(w15["overdue_sorties"]) == {"A", "B"}, "both overdue at t=15")
    check(w15["incomplete"] and not w15["conflicts"],
          "overdue excluded from prediction and result incomplete")


def test_store_revision_protocol():
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        sid = store.create_scenario({"sorties": []}, 30, 10)
        bid = store.create_batch(sid, "b", {"sorties": [{"id": "A"}, {"id": "B"}]},
                                 30, 10, {"offsets": {"A": 1, "B": 2}})

        outcome, _ = store.submit_report(bid, "A", 5, 2)
        check(outcome == "invalid_first_revision", "first report revision must be 1")
        outcome, rep = store.submit_report(bid, "A", 5, 1)
        check(outcome == "inserted" and rep["actual_offset"] == 5, "first insert")
        outcome, rep = store.submit_report(bid, "A", 5, 1)
        check(outcome == "idempotent", "same revision same value idempotent")
        outcome, info = store.submit_report(bid, "A", 6, 1)
        check(outcome == "revision_value_conflict" and info["stored_actual_offset"] == 5,
              "same revision different value conflicts")
        outcome, _ = store.submit_report(bid, "A", 7, 2)
        check(outcome == "inserted", "strictly higher revision accepted")
        outcome, info = store.submit_report(bid, "A", 5, 1)
        check(outcome == "stale_revision" and info["current_revision"] == 2,
              "older revision after a newer one is stale")
        row = store.get_batch(bid)
        check(len(row["reports"]) == 1 and row["reports"][0]["revision"] == 2
              and row["reports"][0]["actual_offset"] == 7,
              "only the highest revision row survives")

        # Freeze: batch stores its own snapshot and plan.
        check(row["plan"]["offsets"] == {"A": 1, "B": 2}, "planned offsets frozen")
        check(row["safety_radius"] == 30 and row["safety_height"] == 10,
              "safety thresholds frozen")


def test_concurrent_keeps_highest():
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        sid = store.create_scenario({"sorties": []}, 30, 10)
        for trial in range(20):
            bid = store.create_batch(sid, f"b{trial}", {"sorties": [{"id": "A"}]},
                                     30, 10, {"offsets": {"A": 0}})
            outcomes = []
            lock = threading.Lock()

            def put(off, rev):
                o, _ = store.submit_report(bid, "A", off, rev)
                with lock:
                    outcomes.append((rev, o))

            ts = [threading.Thread(target=put, args=(10 * r, r))
                  for r in (1, 2, 3, 4, 5, 6, 7, 8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            row = store.get_batch(bid)
            check(len(row["reports"]) == 1, "single report row per sortie")
            check(row["reports"][0]["revision"] == 8
                  and row["reports"][0]["actual_offset"] == 80,
                  f"trial {trial}: highest revision must win, got {outcomes}")


def _mp_worker(path, bid, off, rev, barrier, queue):
    """Open the shared database in a *fresh* process and race one report."""
    store = Store(path)
    barrier.wait()
    outcome, info = store.submit_report(bid, "A", off, rev)
    queue.put((rev, outcome, info))


def test_multiprocess_shared_db():
    """Two OS processes sharing one SQLite file arbitrate at database level.

    With revision 1 stored, rev 3 and rev 2 are submitted simultaneously from
    two processes.  Whichever transaction wins, the final row must be the
    highest revision (3) and the loser must see its current revision.
    Concurrent first reports (both rev 1) follow the same-revision rules.
    """
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        setup = Store(path)
        sid = setup.create_scenario({"sorties": []}, 30, 10)

        def race(bid, offers):
            """offers: list of (revision, actual_offset) for racing processes."""
            barrier = ctx.Barrier(2)
            queue = ctx.Queue()
            procs = [
                ctx.Process(target=_mp_worker,
                            args=(path, bid, off, rev, barrier, queue))
                for rev, off in offers
            ]
            for p in procs:
                p.start()
            results = [queue.get(timeout=30) for _ in procs]
            for p in procs:
                p.join(10)
                check(p.exitcode == 0, "worker process finished cleanly")
            return results

        # --- revision 3 vs 2 on top of an existing revision 1 ---------------
        for trial in range(10):
            bid = setup.create_batch(
                sid, f"race{trial}", {"sorties": [{"id": "A"}]},
                30, 10, {"offsets": {"A": 0}})
            outcome, _ = setup.submit_report(bid, "A", 10, 1)
            check(outcome == "inserted", "seed revision 1")
            results = race(bid, [(3, 30), (2, 20)])
            outcomes = {rev: (o, info) for rev, o, info in results}
            # Revision 3 is always accepted and the stored row is always 3/30.
            check(outcomes[3][0] == "inserted",
                  f"trial {trial}: revision 3 accepted, got {outcomes}")
            # Revision 2 may either lose outright to the committed 3 ...
            if outcomes[2][0] == "stale_revision":
                check(outcomes[2][1]["current_revision"] == 3,
                      f"trial {trial}: stale rev 2 reports current=3, got {outcomes}")
            # ... or serialize first (2 written over 1), in which case rev 3
            # then overwrites it; the invariant is that 2 can never be final.
            else:
                check(outcomes[2][0] == "inserted",
                      f"trial {trial}: rev 2 only allowed to serialize first, "
                      f"got {outcomes}")
            row = setup.get_batch(bid)
            check(len(row["reports"]) == 1
                  and row["reports"][0]["revision"] == 3
                  and row["reports"][0]["actual_offset"] == 30,
                  f"trial {trial}: stored row is the highest revision (3/30)")

        # --- concurrent first reports, same revision 1 ----------------------
        # Same value: one insert + one idempotent, exactly one row.
        bid = setup.create_batch(
            sid, "first-same", {"sorties": [{"id": "A"}]},
            30, 10, {"offsets": {"A": 0}})
        results = race(bid, [(1, 5), (1, 5)])
        # Both carry revision 1; distinguish by outcome only.
        names = sorted(o for _, o, _ in results)
        check(names == ["idempotent", "inserted"],
              f"concurrent identical first reports: {results}")
        row = setup.get_batch(bid)
        check(len(row["reports"]) == 1 and row["reports"][0]["actual_offset"] == 5,
              "single row after concurrent identical first reports")

        # Different values: one insert + one same-revision value conflict.
        bid = setup.create_batch(
            sid, "first-diff", {"sorties": [{"id": "A"}]},
            30, 10, {"offsets": {"A": 0}})
        results = race(bid, [(1, 5), (1, 6)])
        names = sorted(o for _, o, _ in results)
        check(names == ["inserted", "revision_value_conflict"],
              f"concurrent clashing first reports: {results}")
        row = setup.get_batch(bid)
        check(len(row["reports"]) == 1 and row["reports"][0]["revision"] == 1,
              "single row after concurrent clashing first reports")


def test_reload_consistency():
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        store.create_scenario({"sorties": []}, 30, 10)
        bid = store.create_batch(1, "b", {"sorties": []}, 30, 10,
                                 {"offsets": {"A": 0, "B": 10}})
        store.submit_report(bid, "A", 0, 1)
        store.submit_report(bid, "B", 0, 1)
        sorties = two_crossing_sorties()
        reports1 = {r["sortie_id"]: Report(r["actual_offset"], r["revision"])
                    for r in store.get_batch(bid)["reports"]}
        w1 = evaluate_warning(sorties, {"A": 0, "B": 10}, reports1, 30, 10, 0)
        # Simulate a later reload from persistence.
        reports2 = {r["sortie_id"]: Report(r["actual_offset"], r["revision"])
                    for r in store.get_batch(bid)["reports"]}
        w2 = evaluate_warning(sorties, {"A": 0, "B": 10}, reports2, 30, 10, 0)
        check(w1 == w2, "reloaded batch evaluates identically")


def main():
    test_evaluation()
    test_store_revision_protocol()
    test_concurrent_keeps_highest()
    test_multiprocess_shared_db()
    test_reload_consistency()
    print("failures:", FAILS)
    return FAILS


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
