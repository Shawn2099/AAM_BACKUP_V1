"""Per-job run budgets and the schedule check (plan WP14, owner decision 2026-10-15).

Why budgets: the configured tool timeouts x retries add up to ~46 h (cloud)
and ~9 h (LAN) - no schedule can be proven against those. Each job instead
gets a wall-clock budget (config `budget:`), counted from the moment the
job starts - INCLUDING any wait for the backup slot. The long steps take
their timeout from what is left (`RunBudget.cap`); a job that reaches its
budget stops cleanly and is recorded *_PARTIAL ("continues next run"),
never as a success and never left holding the slot.

Because a job never holds the slot longer than `budget` after its scheduled
start, one pairwise rule proves the schedule: for every scheduled start of
a slot job, the next scheduled start of any slot job must satisfy

    budget(this job) <= gap to that next start + maintenance.concurrency_wait_seconds

(the next job may wait up to concurrency_wait_seconds for the slot, and its
own budget already includes that wait). `schedule_problems` checks it over a
whole year of cron occurrences with cronsim.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cronsim import CronSim

# Names as the Settings page shows them.
_JOB_NAMES = {"lan": "LAN backup", "cloud": "Cloud backup", "audit": "weekly safety check"}
_BUDGET_KEY = {"lan": "budget.lan_max_hours", "cloud": "budget.cloud_max_hours",
               "audit": "budget.audit_max_hours"}
_CRON_KEY = {"lan": "schedule.lan_cron", "cloud": "schedule.cloud_cron", "audit": "schedule.audit_cron"}
_ROUND_TO_MINUTES = 15


class BudgetExceeded(RuntimeError):
    """No time left in the job's run budget for the next step."""


@dataclass(frozen=True)
class RunBudget:
    """A job's wall-clock budget; `started` is time.monotonic() at job start."""

    leg: str
    seconds: float
    started: float

    def remaining(self, clock=time.monotonic) -> float:
        return self.seconds - (clock() - self.started)

    def cap(self, timeout_s: float, reserve_s: float = 0.0, clock=time.monotonic) -> float:
        """The step timeout to use: its own timeout, or what is left of the budget
        after `reserve_s` (kept for the steps after it), whichever is smaller."""
        left = self.remaining(clock) - reserve_s
        if left <= 0:
            raise BudgetExceeded(
                f"{self.leg}: run budget of {self.seconds / 3600:g} h used up "
                f"(reserve {reserve_s / 60:.0f} min kept for the remaining steps)")
        return min(float(timeout_s), left)

    def binding(self, timeout_s: float, reserve_s: float = 0.0, clock=time.monotonic) -> bool:
        """True when the budget, not the step's own timeout, limits this step."""
        return self.remaining(clock) - reserve_s < timeout_s

    def describe(self) -> str:
        return f"{self.seconds / 3600:g} h (budget.{self.leg}_max_hours)"


def late_seconds(expected: float, actual: float) -> float:
    """How long after its scheduled time a run really started (never negative). The run budget
    is counted from the scheduled time, so a late start gets only what is left of it."""
    return max(0.0, actual - expected)


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:g} h"


def _round_up(moment: datetime) -> datetime:
    """The next quarter hour at or after `moment` (a start time people can type)."""
    step = _ROUND_TO_MINUTES * 60
    stamp = math.ceil(moment.timestamp() / step) * step
    return datetime.fromtimestamp(stamp, tz=moment.tzinfo)


def _round_down(moment: datetime) -> datetime:
    step = _ROUND_TO_MINUTES * 60
    stamp = math.floor(moment.timestamp() / step) * step
    return datetime.fromtimestamp(stamp, tz=moment.tzinfo)


def _clock(moment: datetime, relative_to: datetime) -> str:
    """HH:MM, with the weekday when it is not the same day as `relative_to`."""
    return f"{moment:%H:%M}" if moment.date() == relative_to.date() else f"{moment:%a %H:%M}"


def _with_start_time(cron: str, hour: int, minute: int) -> str | None:
    """`cron` with its minute and hour replaced (only for plain numeric minute/hour fields)."""
    parts = cron.split()
    if len(parts) != 5 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return f"{minute} {hour} {parts[2]} {parts[3]} {parts[4]}"


def _hard_problems(jobs, timezone: str, wait_seconds: float, days: int = 366) -> int:
    return len(schedule_problems(jobs, timezone, wait_seconds, days=days, _suggest=False))


# Daily and weekly jobs repeat within a few weeks: a short window finds the candidates quickly,
# the full year then confirms the one chosen.
_QUICK_DAYS = 35


