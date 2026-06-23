"""Centralized time utilities — single source of truth for all datetime operations.

Uses pendulum (already a dependency). Every file in this project that touches
datetime must import from here, not from datetime/zoneinfo directly. This
eliminates parsing bugs, timezone mismatches, and duplicated helper functions.

All timestamps are stored and displayed in IST (Asia/Kolkata, UTC+5:30).
India does not observe DST, so IST is constant year-round.
"""

from __future__ import annotations

from datetime import date

import pendulum


# ═══════════════════════════════════════════════════════════════
# Timezone constant
# ═══════════════════════════════════════════════════════════════

IST = pendulum.timezone("Asia/Kolkata")


# ═══════════════════════════════════════════════════════════════
# Current IST timestamps
# ═══════════════════════════════════════════════════════════════

def now_iso() -> str:
    """Current IST time as a timezone-aware ISO 8601 string.

    Returns e.g. "2026-05-30T19:52:00+05:30" — always parseable,
    always carries explicit offset.
    """
    return pendulum.now(IST).isoformat()


def now_formatted(fmt: str = "YYYY-MM-DD HH:mm z") -> str:
    """Current IST time as a human-readable string with timezone label.

    Default: "2026-05-30 19:52 IST"
    """
    return pendulum.now(IST).format(fmt)


# ═══════════════════════════════════════════════════════════════
# Cutoff / relative dates
# ═══════════════════════════════════════════════════════════════

def cutoff_iso(days: int) -> str:
    """Return the ISO timestamp for `days` ago from now in IST.

    e.g. cutoff_iso(7) → "2026-05-23T19:52:00+05:30"
    """
    return pendulum.now(IST).subtract(days=days).isoformat()


# ═══════════════════════════════════════════════════════════════
# Fiscal year routing
# ═══════════════════════════════════════════════════════════════

def get_fy_prefix(today: date | None = None) -> str:
    """Compute GCS fiscal year folder prefix from IST date.

    Fiscal year starts April 1. Auto-rollover on that date.

    Args:
        today: Date to calculate from. Uses current IST date if None.

    Returns:
        String like "FY26-27" (for dates from April 2026 to March 2027).
    """
    if today is None:
        today = pendulum.now(IST).date()

    year = today.year
    if today.month >= 4:
        return f"FY{year % 100:02d}-{(year + 1) % 100:02d}"
    return f"FY{(year - 1) % 100:02d}-{year % 100:02d}"


# ═══════════════════════════════════════════════════════════════
# Schedule display helpers
# ═══════════════════════════════════════════════════════════════

def cron_to_human(cron: str, tz: str) -> str:
    """Convert a 5-field cron expression to a human-readable string."""
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron

    minute, hour, dom, month, dow = parts
    tz_short = tz.split("/")[-1] if "/" in tz else tz

    if dow != "*":
        days = {
            "MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday",
            "THU": "Thursday", "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday",
        }
        day_name = days.get(dow.upper(), dow)
        return f"Every {day_name} at {int(hour):02d}:{int(minute):02d} {tz_short}"

    if dom != "*":
        suffix = (
            "th" if 4 <= int(dom) <= 20
            else {1: "st", 2: "nd", 3: "rd"}.get(int(dom) % 10, "th")
        )
        return f"{int(dom)}{suffix} of month at {int(hour):02d}:{int(minute):02d} {tz_short}"

    return f"Daily at {int(hour):02d}:{int(minute):02d} {tz_short}"
