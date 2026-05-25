"""Fiscal year prefix router — IST date-based auto-rollover on April 1."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def get_fy_prefix(today: date | None = None) -> str:
    """Compute GCS fiscal year folder prefix from IST date.

    Fiscal year starts April 1. Auto-rollover on that date.

    Args:
        today: Date to calculate from. Uses current IST date if None.

    Returns:
        String like "FY26-27" (for dates from April 2026 to March 2027).
    """
    if today is None:
        today = datetime.now(IST).date()

    year = today.year
    if today.month >= 4:
        return f"FY{year % 100:02d}-{(year + 1) % 100:02d}"
    return f"FY{(year - 1) % 100:02d}-{year % 100:02d}"