def _nearest_working_time(jobs: dict[str, tuple[str, float]], name: str, timezone: str,
                          wait_seconds: float) -> tuple[int, int] | None:
    """The (hour, minute) closest to `name`'s current start at which the WHOLE schedule fits,
    trying every quarter hour; None when no time of day works (or the cron is not simple)."""
    cron, budget_s = jobs[name]
    parts = cron.split()
    if len(parts) != 5 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    now_min = int(parts[1]) * 60 + int(parts[0])
    steps = sorted((s for s in range(0, 24 * 60, _ROUND_TO_MINUTES) if s != now_min),
                   key=lambda s: min(abs(s - now_min), 24 * 60 - abs(s - now_min)))
    for step in steps:
        candidate = _with_start_time(cron, step // 60, step % 60)
        trial = {**jobs, name: (candidate, budget_s)}
        if (candidate and _hard_problems(trial, timezone, 0, _QUICK_DAYS) == 0
                and _hard_problems(trial, timezone, 0) == 0):
            return step // 60, step % 60
    for step in steps:      # no time avoids every overlap: accept the allowed waiting time
        candidate = _with_start_time(cron, step // 60, step % 60)
        trial = {**jobs, name: (candidate, budget_s)}
        if (candidate and _hard_problems(trial, timezone, wait_seconds, _QUICK_DAYS) == 0
                and _hard_problems(trial, timezone, wait_seconds) == 0):
            return step // 60, step % 60
    return None


def _overlap_message(name: str, nxt: str, t: datetime, t_next: datetime, budget_s: float,
                     wait_s: float, cron: str, next_cron: str, move_earlier: bool,
                     fix: tuple[int, int] | None, others: tuple[str, ...] = ()) -> str:
    """Action first, then the reason, then the technical detail for support.

    `move_earlier`: the earlier job (`name`) is the one that runs less often (e.g. the weekly
    safety check), so moving it disturbs the least; otherwise the later job is moved. `fix` is
    the nearest start time at which the whole schedule fits (None = none found)."""
    this, other = _JOB_NAMES.get(name, name), _JOB_NAMES.get(nxt, nxt)
    mover, mover_at = (this, t) if move_earlier else (other, t_next)
    gap = (t_next - t).total_seconds()
    latest_end = t + timedelta(seconds=budget_s)
    if fix is not None:
        move = (f"Move the {mover} to start at {fix[0]:02d}:{fix[1]:02d} "
                f"(it is {mover_at:%H:%M} now; this is the closest time that fits your other backups)")
    elif move_earlier:
        latest_start = _round_down(t_next + timedelta(seconds=wait_s - budget_s))
        move = f"Start the {this} at {_clock(latest_start, t)} or earlier (it is set to {t:%a %H:%M})"
    else:
        earliest = _round_up(t + timedelta(seconds=budget_s - wait_s))
        move = f"Start the {other} at {_clock(earliest, t_next)} or later (it is set to {t_next:%H:%M})"
    # the longest time the earlier job may be given so that the later one still gets its turn
    allowed = math.floor((gap + wait_s) / 1800) * 1800
    shorten = (f"set the {this}'s maximum running time to {_hours(allowed)} or less "
               "(Advanced settings, Run time limits)" if allowed >= 1800 else
               f"give the {this} a different day or time")
    not_cause = ""
    if others:
        names = " and ".join(_JOB_NAMES.get(o, o) for o in others)
        not_cause = f" Changing the {names} time will not fix this."
    return (
        f"{move}, or {shorten}.{not_cause} "
        f"Why: the {this} starts {t:%a %H:%M} and may run for up to {_hours(budget_s)} "
        f"(until {latest_end:%a %H:%M}); the {other} starts {t_next:%a %H:%M} and waits only "
        f"{_hours(wait_s)} for it, so it could be skipped. "
        f"[{_CRON_KEY[name]} {cron!r}, {_CRON_KEY[nxt]} {next_cron!r}: "
        f"{_hours(budget_s)} > {_hours(gap)} + {_hours(wait_s)}]")


def schedule_problems(jobs: dict[str, tuple[str, float]], timezone: str,
                      wait_seconds: float, *, start: datetime | None = None,
                      days: int = 366, _suggest: bool = True) -> list[str]:
    """Check the pairwise rule over `days` of cron occurrences.

    jobs: {name: (cron, budget_seconds)} for every job that takes the slot.
    Returns human-readable violations (empty = the schedule is provably fine);
    one message per offending job pair: what to do, why, then the arithmetic.
    """
    tz = ZoneInfo(timezone)
    start = start or datetime(2027, 1, 1, tzinfo=tz)
    end = start + timedelta(days=days)
    problems: list[str] = []
    for name, (_, budget_s) in jobs.items():
        if budget_s <= wait_seconds:
            problems.append(
                f"Set {_BUDGET_KEY.get(name, name)} above {_hours(wait_seconds)} "
                f"(it is {_hours(budget_s)}), or shorten maintenance.concurrency_wait_seconds. "
                "Why: a run that waits the full time for the backup slot would have no time left.")
    occurrences: list[tuple[datetime, str]] = []
    for name, (cron, _) in jobs.items():
        for t in CronSim(cron, start):
            if t >= end:
                break
            occurrences.append((t, name))
    occurrences.sort()
    per_year = {n: sum(1 for _, o in occurrences if o == n) for n in jobs}
    seen: set[tuple[str, str]] = set()
    for (t, name), (t_next, nxt) in zip(occurrences, occurrences[1:]):
        budget_s = jobs[name][1]
        gap = (t_next - t).total_seconds()
        if budget_s > gap + wait_seconds and (name, nxt) not in seen:
            seen.add((name, nxt))
            move_earlier = per_year[name] < per_year[nxt]
            fix = None
            if _suggest:
                try:   # advice only: a failure here must never change the verdict or break saving
                    fix = _nearest_working_time(jobs, name if move_earlier else nxt, timezone,
                                                wait_seconds)
                except Exception:  # noqa: BLE001
                    fix = None
            problems.append(_overlap_message(
                name, nxt, t, t_next, budget_s, wait_seconds, jobs[name][0], jobs[nxt][0],
                move_earlier=move_earlier, fix=fix,
                others=tuple(j for j in jobs if j not in (name, nxt))))
    return problems
