"""Centralized time utilities — single source of truth for all datetime operations.

Uses pendulum (already a dependency). Every file in this project that touches
datetime must import from here, not from datetime/zoneinfo directly. This
eliminates parsing bugs, timezone mismatches, and duplicated helper functions.

All timestamps are stored and displayed in IST (Asia/Kolkata, UTC+5:30).
India does not observe DST, so IST is constant year-round.
"""

from __future__ import annotations

from datetime import date

from cron_descriptor import ExpressionDescriptor, FormatException, MissingFieldException, Options
from loguru import logger
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

def cron_to_human(cron: str, tz: str | None = None) -> str:
    """Convert a 5-field cron expression to a human-readable string.

    Uses ExpressionDescriptor as the primary engine with 24-hour time formatting.
    Two-tier exception handling ensures the status dashboard never crashes on malformed input.
    """
    if not cron or not isinstance(cron, str):
        return str(cron or "")
    try:
        options = Options()
        options.use_24hour_time_format = True
        desc = ExpressionDescriptor(cron.strip(), options).get_description()
        tz_short = str(tz or "").split("/")[-1] if "/" in str(tz or "") else str(tz or "")
        return f"{desc} ({tz_short})" if tz_short else desc
    except (MissingFieldException, FormatException) as e:
        logger.warning(f"cron_descriptor failed to parse cron expression {cron!r}: {e}")
        return cron
    except Exception as e:
        logger.error(f"cron_to_human: unexpected error parsing {cron!r}, falling back to raw: {e}")
        return cron
