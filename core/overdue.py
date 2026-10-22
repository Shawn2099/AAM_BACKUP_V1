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


def latest_firing(cron: str, timezone_name: str, at: datetime) -> datetime:
    """The most recent scheduled firing at or before `at` (the run that is the latest one owed)."""
    tz = ZoneInfo(timezone_name)
    moment = _aware(at, tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    return next(iter(CronSim(cron, moment, reverse=True)))


def latest_owed(cron: str, timezone_name: str, now: datetime, budget_hours: float) -> datetime:
    """The most recent scheduled run whose run time (`budget_hours`) has already passed at `now`:
    the run the owner has had a chance to hear about. A run that is still working (it started at
    most `budget_hours` ago) is not owed yet, so its failure mail is not expected before it ends."""
    tz = ZoneInfo(timezone_name)
    at = datetime.fromtimestamp(_aware(now, tz).timestamp() - float(budget_hours) * 3600, tz)
    return latest_firing(cron, timezone_name, at)


def firings_between(cron: str, timezone_name: str, start: datetime, end: datetime) -> int:
    """How many scheduled firings fall in the half-open interval [start, end)."""
    tz = ZoneInfo(timezone_name)
    begin, stop = _aware(start, tz), _aware(end, tz)
    count = 0
    # CronSim returns times strictly after its start, so begin one minute earlier to include `start`
    for when in CronSim(cron, begin - timedelta(minutes=1)):
        if when >= stop:
            break
        count += 1
    return count


def deadline(cron: str, timezone_name: str, started: datetime, budget_hours: float,
             grace_hours: float) -> datetime:
    """The moment a leg whose last success started at `started` becomes overdue."""
    tz = ZoneInfo(timezone_name)
    due = next_firing(cron, timezone_name, started)
    elapsed = timedelta(hours=float(budget_hours) + float(grace_hours))
    return (due.astimezone(timezone.utc) + elapsed).astimezone(tz)
