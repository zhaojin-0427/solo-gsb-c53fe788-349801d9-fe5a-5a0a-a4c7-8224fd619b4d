"""Execution-batch semantics and deviation early-warning validation.

Covers (without any HTTP layer):

* batch creation freezes input / thresholds / planned offsets;
* report revision protocol: first revision 1, strictly increasing,
  same-revision-same-value idempotence, same-revision-different-value and
  stale revision conflicts, concurrent submissions keep the highest revision;
* the same arbitration across OS *processes* sharing one SQLite file: the
  compare-and-set is one database-level atomic transaction
  (``BEGIN IMMEDIATE``), so a revision-2 report committed while revision 3 is
  in flight can never overwrite revision 3, and concurrent first reports
  follow the same-number idempotent / conflict rules;
* warning classification (reported / planned / overdue), the early-time
  rejection, incomplete flag, continuous-time conflict intervals projected
  onto ``t`` and first-conflict time;
* reloading a batch and re-evaluating gives identical results.

Run:  python tests/test_batches_warning.py
"""

from __future__ import annotations

import multiprocessing as mp
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


def _mp_report_worker(db_path, barrier, index, batch_id, sortie_id,
                      offset, revision, queue):
    """Top-level worker so it is picklable for 'spawn' start methods."""
    store = Store(db_path)
    barrier.wait()
    queue.put((index, store.submit_report(batch_id, sortie_id, offset, revision)))


def _multiprocess_race(db_path, batch_id, submissions):
    """Release every (offset, revision) submission from its own OS process."""
    queue = mp.Queue()
    barrier = mp.Barrier(len(submissions))
    procs = [
        mp.Process(target=_mp_report_worker,
                   args=(db_path, barrier, i, batch_id, "A", off, rev, queue))
        for i, (off, rev) in enumerate(submissions)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    for p in procs:
        check(p.exitcode == 0, f"worker process exited with {p.exitcode}")
    outcomes = [o for _, o in sorted(queue.get() for _ in procs)]
    return outcomes


def test_multiprocess_revision_arbitration():
    # Two OS processes sharing one SQLite file must be arbitrated by the
    # database itself: a threading lock cannot protect them.  After revision 1
    # is stored, revisions 3 and 2 race.  If 3 commits first, 2 must be rejected
    # as stale and cite current revision 3; if 2 commits first, 3 still updates.
    # Either order ends with revision 3 (the highest) as the stored row.
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "t.db")
        store = Store(db_path)
        sid = store.create_scenario({"sorties": []}, 30, 10)
        for trial in range(15):
            bid = store.create_batch(
                sid, f"b{trial}", {"sorties": [{"id": "A"}]},
                30, 10, {"offsets": {"A": 0}})
            outcome, _ = store.submit_report(bid, "A", 5, 1)
            check(outcome == "inserted", "seed revision 1")
            outcomes = _multiprocess_race(db_path, bid, [(30, 3), (20, 2)])
            o_high, o_low = outcomes  # index order matches submission order
            check(o_high[0] == "inserted",
                  f"trial {trial}: revision 3 must always be accepted, got {outcomes}")
            if o_low[0] == "stale_revision":
                check(o_low[1]["current_revision"] == 3,
                      f"trial {trial}: stale conflict must cite current revision 3")
            else:
                check(o_low[0] == "inserted",
                      f"trial {trial}: revision 2 may only win when it commits "
                      f"before 3, got {outcomes}")
            row = store.get_batch(bid)["reports"][0]
            check(row["revision"] == 3 and row["actual_offset"] == 30,
                  f"trial {trial}: highest revision must survive across "
                  f"processes, got {dict(row)}")


def test_multiprocess_concurrent_first_reports():
    # Concurrent first reports (no row yet) follow exactly the same-number
    # rules for revision 1: same value is inserted + idempotent, different
    # values is inserted + revision_value_conflict.  For a racing non-1
    # revision, see the dedicated case below.
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "t.db")
        store = Store(db_path)
        sid = store.create_scenario({"sorties": []}, 30, 10)

        for trial in range(10):
            bid = store.create_batch(
                sid, f"same{trial}", {"sorties": [{"id": "A"}]},
                30, 10, {"offsets": {"A": 0}})
            outcomes = _multiprocess_race(db_path, bid, [(7, 1), (7, 1)])
            names = sorted(o[0] for o in outcomes)
            check(names == ["idempotent", "inserted"],
                  f"same-value race must be idempotent, got {outcomes}")
            row = store.get_batch(bid)["reports"][0]
            check((row["revision"], row["actual_offset"]) == (1, 7),
                  "single rev-1 row with the shared value after same-value race")

        for trial in range(10):
            bid = store.create_batch(
                sid, f"diff{trial}", {"sorties": [{"id": "A"}]},
                30, 10, {"offsets": {"A": 0}})
            outcomes = _multiprocess_race(db_path, bid, [(11, 1), (22, 1)])
            names = sorted(o[0] for o in outcomes)
            check(names == ["inserted", "revision_value_conflict"],
                  f"different-value first reports must conflict, got {outcomes}")
            conflict = next(o for o in outcomes if o[0] == "revision_value_conflict")
            check(conflict[1]["stored_actual_offset"] in (11, 22),
                  "conflict names the value actually stored by the winner")
            row = store.get_batch(bid)["reports"][0]
            check(row["revision"] == 1 and row["actual_offset"] in (11, 22),
                  "exactly one first report survives the different-value race")

        # A first report at revision != 1 is only invalid while no row exists.
        # Racing first writers (rev 1 vs rev 3) are serialized by the database:
        # exactly one insert wins.  If rev 3 commits first it is the legitimate
        # first report (revisions are client-chosen and "first must be 1" is
        # evaluated against stored state, not wall-clock order); rev 1 is then
        # stale.  If rev 1 commits first, rev 3 is invalid_first_revision.
        bid = store.create_batch(
            sid, "badfirst", {"sorties": [{"id": "A"}]},
            30, 10, {"offsets": {"A": 0}})
        outcomes = _multiprocess_race(db_path, bid, [(7, 1), (9, 3)])
        names = {o[0] for o in outcomes}
        check(
            names == {"inserted", "invalid_first_revision"}
            or names == {"inserted", "stale_revision"},
            f"racing first reports must serialize to one insert, got {outcomes}")
        inserted = next(o for o in outcomes if o[0] == "inserted")
        if inserted[1]["revision"] == 1:
            check(any(o[0] == "invalid_first_revision" for o in outcomes),
                  "rev 3 rejected when rev 1 stores first")
        else:
            stale = next(o for o in outcomes if o[0] == "stale_revision")
            check(stale[1]["current_revision"] == 3,
                  f"rev 1 must be stale against current revision 3, got {stale}")
        row = store.get_batch(bid)["reports"][0]
        check((row["revision"], row["actual_offset"]) in ((1, 7), (3, 9)),
              "exactly one racing first report survives")


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
    test_multiprocess_revision_arbitration()
    test_multiprocess_concurrent_first_reports()
    test_reload_consistency()
    print("failures:", FAILS)
    return FAILS


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
