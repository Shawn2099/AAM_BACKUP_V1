"""Manual-only legs (owner 2026-11-23): one place that says what a backup leg's mode is.

A leg (`cloud` or `lan`) is in one of three modes:
  off      `<leg>.enabled` false      - nothing runs, nothing is expected
  nightly  enabled and scheduled      - judged against `schedule.<leg>_cron` (overdue, shortfall, ...)
  manual   enabled, `scheduled` false - started from the dashboard only; no schedule-based alarm,
                                        but the truth is always shown (last backup date and age) and
                                        a soft amber note (never a mail) appears after
                                        `<leg>.on_demand_warn_after_days`.

Every schedule-based check asks `is_nightly()`; every "say the truth" line comes from `mode_line()`.
"""

from __future__ import annotations

import pendulum

from core.time_utils import IST, format_ist

LEG_NAME = {"cloud": "Cloud", "lan": "LAN"}
OFF, NIGHTLY, MANUAL = "off", "nightly", "manual"


def mode(config, leg: str) -> str:
    cfg = getattr(config, leg)
    if not cfg.enabled:
        return OFF
    return NIGHTLY if getattr(cfg, "scheduled", True) else MANUAL


def is_nightly(config, leg: str) -> bool:
    return mode(config, leg) == NIGHTLY


def is_manual_only(config, leg: str) -> bool:
    return mode(config, leg) == MANUAL


def nothing_automatic(config) -> bool:
    """At least one leg is on, and every leg that is on is manual-only."""
    modes = [mode(config, leg) for leg in LEG_NAME]
    return MANUAL in modes and NIGHTLY not in modes


def warn_after_days(config, leg: str) -> int:
    return int(getattr(config, leg).on_demand_warn_after_days)


def last_success(db, leg: str) -> dict | None:
    """The most recent successful run of the leg in ANY financial year."""
    from core.manifest import ALL_FYS

    return db.last_successful_run(leg, fy=ALL_FYS)


def last_success_age_days(db, leg: str, now=None) -> tuple[str | None, int | None]:
    """(ISO time of the last success or None, whole days since it or None)."""
    now = now or pendulum.now(IST)
    run = last_success(db, leg)
    ts = (run.get("ended_at") or run.get("started_at")) if run else None
    if not ts:
        return None, None
    delta = now - pendulum.parse(ts, tz=IST)
    return ts, max(0, int(delta.total_seconds() // 86400))


def is_old(config, db, leg: str, now=None) -> bool:
    """Manual-only leg whose last success is older than the warn limit, or that never succeeded."""
    if not is_manual_only(config, leg):
        return False
    ts, days = last_success_age_days(db, leg, now)
    return ts is None or days > warn_after_days(config, leg)


def lan_not_final_fy(db) -> str | None:
    """The FY whose final NAS backup at the April rollover did not happen (manual-only NAS that was off),
    until a successful LAN backup of that FY started after it (owner 2026-11-23). None otherwise."""
    import json

    try:
        raw = db.get_meta("lan_final_missing")
        rec = json.loads(raw) if raw else None
        if not isinstance(rec, dict) or not rec.get("fy"):
            return None
        later = db.last_successful_run("lan", fy=rec["fy"])
        if later and str(later.get("started_at") or "") > str(rec.get("at") or ""):
            return None
        return str(rec["fy"])
    except Exception:  # noqa: BLE001 - unreadable = no note; the run rows still tell the truth
        return None


def mode_line(config, db, leg: str, now=None) -> dict | None:
    """The truth line for a leg that is off or manual-only; None for a nightly leg.

    {"mode", "text", "soft_warn"}. Reports and the dashboard show `text` as is."""
    m = mode(config, leg)
    name = LEG_NAME[leg]
    if m == NIGHTLY:
        return None
    if m == OFF:
        return {"mode": OFF, "text": f"{name}: off", "soft_warn": False}
    ts, days = last_success_age_days(db, leg, now)
    if ts is None:
        text = f"{name}: manual only, no successful backup recorded yet"
    else:
        unit = "day" if days == 1 else "days"
        text = f"{name}: manual only, last backup {format_ist(ts)}, {days} {unit} ago"
    unsynced = lan_not_final_fy(db) if leg == "lan" else None
    if unsynced:
        text += f"; {unsynced} was not final-synced to the NAS at the year switch"
    return {"mode": MANUAL, "text": text, "soft_warn": is_old(config, db, leg, now) or bool(unsynced)}


def mode_lines(config, db, now=None) -> list[dict]:
    """Truth lines for every leg that is off or manual-only, plus the 'nothing automatic' line."""
    out = [ln for leg in LEG_NAME if (ln := mode_line(config, db, leg, now))]
    if nothing_automatic(config):
        out.append({"mode": "none_automatic", "soft_warn": True,
                    "text": "Nothing runs automatically: every backup is started by hand from "
                            "the dashboard"})
    return out


__all__ = ["LEG_NAME", "mode", "is_nightly", "is_manual_only", "nothing_automatic", "mode_line",
           "mode_lines", "is_old", "last_success_age_days"]
