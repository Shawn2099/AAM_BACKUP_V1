"""The period a report covers (plan C1): a half-open [start, end) interval in IST.

Scheduled emails use whole calendar periods (`previous_week`, `previous_month`), taken from the
flow run's SCHEDULED time so a report sent late still covers the right period (G-9). Dashboard
downloads and send buttons use a rolling window ending now (`last_days`).
"""
from __future__ import annotations

from dataclasses import dataclass

import pendulum

from core.time_utils import IST


@dataclass(frozen=True)
class ReportPeriod:
    start: pendulum.DateTime
    end: pendulum.DateTime
    label: str
    kind: str

    def contains(self, when) -> bool:
        return self.start <= when < self.end

    @classmethod
    def previous_week(cls, scheduled) -> "ReportPeriod":
        """The last full Monday 00:00 - Monday 00:00 week before `scheduled`."""
        scheduled = pendulum.instance(scheduled).in_timezone(IST)
        end = scheduled.start_of("week")                     # pendulum weeks start on Monday
        start = end.subtract(weeks=1)
        return cls(start, end, _range_label(start, end.subtract(days=1)), "Weekly")

    @classmethod
    def previous_month(cls, scheduled) -> "ReportPeriod":
        scheduled = pendulum.instance(scheduled).in_timezone(IST)
        end = scheduled.start_of("month")
        start = end.subtract(months=1)
        return cls(start, end, start.format("MMMM YYYY"), "Monthly")

    @classmethod
    def last_days(cls, days: int, now=None) -> "ReportPeriod":
        end = pendulum.instance(now).in_timezone(IST) if now else pendulum.now(IST)
        start = end.subtract(days=days)
        kind = {7: "Weekly", 30: "Monthly"}.get(days, f"Last {days} days")
        return cls(start, end, _range_label(start, end.subtract(days=1)), kind)


def _range_label(first, last) -> str:
    if first.month == last.month:
        return f"{first.format('D')}-{last.format('D MMM YYYY')}"
    return f"{first.format('D MMM')} - {last.format('D MMM YYYY')}"
