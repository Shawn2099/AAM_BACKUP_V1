"""FY overlap building blocks (plan docs/PLAN_FY_OVERLAP_2026-11-20.md, section 3 and 5.1; task OV-2.2).

Pure decisions about which financial-year folders are backed up in one flow run. Nothing here
writes anything; the only reads are the manifest DB passed in. The clock is never read implicitly
except by `run_date()` (taken ONCE per flow run and handed to every function here).

Words (plan section 3): configured FY = leaf of paths.source_drive; next FY = the FY after it;
calendar FY = get_fy_prefix(date); in window = calendar FY is the configured one and the month is
between rollover.early_start_month and March; switch pending = calendar FY is the next FY and the
rollover journal has not finished its config_switch step; active set = [next FY, configured FY]
(new first = run order) when overlap is on, in window or switch pending, and the next FY has a
durable activation row, otherwise [configured FY].
"""

from __future__ import annotations

import calendar
import contextlib
import os
import re
from datetime import date as _date
from datetime import datetime
from pathlib import Path

import pendulum
from loguru import logger

from core import rollover_journal as rj
from core.fy_rollover import (
    FORWARD,
    JUMP,
    _child_path,
    _parent_path,
    classify_rollover,
    configured_fy,
    effective_fy_prefix,
    folder_entry_names,
    new_fy_file_count,
    new_fy_has_files,
    write_placeholder_if_empty,
)
from core.guards import MOUNT_MARKER
from core.time_utils import IST, format_ist, get_fy_prefix
from models.config import AppConfig

CURRENT = "current"
EARLY = "early"

_CANONICAL_FY = re.compile(r"^FY(\d{2})-(\d{2})$")
_LAST_MONTH_OF_FY = 3        # March: the calendar month in which a financial year ends (April start)
# "FY 27-28", "FY2027-28", "FY_27_28": looks like a year folder (plan 5.2). Case-insensitive, anywhere in the name.
_FY_LOOKING = re.compile(r"FY[\s_-]*\d{2,4}\s*[-_ ]\s*\d{2,4}", re.IGNORECASE)

SIBLING_MISNAMED = "misnamed"
SIBLING_UNPROTECTED = "unprotected"
SIBLING_KEY_PREFIX = "overlap:sibling:"          # + folder name (misnamed)
UNPROTECTED_KEY_PREFIX = "overlap:unprotected:"  # + canonical FY (unprotected)


def run_date(scheduled: datetime | None = None) -> pendulum.Date:
    """The IST date of a run: of `scheduled` when given (a late scheduled run keeps its own day),
    else of now. Naive datetimes are taken as IST."""
    if scheduled is None:
        return pendulum.now(IST).date()
    if scheduled.tzinfo is None:
        moment = pendulum.instance(scheduled, tz=IST)
    else:
        moment = pendulum.instance(scheduled).in_timezone(IST)
    return moment.date()


def next_fy(fy: str) -> str:
    """'FY26-27' -> 'FY27-28'; 'FY99-00' -> 'FY00-01'. ValueError for anything not canonical."""
    m = _CANONICAL_FY.match(fy.strip().upper()) if isinstance(fy, str) else None
    if not m:
        raise ValueError(f"not a financial-year name like 'FY26-27': {fy!r}")
    start, end = int(m.group(1)), int(m.group(2))
    if end != (start + 1) % 100:
        raise ValueError(f"not a consecutive financial-year name: {fy!r}")
    nxt = (start + 1) % 100
    return f"FY{nxt:02d}-{(nxt + 1) % 100:02d}"


def calendar_fy(date: _date) -> str:
    """The FY the calendar says for this date (April start)."""
    return get_fy_prefix(date)


def _configured(config: AppConfig) -> str | None:
    return configured_fy(config.paths.source_drive, config.paths.lan_destination)


def in_window(config: AppConfig, run_date: _date) -> bool:
    """Overlap on, the calendar year is still the configured one, and the month is early_start_month..March."""
    if not config.rollover.overlap_enabled:
        return False
    configured = _configured(config)
    if configured is None or calendar_fy(run_date) != configured:
        return False
    return config.rollover.early_start_month <= run_date.month <= _LAST_MONTH_OF_FY


