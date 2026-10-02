"""Execution batches and deviation early-warning evaluation.

A *batch* freezes one saved feasible plan: the input trajectories (snapshot),
the safety thresholds (R, H) and the planned take-off offsets.  Batch state
then only changes through per-sortie actual-take-off reports carrying a
strictly increasing revision number.

Given a non-negative current time ``t`` the warning evaluation classifies
every sortie:

* **reported** — an actual take-off offset has been reported; it flies at the
  reported time;
* **planned** — not reported yet, but its planned take-off is not earlier
  than ``t``: it is still predictable and flies at the planned time;
* **overdue** — not reported and its planned take-off is already in the past
  (strictly earlier than ``t``): excluded from prediction and the result is
  flagged incomplete.

Conflicts reuse the continuous-time detector in :mod:`app.geometry` on the
effective (reported / planned) schedule and are reported for instants
``>= t``.  The evaluation is a pure function of the frozen batch and the
stored reports, so reloading a batch and re-evaluating always gives the same
result.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import EPS, conflicts_between_sorties

REPORTED = "reported"
PLANNED = "planned"
OVERDUE = "overdue"


@dataclass(frozen=True)
class Report:
    actual_offset: int
    revision: int


class EarlyTimeReject(Exception):
    """Raised when ``t`` is earlier than a reported actual take-off."""

    def __init__(self, t: int, violations: list[dict]):
        self.t = t
        self.violations = violations
        super().__init__(f"t={t} is earlier than a reported actual take-off")


def _conflict_dto(sorties_by_id, takeoff_a, takeoff_b, rec, t):
    """Project a conflict record onto ``[t, ...]`` and refresh its witness."""
    start = max(float(t), rec.time_start)
    end = rec.time_end
    length = end - start
    witness = start if length <= 1e-9 else 0.5 * (start + end)
    pa = sorties_by_id[rec.sortie_a].position(witness - takeoff_a)
    pb = sorties_by_id[rec.sortie_b].position(witness - takeoff_b)
    dx, dy = pa[0] - pb[0], pa[1] - pb[1]
    return {
        "sortie_a": rec.sortie_a,
        "sortie_b": rec.sortie_b,
        "time_start": round(start, 6),
        "time_end": round(end, 6),
        "witness_time": round(witness, 6),
        "a_pos": [round(v, 6) for v in pa],
        "b_pos": [round(v, 6) for v in pb],
        "horizontal_distance": round((dx * dx + dy * dy) ** 0.5, 6),
        "vertical_distance": round(abs(pa[2] - pb[2]), 6),
    }


def evaluate_warning(sorties, planned_offsets: dict[str, int],
                     reports: dict[str, Report], safety_radius: float,
                     safety_height: float, t: int) -> dict:
    """Compute the deviation warning for a frozen batch at current time ``t``.

    Raises :class:`EarlyTimeReject` when ``t`` precedes a reported take-off.
    """
    # A reported sortie has actually taken off at its offset; an evaluation
    # time before that instant is inconsistent and must be rejected.
    violations = [
        {"sortie_id": sid, "actual_takeoff": rep.actual_offset}
        for sid, rep in sorted(reports.items())
        if t < rep.actual_offset
    ]
    if violations:
        raise EarlyTimeReject(t, violations)

    by_id = {s.id: s for s in sorties}
    statuses: dict[str, str] = {}
    effective: dict[str, int] = {}
    for s in sorties:
        rep = reports.get(s.id)
        if rep is not None:
            statuses[s.id] = REPORTED
            effective[s.id] = rep.actual_offset
        elif planned_offsets[s.id] >= t:
            statuses[s.id] = PLANNED
            effective[s.id] = planned_offsets[s.id]
        else:
            statuses[s.id] = OVERDUE

    # Continuous-time conflicts on the effective schedule, restricted to the
    # participating (non-overdue) sorties and to instants at or after t.
    conflicts: list[dict] = []
    for i, sa in enumerate(sorties):
        if statuses[sa.id] == OVERDUE:
            continue
        for sb in sorties[i + 1:]:
            if statuses[sb.id] == OVERDUE:
                continue
            recs = conflicts_between_sorties(
                sa, effective[sa.id], sb, effective[sb.id],
                safety_radius, safety_height,
            )
            for rec in recs:
                if rec.time_end < t - EPS:
                    continue
                conflicts.append(
                    _conflict_dto(by_id, effective[sa.id], effective[sb.id], rec, t)
                )
    conflicts.sort(key=lambda r: (r["time_start"], r["sortie_a"], r["sortie_b"]))
    first_conflict_time = conflicts[0]["time_start"] if conflicts else None

    sortie_rows = []
    deviations = []
    for s in sorties:
        sid = s.id
        status = statuses[sid]
        rep = reports.get(sid)
        planned = planned_offsets[sid]
        row = {
            "id": sid,
            "status": status,
            "planned_offset": planned,
            "actual_offset": rep.actual_offset if rep is not None else None,
            "revision": rep.revision if rep is not None else None,
            "takeoff_offset": effective.get(sid),
            "deviation_seconds": None,
        }
        if rep is not None:
            deviation = rep.actual_offset - planned
            row["deviation_seconds"] = deviation
            deviations.append({
                "sortie_id": sid,
                "planned_offset": planned,
                "actual_offset": rep.actual_offset,
                "revision": rep.revision,
                "deviation_seconds": deviation,
            })
        sortie_rows.append(row)

    overdue_ids = [row["id"] for row in sortie_rows if row["status"] == OVERDUE]
    return {
        "t": t,
        "complete": not overdue_ids,
        "incomplete": bool(overdue_ids),
        "overdue_sorties": overdue_ids,
        "sorties": sortie_rows,
        "conflicts": conflicts,
        "first_conflict_time": first_conflict_time,
        "reported_deviations": deviations,
    }
