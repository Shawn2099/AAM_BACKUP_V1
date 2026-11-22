"""Was the manifest database silently replaced? (opus-decision L2, live-stack finding LS8)

A lost, renamed or restored-over manifest.db is re-created empty by `ManifestDB`: history, approvals, guard
baselines, activation rows and incidents are gone and nobody is told. Backups themselves stay safe (the G1/G3
caps work without a baseline), so this is LOUD, never blocking: one ACTION_REQUIRED mail and a red dashboard item
until an admin acknowledges it.

Detection (a fact check, no config key): the database records `created_at` when its file is created. The
per-run log day folders under `config.run_logs_dir` are written only by runs, which also write the database. A
database created on a LATER day than the oldest day folder cannot be the one those runs wrote to. A genuine
fresh install has no day folder; a database from before the marker has no `created_at`: both give no alarm.
"""
from __future__ import annotations

import json
from pathlib import Path

import pendulum
from loguru import logger

from core.run_artefacts import _DATE_DIR
from core.time_utils import IST, now_iso

ALERT_KEY = "db:recreated"
META_DETECTED = "db_recreated_detected"
META_ACK = "db_recreated_ack"


def oldest_run_day(run_logs_dir) -> str | None:
    """Name (YYYY-MM-DD) of the oldest run-log day folder, or None when there is none."""
    base = Path(run_logs_dir)
    try:
        days = sorted(e.name for e in base.iterdir() if e.is_dir() and _DATE_DIR.match(e.name))
    except OSError:
        return None
    return days[0] if days else None


def detect(config, db) -> dict | None:
    """{created_at, oldest_day} when the database is newer than the run logs, else None."""
    created = db.meta_created_at()
    if not created:
        return None
    oldest = oldest_run_day(config.run_logs_dir)
    if not oldest:
        return None
    created_day = pendulum.parse(created).in_timezone(IST).format("YYYY-MM-DD")
    if created_day > oldest:
        return {"created_at": created, "oldest_day": oldest}
    return None


def check_at_startup(config, db) -> bool:
    """Run once at agent start. True when a NEW finding was recorded (and mailed). Never raises."""
    try:
        if db.get_meta(META_DETECTED):
            return False                                  # already reported; the item stays until acknowledged
        found = detect(config, db)
        if not found:
            return False
        db.set_meta(META_DETECTED, json.dumps({**found, "detected_at": now_iso()}))
        logger.error(f"The backup database was created new at {found['created_at']} although run logs exist "
                     f"since {found['oldest_day']}: history and approvals were lost")
        from core import notify as nt
        nt.notify(config, nt.AlertEvent(
            key=ALERT_KEY, kind="ACTION_REQUIRED", mode="db",
            title="Backup history database was created new",
            message=(f"The backup database was created new at {found['created_at']} although runs exist "
                     f"since {found['oldest_day']}; history and approvals were lost; check for a restore "
                     "mistake. Backups are not blocked. Acknowledge this on the dashboard once checked.")),
            db=db)
        return True
    except Exception as e:  # noqa: BLE001 - startup must never be blocked by this check
        logger.warning(f"Database identity check skipped: {e}")
        return False


def pending(db) -> dict | None:
    """The finding while it is not yet acknowledged, else None."""
    raw = db.get_meta(META_DETECTED)
    if not raw or db.get_meta(META_ACK):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def acknowledge(db, actor: str) -> bool:
    """An admin has seen it. False when there is nothing to acknowledge."""
    if pending(db) is None:
        return False
    db.set_meta(META_ACK, json.dumps({"by": actor, "at": now_iso()}))
    logger.warning(f"The re-created-database notice was acknowledged by {actor}")
    return True