def switch_pending(config: AppConfig, db, run_date: _date) -> bool:
    """The calendar has moved exactly one FY past the configured one and the config switch has not
    happened yet (no journal, or its config_switch step is not done). Open follow-up steps do not count.
    A jump (more than one FY) or a clock behind is never pending. Needs no clock proof."""
    if not config.rollover.overlap_enabled:
        return False
    configured = _configured(config)
    if configured is None:
        return False
    nxt = next_fy(configured)
    if calendar_fy(run_date) != nxt:
        return False
    journal = db.rollover_get(configured, nxt)
    switched = journal is not None and journal["steps"][rj.CONFIG_SWITCH]["state"] == rj.DONE
    return not switched


def active_folders(config: AppConfig, db, run_date: _date) -> list[str]:
    """FY folders backed up in this run, in run order (new FY first)."""
    configured = _configured(config)
    if configured is None:
        return [effective_fy_prefix(config.paths.source_drive)]
    if config.rollover.overlap_enabled and (in_window(config, run_date) or switch_pending(config, db, run_date)):
        nxt = next_fy(configured)
        if db.get_activation(nxt) is not None:
            return [nxt, configured]
    return [configured]


def folder_for(config: AppConfig, fy: str) -> str:
    """The source folder of that FY (the configured FY's is paths.source_drive itself)."""
    if fy == _configured(config):
        return config.paths.source_drive
    return _child_path(_parent_path(config.paths.source_drive), fy)


def folder_view(config: AppConfig, fy: str) -> AppConfig:
    """The config as seen by one FY's pipeline: source (and NAS folder when the LAN leg is on) point at
    that FY's folders. The configured FY gets the config itself; other FYs a deep copy (never mutates `config`)."""
    if fy == _configured(config):
        return config
    view = config.model_copy(deep=True)
    view.paths.source_drive = _child_path(_parent_path(config.paths.source_drive), fy)
    if config.lan.enabled:
        view.paths.lan_destination = _child_path(_parent_path(config.paths.lan_destination), fy)
    return view


def role_of(config: AppConfig, fy: str) -> str:
    """'current' for the configured FY, 'early' for the one backed up ahead of the switch."""
    return CURRENT if fy == _configured(config) else EARLY


# ---- activation (OV-2.3) --------------------------------------------------------------------

NOT_NEEDED = "not_needed"
ALREADY_ACTIVE = "already_active"
ACTIVATED = "activated"
DEFERRED = "deferred"

_META_FIRST_DEFERRED = "overlap_first_deferred:{fy}"
_META_ALERT_SENT = "overlap_alert_sent:{fy}"
_SECONDS_PER_HOUR = 3600


def _result(state: str, fy: str | None, reason: str) -> dict:
    logger.info(f"FY overlap activation: {state} ({fy or '-'}): {reason}")
    return {"state": state, "fy": fy, "reason": reason}


def _is_link(path: Path) -> bool:
    """Symlink or Windows junction (a reparse point the office or an installer made)."""
    return os.path.islink(path) or os.path.isjunction(path)


def _default_clock_check(config: AppConfig):
    from core.health import check_clock
    from core.run_artefacts import RunArtefacts

    return check_clock(
        max_skew_seconds=config.health.max_clock_skew_seconds,
        connection_timeout=config.health.clock_check_timeout_seconds,
        ntp_server=config.health.ntp_server,
        w32tm_timeout_s=config.health.w32tm_timeout_seconds,
        artefacts=RunArtefacts.ephemeral("overlap-clock"),
    )


def _find_canonical(parent: Path, fy: str) -> Path | None:
    """The child of `parent` named `fy` ignoring case (its real spelling), else None."""
    wanted = fy.upper()
    for entry in parent.iterdir():
        if entry.name.upper() == wanted:
            return entry
    return None


def _defer(db, config, fy: str, reason: str, notify_fn, fault_code: str | None = None) -> dict:
    """Not activated now: remember when this started and, after activation_alert_after_hours, tell
    the owner ONCE. Nothing in the folders was touched."""
    now = pendulum.now(IST)
    key = _META_FIRST_DEFERRED.format(fy=fy)
    first_raw = db._meta(key)
    first = None
    try:
        first = pendulum.parse(first_raw) if first_raw else None
    except Exception:  # noqa: BLE001 - an unreadable marker is replaced
        first = None
    if first is None:
        first = now
        db._set_meta(key, now.isoformat())
    hours = config.rollover.activation_alert_after_hours
    if (now - first).total_seconds() > hours * _SECONDS_PER_HOUR and not db._meta(_META_ALERT_SENT.format(fy=fy)):
        _send_deferred_alert(db, config, fy, reason, notify_fn, fault_code)
    return _result(DEFERRED, fy, reason)


