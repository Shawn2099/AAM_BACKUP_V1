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
    """Convert a 5-field cron expression to a human-readable string.

    AUDIT-002: Prefect accepts step values (``*/5``), lists (``0,30``), and
    ranges (``0-30/10``) in every field — the old implementation called
    ``int()`` on each and crashed with ValueError on any of them, which
    500'd the dashboard ``/status`` endpoint (and therefore froze the UI).
    This version handles the common forms and *always* returns a string:
    anything it cannot render verbosely falls back to the raw expression.
    """
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron

    minute, hour, dom, month, dow = parts
    tz_short = tz.split("/")[-1] if "/" in tz else tz

    def _simple(field: str) -> int | None:
        try:
            return int(field)
        except ValueError:
            return None

    def _time_label() -> str | None:
        """'HH:MM' when both are plain ints, else None."""
        h, m = _simple(hour), _simple(minute)
        if h is None or m is None:
            return None
        return f"{h:02d}:{m:02d}"

    def _fallback(label: str) -> str:
        return f"{label} — {cron} (cron expression) {tz_short}"

    time_label = _time_label()

    # step minute ("*/5") with a fixed hour — "Every N minutes"
    if minute.startswith("*/") and _simple(hour) is not None and dow == "*" and dom == "*":
        try:
            step = int(minute.split("*/", 1)[1])
            return f"Every {step} minutes {tz_short}"
        except ValueError:
            return _fallback("Recurring schedule")

    if dow != "*":
        days = {
            "MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday",
            "THU": "Thursday", "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday",
        }
        if time_label is None:
            return _fallback(f"Every {dow}")
        if "," in dow:
            names = [days.get(p.strip().upper(), p.strip()) for p in dow.split(",")]
            return f"Every {', '.join(names)} at {time_label} {tz_short}"
        day_name = days.get(dow.upper(), dow)
        return f"Every {day_name} at {time_label} {tz_short}"

    if dom != "*":
        d = _simple(dom)
        if d is None or time_label is None:
            return _fallback(f"On day {dom} of month")
        # F14: 11/13 (and the 4-20 range) are always -th; the old code took
        # 11 % 10 -> "st" and 13 % 10 -> "rd".
        suffix = (
            "th" if (4 <= d <= 20) or d in (11, 13)
            else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
        )
        return f"{d}{suffix} of month at {time_label} {tz_short}"

    if time_label is None:
        return _fallback("Recurring schedule")
    return f"Daily at {time_label} {tz_short}"
