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

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cronsim import CronSim


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


def schedule_problems(jobs: dict[str, tuple[str, float]], timezone: str,
                      wait_seconds: float, *, start: datetime | None = None,
                      days: int = 366) -> list[str]:
    """Check the pairwise rule over `days` of cron occurrences.

    jobs: {name: (cron, budget_seconds)} for every job that takes the slot.
    Returns human-readable violations (empty = the schedule is provably fine);
    one message per offending job pair, with the arithmetic.
    """
    tz = ZoneInfo(timezone)
    start = start or datetime(2027, 1, 1, tzinfo=tz)
    end = start + timedelta(days=days)
    problems: list[str] = []
    for name, (_, budget_s) in jobs.items():
        if budget_s <= wait_seconds:
            problems.append(
                f"budget.{name}_max_hours ({budget_s / 3600:g} h) must be longer than "
                f"maintenance.concurrency_wait_seconds ({wait_seconds / 3600:g} h): a run that "
                "waits the full time for the slot would have no time left")
    occurrences: list[tuple[datetime, str]] = []
    for name, (cron, _) in jobs.items():
        for t in CronSim(cron, start):
            if t >= end:
                break
            occurrences.append((t, name))
    occurrences.sort()
    seen: set[tuple[str, str]] = set()
    for (t, name), (t_next, nxt) in zip(occurrences, occurrences[1:]):
        budget_s = jobs[name][1]
        gap = (t_next - t).total_seconds()
        if budget_s > gap + wait_seconds and (name, nxt) not in seen:
            seen.add((name, nxt))
            problems.append(
                f"schedule.{name}_cron ({jobs[name][0]!r}, e.g. {t:%a %H:%M}) may run up to "
                f"budget.{name}_max_hours = {budget_s / 3600:g} h, but schedule.{nxt}_cron "
                f"({jobs[nxt][0]!r}) starts {gap / 3600:g} h later ({t_next:%a %H:%M}) and waits "
                f"at most maintenance.concurrency_wait_seconds = {wait_seconds / 3600:g} h for "
                f"the slot: {budget_s / 3600:g} h > {gap / 3600:g} h + {wait_seconds / 3600:g} h. "
                f"Move one of the two jobs, or lower budget.{name}_max_hours.")
    return problems