def _call_notify(config, db, event, notify_fn):
    """Send through `notify_fn` (default core.notify.notify); a failing mail never raises."""
    try:
        if notify_fn is None:
            from core.notify import notify as notify_fn
        return notify_fn(config, event, db)
    except Exception as e:  # noqa: BLE001 - mail trouble must not undo or block anything
        logger.error(f"FY overlap mail {event.key} failed: {e}")
        return None


def _send_deferred_alert(db, config, fy: str, reason: str, notify_fn, fault_code: str | None) -> None:
    from core.notify import AlertEvent

    configured = _configured(config)
    event = AlertEvent(
        key=f"overlap:activation:{fy}", kind="ACTION_REQUIRED", mode="rollover",
        title=f"{fy} is not being backed up yet",
        message=(f"Next year's folder ({fy}) is not being backed up because {reason}. "
                 f"{configured} is still backed up every night as usual, so nothing is lost. "
                 f"The backup tries again at every run and starts on its own once the problem is gone."),
        fault_code=fault_code, fy=fy)
    res = _call_notify(config, db, event, notify_fn)
    if getattr(res, "status", None) != "FAILED":     # a failed mail is tried again at the next run
        db._set_meta(_META_ALERT_SENT.format(fy=fy), pendulum.now(IST).isoformat())


def _send_start_mail(db, config, fy: str, configured: str, folder: Path, created: bool, when_iso: str,
                     notify_fn) -> None:
    from core.notify import AUDIENCE_REPORTS, AlertEvent

    how = "was created for it" if created else "was already there and is used as it is"
    event = AlertEvent(
        key=f"overlap:start:{fy}", kind="INFO", mode="rollover",
        title=f"{fy} is now backed up as well",
        message=(f"Since {format_ist(when_iso)} the nightly backups copy both folders: {configured} "
                 f"(this year) and {fy} (next year, {folder}). The {fy} folder {how}. "
                 f"You can start saving next year's files in {fy} now; the backup never moves or "
                 f"changes your files. On 1 April the backups switch to {fy} by themselves."),
        audience=AUDIENCE_REPORTS, fy=fy)
    _call_notify(config, db, event, notify_fn)


def ensure_overlap_activated(config: AppConfig, run_date: _date, db=None, *,
                             clock_check=None, notify_fn=None) -> dict:
    """Start backing up next year's folder when it is time (plan 5.1). Idempotent, needs no backup slot,
    never raises. Returns {'state': not_needed | already_active | activated | deferred, 'fy', 'reason'}.

    clock_check() -> object with .state/.reason (default: health.check_clock from config.health.*);
    notify_fn(config, event, db) (default: core.notify.notify). Every call re-checks a deferred activation.
    """
    from core.health import CLOCK_OK
    from core.manifest import ManifestDB

    own_db = db is None
    fy = None
    try:
        if db is None:
            db = ManifestDB.from_config(config)
        configured = _configured(config)
        if configured is None or not (in_window(config, run_date) or switch_pending(config, db, run_date)):
            return _result(NOT_NEEDED, None, "overlap is off or it is not the time for it")
        fy = next_fy(configured)
        if db.get_activation(fy) is not None:
            return _result(ALREADY_ACTIVE, fy, "already activated")

        clock = (clock_check or (lambda: _default_clock_check(config)))()
        if clock.state != CLOCK_OK:
            return _defer(db, config, fy,
                          f"the server's clock could not be proven correct ({clock.state}: {clock.reason})",
                          notify_fn, fault_code="AAM-ROL-03")

        parent = Path(_parent_path(config.paths.source_drive))
        if not parent.is_dir():
            return _defer(db, config, fy, f"the folder {parent} that should hold it cannot be reached", notify_fn)
        existing = _find_canonical(parent, fy)
        folder = existing if existing is not None else Path(folder_for(config, fy))
        if existing is not None and _is_link(existing):
            return _defer(db, config, fy,
                          f"{existing} is a shortcut (link), not a real folder, and the backup refuses to use it",
                          notify_fn)
        if existing is not None and not existing.is_dir():
            return _defer(db, config, fy, f"{existing} exists but is not a folder", notify_fn)

        created = existing is None
        if created:
            folder.mkdir(exist_ok=False)          # never parents=True: a wrong drive is not invented
        write_placeholder_if_empty(folder, fy, config.rollover.source_placeholder_name)

        when = pendulum.now(IST).isoformat()
        first = db.record_activation(fy, activated_at=when, source_path=str(folder),
                                     created_by_app=created, clock_state=clock.state)
        if not first:
            return _result(ALREADY_ACTIVE, fy, "activated by another run at the same moment")
        _send_start_mail(db, config, fy, configured, folder, created, when, notify_fn)
        return _result(ACTIVATED, fy, f"{'created' if created else 'adopted'} {folder}")
    except Exception as e:  # noqa: BLE001 - activation must never break a backup flow
        logger.exception("FY overlap activation failed")
        return _result(DEFERRED, fy, f"activation could not be carried out ({type(e).__name__}: {e})")
    finally:
        if own_db and db is not None:
            with contextlib.suppress(Exception):
                db.close()


