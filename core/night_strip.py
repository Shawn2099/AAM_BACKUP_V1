"""One mark per night for the dashboard's "last N nights" strip.

A night is one IST calendar day. Its state is the WORST result of the runs that started that
day (a failed run is never hidden by a later success the same day), taken from
`core.statuses.category` - the single definition of what a status means.
"""

from __future__ import annotations

import pendulum

from core import statuses
from core.time_utils import IST

OK = "ok"
PARTIAL = "partial"
BAD = "bad"
NONE = "none"

# Higher = worse; the night shows the worst run of that day.
_SEVERITY = {NONE: 0, OK: 1, PARTIAL: 2, BAD: 3}
_STATE_OF_CATEGORY = {
    "success": OK, "no-changes": OK,
    "partial": PARTIAL,
    "failed": BAD, "blocked": BAD, "skipped": BAD, "unknown": BAD,
}


def state_of(status: str) -> str:
    """Strip state of one run status; a run still in progress does not count yet."""
    cat = statuses.category(status)
    if cat == "running":
        return NONE
    return _STATE_OF_CATEGORY.get(cat, BAD)


def build(runs: list[dict], *, days: int, today: pendulum.DateTime | None = None) -> list[dict]:
    """[{date: 'YYYY-MM-DD', state}] for the last `days` nights, oldest first, ending today (IST)."""
    end = (today or pendulum.now(IST)).in_timezone(IST).start_of("day")
    worst: dict[str, str] = {}
    for run in runs:
        try:
            day = pendulum.parse(run["started_at"]).in_timezone(IST).format("YYYY-MM-DD")
        except Exception:  # noqa: BLE001 - an unreadable row must not blank the strip
            continue
        state = state_of(run.get("status", ""))
        if _SEVERITY[state] > _SEVERITY[worst.get(day, NONE)]:
            worst[day] = state
    out = []
    for offset in range(days - 1, -1, -1):
        day = end.subtract(days=offset).format("YYYY-MM-DD")
        out.append({"date": day, "state": worst.get(day, NONE)})
    return out
