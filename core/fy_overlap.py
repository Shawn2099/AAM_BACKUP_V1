"""FY overlap helpers (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, section 2.1).

While two financial years are active (the configured year plus the next one, from the activation day until the
April switch) every backup pass runs the unchanged pipeline once per year. This module holds the pure helpers
the passes and the rollover share: year arithmetic, the per-pass config, ids with an FY suffix, row FY lookup and
the NAS folder rule. Only `activate_if_due` (section 3, the daily rollover check) writes: the next year's source
folder and its activation record. The pure helpers never touch the database.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

import pendulum
from loguru import logger

from core import fy_rollover
from core.fy_rollover import _child_path, _fy_name, _parent_path
from core.guards import MOUNT_MARKER
from core.health import CLOCK_OK, check_clock
from core.notify import AUDIENCE_REPORTS, AlertEvent, notify
from core.run_artefacts import RunArtefacts
from core.time_utils import get_fy_prefix, now_iso

# ensure_nas_folder() outcomes
NAS_CREATED = "created"            # folder missing: made, marker written
NAS_READY = "ready"                # folder and marker already there
NAS_MARKED = "marked"              # folder existed, empty: marker written
NAS_NEEDS_APPROVAL = "needs_approval"   # folder has files but no marker: left alone (guard G5 asks)

# Indian Standard Time has no DST; a fixed offset is exact.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

_FY_RE = re.compile(r"^FY(\d{2})-(\d{2})$", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"-(FY\d{2}-\d{2})$")


def _start_year(fy: str) -> int:
    match = _FY_RE.match(fy or "")
    if not match:
        raise ValueError(f"Not a financial year name: {fy!r} (expected a form like FY26-27)")
    return 2000 + int(match.group(1))


def next_fy(fy: str) -> str:
    """FY26-27 -> FY27-28; FY99-00 -> FY00-01."""
    start = _start_year(fy) + 1
    return f"FY{start % 100:02d}-{(start + 1) % 100:02d}"


def activation_date(configured_fy: str, activate_on: str) -> dt.date:
    """The calendar day the next FY is activated, for the configured FY and an MM-DD day.

    A month from April on falls in the year the configured FY starts (31 Dec 2026 for FY26-27); January to
    March falls in the year after that (15 Jan 2027).
    """
    month, day = (int(part) for part in activate_on.split("-"))
    start = _start_year(configured_fy)
    year = start if month >= 4 else start + 1
    return dt.date(year, month, day)


def closing_moment(from_fy: str) -> dt.datetime:
    """1 April 00:00 IST that closes `from_fy` (2027-04-01 for FY26-27)."""
    return dt.datetime(_start_year(from_fy) + 1, 4, 1, 0, 0, tzinfo=IST)


def fy_of_id(ident: str | None) -> str | None:
    """The FY of a run / audit id that carries a `-FYxx-yy` suffix, else None."""
    if not ident:
        return None
    match = _SUFFIX_RE.search(str(ident))
    return match.group(1) if match else None


def pass_run_id(base: str, fy: str) -> str:
    """Run id of a next-FY pass: `<flow-run>-cloud` -> `<flow-run>-cloud-FY27-28`."""
    return f"{base}-{fy}"


def artifact_key(base: str, fy: str) -> str:
    """Prefect artifact key of a pass (keys are lowercase)."""
    return f"{base}-{fy.lower()}"


def config_for_fy(config, fy: str):
    """A copy of `config` whose source and NAS folders are the `fy` folders (plan D-3).

    `E:\\FY26-27` -> `E:\\FY27-28` and `\\\\nas\\backup\\FY26-27` -> `\\\\nas\\backup\\FY27-28`, the same rule
    `update_config_yaml` uses. The NAS path moves only when its leaf is a year folder (a cloud-only placeholder
    stays). Every other section is shared, unchanged.
    """
    source = config.paths.source_drive
    if _fy_name(source) is None:
        raise ValueError(f"The source folder {source!r} does not end in a financial-year folder; "
                         "an overlap pass cannot be built for it.")
    new_source = _child_path(_parent_path(source), fy)
    new_lan = config.paths.lan_destination
    if _fy_name(new_lan) is not None:
        new_lan = _child_path(_parent_path(new_lan), fy)
    paths = config.paths.model_copy(update={"source_drive": new_source, "lan_destination": new_lan})
    return config.model_copy(update={"paths": paths})


def _metrics(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def row_fy(row: dict) -> str | None:
    """The FY a run_history / audit row belongs to.

    Order: the id suffix, then `extended_metrics.fy`, then `extended_metrics.guards.fy`. Baseline rows have
    none of these and return None (the configured year).
    """
    fy = fy_of_id(row.get("run_id")) or fy_of_id(row.get("audit_id"))
    if fy:
        return fy
    metrics = _metrics(row.get("extended_metrics"))
    if metrics.get("fy"):
        return str(metrics["fy"])
    guards = metrics.get("guards")
    if isinstance(guards, dict) and guards.get("fy"):
        return str(guards["fy"])
    return None


def _is_dir(path: str) -> bool:
    return Path(path).is_dir()


def ensure_nas_folder(lan_root: str, fy: str) -> str:
    """Make `<lan_root>\\<fy>` ready for a backup pass, without ever inventing a share (plan section 3).

    `lan_root` is the share's parent folder (`\\\\nas\\backup`). An unreachable parent raises OSError (the pass
    fails with AAM-LAN-01); the folder is never created outside an existing share. Returns one of NAS_*.
    """
    if not _is_dir(lan_root):
        raise OSError(f"NAS share {lan_root} is not reachable - the folder {fy} was not made")
    folder = Path(lan_root) / fy
    marker = folder / MOUNT_MARKER
    if not folder.exists():
        folder.mkdir()
        marker.touch()
        return NAS_CREATED
    if marker.is_file():
        return NAS_READY
    if not any(folder.iterdir()):
        marker.touch()
        return NAS_MARKED
    return NAS_NEEDS_APPROVAL


# activate_if_due() outcomes
ACTIVATED = "activated"               # folder + placeholder made, record written, INFO mail sent
ALREADY_ACTIVE = "already_active"     # the record exists: nothing to do
DISABLED = "disabled"                 # rollover.overlap_enabled is false
NO_FY_FOLDER = "no_fy_folder"         # the source folder is not a financial-year folder
NOT_DUE = "not_due"                   # before rollover.overlap_activate_on
REFUSED_STATE = "refused_state"       # CLOCK_BEHIND or JUMP: never activated (the rollover check alerts)
CLOCK_UNPROVEN = "clock_unproven"     # the clock is not OK: nothing created, mail AAM-ROL-07
REFUSED_LINK = "refused_link"         # the next year's folder is a link or junction
FOLDER_FAILED = "folder_failed"       # the folder could not be made (mail AAM-ROL-07 with the reason)
FAILED = "failed"                     # an unexpected error: logged, the daily check goes on

NOT_ADDED_FAULT = "AAM-ROL-07"        # "Next financial year not added yet - clock not proven"


def _is_link_or_junction(path: str) -> bool:
    return os.path.islink(path) or os.path.isjunction(path)


def activate_if_due(config, db, now: dt.datetime | None = None) -> str:
    """Add the next financial year to the backups once it is due (plan section 3, decision D-2).

    Called by the daily rollover check before the unprotected-files alarm. Idempotent: the activation record
    `db_meta['overlap:<next FY>']` is written last, so a crash before it is repeated safely the next morning.
    Creates only the SOURCE folder (+ placeholder); the NAS folder is made by the next-year LAN pass. Acts only on a
    proven clock (`check_clock`, the rollover's own check); otherwise mails and waits. Never raises: an unexpected
    error is logged and returns FAILED.
    """
    try:
        return _activate(config, db, now if now is not None else pendulum.now(IST))
    except Exception as e:  # noqa: BLE001 - the daily check must go on
        logger.warning(f"FY overlap activation check failed: {type(e).__name__}: {e}")
        return FAILED


def _activate(config, db, now: dt.datetime) -> str:
    rollover = config.rollover
    if not rollover.overlap_enabled:
        return DISABLED
    configured = fy_rollover.configured_fy(config.paths.source_drive, config.paths.lan_destination)
    if configured is None:
        return NO_FY_FOLDER
    nxt = next_fy(configured)
    if db.overlap_record(nxt) is not None:
        return ALREADY_ACTIVE
    today = now.astimezone(IST).date()
    activate_on = activation_date(configured, rollover.overlap_activate_on)
    if today < activate_on:
        return NOT_DUE
    kind = fy_rollover.classify_rollover(configured, get_fy_prefix(today))
    if kind not in (fy_rollover.NONE, fy_rollover.FORWARD):
        logger.warning(f"FY overlap: {nxt} not activated - the clock says {kind} against {configured}; "
                       "the rollover check reports it")
        return REFUSED_STATE

    health = config.health
    clock = check_clock(
        max_skew_seconds=health.max_clock_skew_seconds,
        connection_timeout=health.clock_check_timeout_seconds,
        ntp_server=health.ntp_server,
        w32tm_timeout_s=health.w32tm_timeout_seconds,
        artefacts=RunArtefacts.ephemeral("overlap-clock"),
    )
    if clock.state != CLOCK_OK:
        _not_added(config, configured, nxt, today, activate_on,
                   f"the server clock could not be proven correct ({clock.state}: {clock.reason})")
        return CLOCK_UNPROVEN

    source_root = _parent_path(config.paths.source_drive)
    new_source = _child_path(source_root, nxt)
    if _is_link_or_junction(new_source):
        _not_added(config, configured, nxt, today, activate_on,
                   f"the folder {new_source} is a link or junction, not a folder")
        return REFUSED_LINK
    try:
        fy_rollover.create_source_folder(source_root, nxt, rollover.source_placeholder_name)
    except OSError as e:
        _not_added(config, configured, nxt, today, activate_on,
                   f"the folder {new_source} could not be made ({e})")
        return FOLDER_FAILED

    db.set_overlap_record(nxt, {
        "activated_at": now_iso(),
        "from_fy": configured,
        "source_folder": new_source,
        "clock": clock.to_metrics(),
    })
    notify(config, AlertEvent(
        key=f"rollover:activated:{nxt}", kind="INFO", mode="rollover",
        title=f"{nxt} is added to the backups",
        message=(f"From today {nxt} is backed up as well as {configured}. Files saved in {new_source} are "
                 f"backed up from the next backup run; its NAS folder is made by the first NAS backup of {nxt}."),
        occurred_at=now_iso(), audience=AUDIENCE_REPORTS))
    return ACTIVATED


def _not_added(config, configured: str, nxt: str, today: dt.date, activate_on: dt.date, reason: str) -> None:
    """The waiting mail: WARNING on the activation day, ACTION_REQUIRED from the next day (plan section 3)."""
    kind = "WARNING" if today == activate_on else "ACTION_REQUIRED"
    notify(config, AlertEvent(
        key=f"rollover:activation:{nxt}", kind=kind, mode="rollover",
        title=f"{nxt} is not added to the backups yet",
        message=(f"{nxt} is not added to the backups yet: {reason}. Until then only {configured} is backed up, "
                 f"and files saved in the {nxt} folder are not backed up. The check repeats every morning; "
                 "'Run FY rollover now' on the dashboard tries again."),
        occurred_at=now_iso(), fault_code=NOT_ADDED_FAULT,
        cooldown_hours=config.alerts.stale_repeat_hours))