# ---- sibling check (OV-2.6, plan 5.2; replaces the ROL-06 "unprotected" alarm) ---------------

def sibling_findings(config: AppConfig, active: list[str], db=None, *, count: bool = True) -> list[dict]:
    """Folders next to the year folders that are NOT backed up although the office probably thinks they are.
    Pure read of the direct children of parent(paths.source_drive); never writes, never follows links.

    kind 'misnamed': the name looks like a year folder (`FY 27-28`, `FY2027-28`, `FY_27_28`) but is not the
    canonical `FYnn-nn` (any case). kind 'unprotected': a canonical folder of a NEWER FY than the configured one,
    not in `active`, holding real files (placeholder and system files do not count). Past FYs, the configured FY,
    the active FYs and `rollover.sibling_ignore` are never reported.

    Returns [{'kind', 'name', 'fy' (canonical FY of an unprotected one, else None), 'files'}], sorted by name.
    `count=False` (dashboard refresh) stops at the first file: `files` is then None. `db` is accepted for the
    callers' symmetry and is not used."""
    configured = _configured(config)
    parent = Path(_parent_path(config.paths.source_drive))
    if configured is None or not parent.is_dir():
        return []
    ignore = {n.lower() for n in config.rollover.sibling_ignore}
    active_up = {a.upper() for a in active}
    placeholder = config.rollover.source_placeholder_name
    found: list[dict] = []
    try:
        entries = sorted(os.scandir(parent), key=lambda e: e.name.lower())
    except OSError as e:
        logger.warning(f"Sibling check: cannot list {parent}: {e}")
        return []
    for entry in entries:
        name = entry.name
        try:
            if name.lower() in ignore or not entry.is_dir():
                continue
        except OSError:
            continue
        if _CANONICAL_FY.match(name.upper()):
            fy = name.upper()
            if fy in active_up or fy == configured.upper():
                continue
            if classify_rollover(configured, fy) not in (FORWARD, JUMP):
                continue                                   # a past FY: ignored (plan 5.2)
            if count:
                files = new_fy_file_count(config.paths.source_drive, name, placeholder)
                if not files:
                    continue                               # missing / link / only the placeholder
            elif new_fy_has_files(config.paths.source_drive, name, placeholder):
                files = None
            else:
                continue
            found.append({"kind": SIBLING_UNPROTECTED, "name": name, "fy": fy, "files": files})
        elif _FY_LOOKING.search(name):
            found.append({"kind": SIBLING_MISNAMED, "name": name, "fy": None, "files": None})
    return found


def parent_readable(config: AppConfig) -> bool:
    """Can the folder holding the year folders be listed? When not, 'no findings' proves nothing."""
    return Path(_parent_path(config.paths.source_drive)).is_dir()


def sibling_key(finding: dict) -> str:
    """The alert / attention key of a finding (also the incident key in the notifications table)."""
    if finding["kind"] == SIBLING_MISNAMED:
        return f"{SIBLING_KEY_PREFIX}{finding['name']}"
    return f"{UNPROTECTED_KEY_PREFIX}{finding['fy']}"


def suggested_name(name: str) -> str:
    """'FY 27-28' / 'FY2027-28' / 'FY_27_28' -> 'FY27-28' (the last two digits of both years)."""
    m = _FY_LOOKING.search(name)
    nums = re.findall(r"\d+", m.group(0)) if m else []
    if len(nums) < 2:
        return "FYnn-nn"
    return f"FY{nums[0][-2:]}-{nums[1][-2:]}"


