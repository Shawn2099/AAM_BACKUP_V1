"""When is a backup leg overdue? (owner 2026-10-22: alerts must follow the chosen schedule).

A leg is overdue when  now > deadline(...)  where the deadline is

    the next scheduled firing AFTER the last success started
    + that leg's run budget (a healthy run may still be working until then)
    + dashboard.stale_grace_hours.

One `CronSim` step per call - exact for daily, twice-daily, weekdays-only, weekly or monthly schedules
and cheap enough for every dashboard poll. All arithmetic is done on real elapsed time (UTC), so a
daylight-saving change cannot shift a deadline by an hour."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from cronsim import CronSim


def _aware(value: datetime, tz: ZoneInfo) -> datetime:
    """`value` as a plain stdlib aware datetime in the schedule's timezone (naive = already that
    timezone). Rebuilt from the instant so a pendulum DateTime (whose own arithmetic differs) never
    reaches the schedule or the deadline addition."""
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return datetime.fromtimestamp(value.timestamp(), tz)


def next_firing(cron: str, timezone_name: str, after: datetime) -> datetime:
    """The first scheduled firing strictly after the minute of `after`."""
    tz = ZoneInfo(timezone_name)
    return next(iter(CronSim(cron, _aware(after, tz))))


def deadline(cron: str, timezone_name: str, started: datetime, budget_hours: float,
             grace_hours: float) -> datetime:
    """The moment a leg whose last success started at `started` becomes overdue."""
    tz = ZoneInfo(timezone_name)
    due = next_firing(cron, timezone_name, started)
    elapsed = timedelta(hours=float(budget_hours) + float(grace_hours))
    return (due.astimezone(timezone.utc) + elapsed).astimezone(tz)
