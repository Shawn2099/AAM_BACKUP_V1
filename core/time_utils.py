"""Centralized time utilities — single source of truth for all datetime operations.

Uses pendulum (already a dependency). Every file in this project that touches
datetime must import from here, not from datetime/zoneinfo directly. This
eliminates parsing bugs, timezone mismatches, and duplicated helper functions.
"""

from __future__ import annotations

from datetime import date

import pendulum

try:
    from pendulum.parsing.exceptions import ParserError
except ImportError:
    # Fallback for some pendulum 3.x versions
    try:
        from pendulum.parsing.parser import ParserError
    except ImportError:
        class ParserError(Exception): pass  # Dummy fallback


# ═══════════════════════════════════════════════════════════════
# Current UTC timestamps
# ═══════════════════════════════════════════════════════════════

def utcnow_iso() -> str:
    """Current UTC time as a timezone-aware ISO 8601 string.

    Returns e.g. "2026-05-30T14:22:00+00:00" — always parseable,
    always carries explicit offset.
    """
    return pendulum.now("UTC").isoformat()


def utcnow_formatted(fmt: str = "YYYY-MM-DD HH:mm z") -> str:
    """Current UTC time as a human-readable string with timezone label.

    Default: "2026-05-30 14:22 UTC"
    """
    return pendulum.now("UTC").format(fmt)


# ═══════════════════════════════════════════════════════════════
# ISO 8601 parsing → local display
# ═══════════════════════════════════════════════════════════════

def parse_iso_to_local(iso_str: str | None, tz: str = "Asia/Kolkata") -> str:
    """Parse any ISO 8601 string and format for local-timezone display.

    Handles Z suffix, ±HH:MM offsets, naive datetimes (treated as UTC).
    Returns "YYYY-MM-DD HH:MM:SS" in the server's local timezone.
    Returns "-" for None/empty/unparseable input.
    """
    if not iso_str:
        return "-"

    try:
        dt = pendulum.parse(str(iso_str).strip())
        if dt is None:
            return "-"
        return dt.in_timezone(tz).format("YYYY-MM-DD HH:mm:ss")
    except (ValueError, ParserError):
        s = str(iso_str).strip()
        return s[:19].replace("T", " ") if s else "-"


def format_iso_for_js(iso_str: str | None) -> str | None:
    """Convert a stored ISO string into a form JS `new Date()` parses reliably.

    Pendulum's isoformat() always includes the timezone offset, so JS gets
    a proper ISO 8601 string every time. Returns None if input is None.
    """
    if not iso_str:
        return None
    try:
        dt = pendulum.parse(str(iso_str).strip())
        if dt is None:
            return None
        return dt.isoformat()
    except (ValueError, ParserError):
        return None


# ═══════════════════════════════════════════════════════════════
# Cutoff / relative dates
# ═══════════════════════════════════════════════════════════════

def cutoff_iso(days: int) -> str:
    """Return the ISO timestamp for `days` ago from now in UTC.

    e.g. cutoff_iso(7) → "2026-05-23T14:22:00+00:00"
    """
    return pendulum.now("UTC").subtract(days=days).isoformat()


# ═══════════════════════════════════════════════════════════════
# Fiscal year routing
# ═══════════════════════════════════════════════════════════════

IST = pendulum.timezone("Asia/Kolkata")


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