def _unprotected_reason(config: AppConfig, run_date: _date, db) -> str:
    if not config.rollover.overlap_enabled:
        return "backing up next year's folder in advance is switched off in the settings"
    try:
        timely = in_window(config, run_date) or (db is not None and switch_pending(config, db, run_date))
    except Exception:  # noqa: BLE001 - the wording must never break the alarm
        timely = False
    if timely:
        return ("it has not been set up for backup yet (the server could not do that so far; "
                "it is tried again at every backup run)")
    month = calendar.month_name[config.rollover.early_start_month]
    return f"next year's folder is only backed up from {month} on"


def sibling_text(finding: dict, config: AppConfig, run_date: _date, db=None) -> tuple[str, str]:
    """(title, message) in plain words, shared by the daily mail and the dashboard item."""
    if finding["kind"] == SIBLING_MISNAMED:
        name, good = finding["name"], suggested_name(finding["name"])
        return (f'The folder "{name}" is not backed up',
                f'The folder "{name}" looks like a new financial-year folder but is not named {good}, so it is '
                f"NOT backed up. Rename it to {good}.")
    fy, configured = finding["fy"], _configured(config) or "this year's folder"
    saved = f"{finding['files']} file(s) were" if finding.get("files") else "Files were"
    return (f"Files in {fy} are not backed up yet",
            f"{saved} saved in {fy}, but they are NOT backed up yet: {_unprotected_reason(config, run_date, db)}. "
            f"Until then keep working in {configured} instead. Do not move files out of {configured} "
            "meanwhile: the next backup would remove them from the backup copies too.")


def open_sibling_alerts(db) -> list[tuple[str, int]]:
    """(event key, id of its newest SENT mail) of every sibling alarm mail that was delivered and not yet
    followed by a delivered RESOLVED mail of the same key. Read-only."""
    with db._lock:
        rows = db._get_conn().execute(
            "SELECT event_key, MAX(id) AS last_id FROM notifications WHERE status = 'SENT' "
            "AND (event_key LIKE ? OR event_key LIKE ?) GROUP BY event_key",
            (f"{SIBLING_KEY_PREFIX}%", f"{UNPROTECTED_KEY_PREFIX}%")).fetchall()
    open_ones = []
    for row in rows:
        key, last_id = row[0], row[1]
        resolved = db.last_notification(f"recovered:{key}", status="SENT")
        if resolved is None or resolved["id"] < last_id:
            open_ones.append((key, last_id))
    return open_ones


# ---- NAS folder (OV-2.5) --------------------------------------------------------------------

def ensure_overlap_nas_folder(config: AppConfig, fy: str) -> dict:
    """The NAS folder of `fy` for the LAN flow (NAS awake, before the first pipeline). Never raises.
    state: created | ok | marked | needs_confirmation | unreachable. Nothing is ever created above the
    share root; a non-empty folder without the marker is left for guard G5 to ask about."""
    nas = None
    try:
        if not config.lan.enabled:
            return {"state": "unreachable", "fy": fy, "path": None, "reason": "the NAS leg is switched off"}
        nas = Path(folder_view(config, fy).paths.lan_destination)
        if not nas.parent.is_dir():
            return {"state": "unreachable", "fy": fy, "path": str(nas),
                    "reason": f"the share {nas.parent} cannot be reached"}
        if not nas.exists():
            nas.mkdir(exist_ok=False)             # never parents=True
            (nas / MOUNT_MARKER).touch()
            return {"state": "created", "fy": fy, "path": str(nas), "reason": "folder and marker created"}
        if not nas.is_dir():
            return {"state": "needs_confirmation", "fy": fy, "path": str(nas),
                    "reason": "exists but is not a folder"}
        names = [n.lower() for n in folder_entry_names(nas)]
        if MOUNT_MARKER.lower() in names:
            return {"state": "ok", "fy": fy, "path": str(nas), "reason": "marker present"}
        if not names:
            (nas / MOUNT_MARKER).touch()
            return {"state": "marked", "fy": fy, "path": str(nas), "reason": "empty folder, marker written"}
        return {"state": "needs_confirmation", "fy": fy, "path": str(nas),
                "reason": "folder holds files but has no marker: left untouched (guard G5 asks)"}
    except Exception as e:  # noqa: BLE001 - the pipeline's own checks report the real problem
        logger.warning(f"FY overlap NAS folder check for {fy} failed: {e}")
        return {"state": "unreachable", "fy": fy, "path": str(nas) if nas else None,
                "reason": f"{type(e).__name__}: {e}"}
