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
import shutil
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
    new_fy_root_exists,
    write_placeholder_if_empty,
)
from core.guards import MOUNT_MARKER
from core.time_utils import IST, format_ist, get_fy_prefix
from models.config import AppConfig

CURRENT = "current"
EARLY = "early"
OLD = "old"          # a year the rollover switched past that is not yet fully copied by every enabled leg

_CANONICAL_FY = re.compile(r"^FY(\d{2})-(\d{2})$")
_LAST_MONTH_OF_FY = 3        # March: the calendar month in which a financial year ends (April start)
# "FY 27-28", "FY2027-28", "FY_27_28": looks like a year folder (plan 5.2). Case-insensitive, anywhere in the name.
_FY_LOOKING = re.compile(r"FY[\s_-]*\d{2,4}\s*[-_ ]\s*\d{1,4}", re.IGNORECASE)


class SiblingReadError(Exception):
    """The folders next to the year folders could not be read (parent listing, an entry, or the files of a
    candidate folder). 'No findings' would prove nothing then: callers send no mail and no RESOLVED."""


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


def protected_fy_names(config: AppConfig) -> list[str]:
    """The year names the owner's tools (upload_fy.py, restore.py) must never write to: the configured year and,
    while the overlap switch is on, the early next year (R-20). Deliberately ignores the January window and the
    activation row: a tool must not guess whether the early folder is live, it refuses the name outright.
    Empty when the configured folder is not an FY folder at all."""
    configured = _configured(config)
    if not configured:
        return []
    names = [configured]
    if config.rollover.overlap_enabled:
        names.append(next_fy(configured))
    return names


def protected_folders(config: AppConfig) -> list[str]:
    """Source and NAS folders of every protected year (the early folder is a sibling of the configured one,
    whether or not it exists yet)."""
    from pathlib import Path, PureWindowsPath
    out: list[str] = []
    for fy in protected_fy_names(config):
        out.append(str(Path(config.paths.source_drive).parent / fy))
        if config.paths.lan_destination:
            out.append(str(PureWindowsPath(config.paths.lan_destination).parent / fy))
    return out


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
    switched = journal is not None and rj.is_finished(journal["steps"][rj.CONFIG_SWITCH])
    return not switched


FORCED_META = "overlap:forced:{fy}"     # db_meta: the owner forced this activation early (dashboard button)


def _forced(db, fy: str) -> bool:
    """Was `fy` activated by the owner's "Create next year's folder now" button (so it stays active until the
    switch even outside the January..March window)?"""
    try:
        return bool(db.get_meta(FORCED_META.format(fy=fy)))
    except Exception:  # noqa: BLE001 - an unreadable flag only means the normal window rules apply
        return False


def active_folders(config: AppConfig, db, run_date: _date) -> list[str]:
    """FY folders backed up in this run, in run order (new FY first). After the 1 April switch the old
    year stays in the list, last, until every enabled leg has a confirmed full copy of it
    (`unfinished_old_years`, owner decision 2026-11-23): only years named by a finished rollover journal."""
    configured = _configured(config)
    if configured is None:
        return [effective_fy_prefix(config.paths.source_drive)]
    base = [configured]
    if config.rollover.overlap_enabled:
        nxt = next_fy(configured)
        # an activated early year stays active until the switch, even if early_start_month is raised later
        # (owner decision D12 a); in_window itself still honours the month for activation timing
        kept = (calendar_fy(run_date) == configured
                and (run_date.month <= _LAST_MONTH_OF_FY or _forced(db, nxt))
                and db.get_activation(nxt) is not None)
        if in_window(config, run_date) or switch_pending(config, db, run_date) or kept:
            if db.get_activation(nxt) is not None:
                base = [nxt, configured]
    old = [o["fy"] for o in unfinished_old_years(config, db) if o["fy"] not in base]
    return base + old


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
    """'current' for the configured FY, 'early' for the one backed up ahead of the switch, 'old' for a year
    the switch already moved past that is still being copied (never anything else)."""
    configured = _configured(config)
    if fy == configured:
        return CURRENT
    try:
        if configured is not None and fy == next_fy(configured):
            return EARLY
    except ValueError:
        pass
    return OLD


# ---- old years that are not fully copied yet (owner decision 2026-11-23, plan 5.9) ---------------

_META_YEAR_CLOSED = "fy_copy_closed:{fy}"
_LEGS = ("cloud", "lan")
LEG_WORDS = {"cloud": "Cloud", "lan": "LAN"}


def year_end(fy: str) -> pendulum.DateTime:
    """The instant a financial year ends: 1 April 00:00 IST of its second year ('FY26-27' -> 2027-04-01)."""
    m = _CANONICAL_FY.match(fy.strip().upper())
    if not m:
        raise ValueError(f"not a financial-year name like 'FY26-27': {fy!r}")
    return pendulum.datetime(2000 + int(m.group(1)) + 1, 4, 1, tz=IST)


def year_closed(db, fy: str) -> bool:
    """Was this year already settled (fully copied by every enabled leg, then closed)? Once it was, a leg
    switched on later never re-opens it."""
    return bool(db.get_meta(_META_YEAR_CLOSED.format(fy=fy)))


def mark_year_closed(db, fy: str, at: str) -> None:
    db.set_meta(_META_YEAR_CLOSED.format(fy=fy), at)


def enabled_legs(config) -> tuple[str, ...]:
    return tuple(leg for leg in _LEGS if getattr(config, leg).enabled)


def copy_state(config, db, fy: str, confirmed=()) -> dict:
    """Which enabled legs have a CONFIRMED full copy of `fy`: a run of that leg and FY with a status in
    core.statuses.SUCCESS_STATUSES that STARTED after the year ended (a failed, partial, blocked, missing or
    never-run sync does not count). Derived from run_history each time, so a crash or restart loses nothing.
    `confirmed`: legs the caller has just seen succeed (the rollover's own final backups).
    Returns {'fy', 'done': [legs], 'missing': [legs]}; a leg that is off is not required."""
    floor = year_end(fy).isoformat()
    done, missing = [], []
    for leg in enabled_legs(config):
        if leg in confirmed or db.leg_success_after(leg, floor, fy=fy):
            done.append(leg)
        else:
            missing.append(leg)
    return {"fy": fy, "done": done, "missing": missing}


def switched_old_years(config, db) -> list[str]:
    """Years named by a finished config switch (journal from_fy), oldest first, never the configured one
    and never one already settled."""
    configured = _configured(config)
    seen: list[str] = []
    for journal in db.rollover_journals():
        fy = journal["from_fy"]
        if (rj.is_finished(journal["steps"][rj.CONFIG_SWITCH]) and fy != configured
                and fy not in seen and not year_closed(db, fy)):
            seen.append(fy)
    return seen


def unfinished_old_years(config, db, confirmed: dict | None = None) -> list[dict]:
    """[{'fy', 'done', 'missing'}] of every switched-past year that some enabled leg has not fully copied
    yet; oldest first. A read-only derivation: nothing is stored for it. Trouble reading the journals
    raises (callers fall back to the configured year alone and say so)."""
    out = []
    for fy in switched_old_years(config, db):
        state = copy_state(config, db, fy, (confirmed or {}).get(fy, ()))
        if state["missing"]:
            out.append(state)
    return out


def not_fully_copied_text(fy: str, missing) -> str:
    """The one wording the dashboard, the reports and the mails share."""
    names = " and ".join(LEG_WORDS[leg] for leg in missing)
    return f"{fy} is not yet fully copied to {names}"


def not_fully_copied_advice(config, fy: str, missing) -> str:
    """What brings the year to 'fully copied', per missing leg: a nightly leg does it by itself, a leg that is
    started by hand needs a press of its button (the NAS has to be on for LAN)."""
    from core import manual_leg

    parts = []
    for leg in missing:
        if manual_leg.is_manual_only(config, leg):
            extra = " (switch the NAS on first)" if leg == "lan" else ""
            parts.append(f"start a {LEG_WORDS[leg]} backup from the dashboard{extra}; it copies {fy} as well")
        else:
            parts.append(f"the nightly {LEG_WORDS[leg]} backup copies {fy} as well and finishes it")
    return "; ".join(parts)


# ---- activation (OV-2.3) --------------------------------------------------------------------

NOT_NEEDED = "not_needed"
ALREADY_ACTIVE = "already_active"
ACTIVATED = "activated"
DEFERRED = "deferred"

_META_FIRST_DEFERRED = "overlap_first_deferred:{fy}"
_META_ALERT_SENT = "overlap_alert_sent:{fy}"             # when the deferral alert was last mailed
_META_ALERT_REASON = "overlap_alert_reason:{fy}"         # the reason that mail gave (a changed one is mailed at once)
_META_DEFERRED_REASON = "overlap_deferred_reason:{fy}"   # the CURRENT reason of the deferral (dashboard item)
_META_RESOLVED_SENT = "overlap_resolved_sent:{fy}"
_META_CREATED_PENDING = "overlap_created_pending:{fy}"   # folder this app just created, activation not yet recorded
_SECONDS_PER_HOUR = 3600
_UNUSABLE_FOLDER_CODE = "AAM-ROL-06"   # "Files in the new year are not backed up yet": the early folder is refused


def _result(state: str, fy: str | None, reason: str) -> dict:
    logger.info(f"FY overlap activation: {state} ({fy or '-'}): {reason}")
    return {"state": state, "fy": fy, "reason": reason}


def _is_link(path: Path) -> bool:
    """Symlink or Windows junction (a reparse point the office or an installer made)."""
    return os.path.islink(path) or os.path.isjunction(path)


def _default_clock_check(config: AppConfig):
    from core.health import check_clock
    from core.run_artefacts import RunArtefacts

    artefacts = RunArtefacts.ephemeral("overlap-clock")
    try:
        return check_clock(
            max_skew_seconds=config.health.max_clock_skew_seconds,
            connection_timeout=config.health.clock_check_timeout_seconds,
            ntp_server=config.health.ntp_server,
            w32tm_timeout_s=config.health.w32tm_timeout_seconds,
            artefacts=artefacts,
        )
    finally:
        shutil.rmtree(artefacts.root, ignore_errors=True)    # the throwaway store is never kept


def _find_canonical(parent: Path, fy: str) -> Path | None:
    """The child of `parent` named `fy` ignoring case (its real spelling), else None."""
    wanted = fy.upper()
    for entry in parent.iterdir():
        if entry.name.upper() == wanted:
            return entry
    return None


def _defer(db, config, fy: str, reason: str, notify_fn, fault_code: str | None = None,
           immediate: bool = False) -> dict:
    """Not activated now: remember when this started and why, and, after activation_alert_after_hours, tell
    the owner - again every alerts.stale_repeat_hours while it stays deferred (overlap review D3), at once
    when the reason changes. `immediate`: a cause only a person can fix (a link or a file where the folder
    should be): tell the owner on the first refusal, not after the wait (LC2a). Nothing in the folders was touched."""
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
    with contextlib.suppress(Exception):          # only feeds the dashboard item; never stops the alert
        db._set_meta(_META_DEFERRED_REASON.format(fy=fy), reason)
    hours = config.rollover.activation_alert_after_hours
    if immediate or (now - first).total_seconds() > hours * _SECONDS_PER_HOUR:
        sent_raw = db._meta(_META_ALERT_SENT.format(fy=fy))
        last_reason = db._meta(_META_ALERT_REASON.format(fy=fy))
        changed = bool(sent_raw) and bool(last_reason) and last_reason != reason
        due = not sent_raw or changed
        if not due:
            try:
                due = (now - pendulum.parse(sent_raw)).total_seconds() >= config.alerts.stale_repeat_hours * _SECONDS_PER_HOUR
            except Exception:  # noqa: BLE001 - an unreadable marker counts as due
                due = True
        if due:
            _send_deferred_alert(db, config, fy, reason, notify_fn, fault_code)
    return _result(DEFERRED, fy, reason)


def deferred_state(config: AppConfig, db, run_date: _date) -> dict | None:
    """The early FY's activation while it is deferred (junction, no create right, unproven clock ...):
    {'fy', 'reason', 'since', 'alerted'}; None when it is not deferred (off, not the time, or activated)."""
    if not config.rollover.overlap_enabled:
        return None
    configured = _configured(config)
    if configured is None or not (in_window(config, run_date) or switch_pending(config, db, run_date)):
        return None
    fy = next_fy(configured)
    if db.get_activation(fy) is not None:
        return None
    since = db._meta(_META_FIRST_DEFERRED.format(fy=fy))
    if not since:
        return None
    return {"fy": fy, "reason": db._meta(_META_DEFERRED_REASON.format(fy=fy)) or "the reason is not known",
            "since": since, "alerted": bool(db._meta(_META_ALERT_SENT.format(fy=fy)))}


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
                 f"The backup tries again at every run and starts on its own once the problem is gone."
                 + (f" Until it is fixed, {fy} will not be backed up at all, so files saved there exist "
                    f"on the server only." if fault_code == _UNUSABLE_FOLDER_CODE else "")),
        fault_code=fault_code, fy=fy,
        cooldown_hours=0)          # the repeat spacing (alerts.stale_repeat_hours) is decided in _defer
    res = _call_notify(config, db, event, notify_fn)
    # SENT and SUPPRESSED count as told; DISABLED / FAILED / no result are tried again at the next run
    if getattr(res, "status", None) in ("SENT", "SUPPRESSED"):
        db._set_meta(_META_ALERT_SENT.format(fy=fy), pendulum.now(IST).isoformat())
        db._set_meta(_META_ALERT_REASON.format(fy=fy), reason)


def _send_resolved_mail(db, config, fy: str, notify_fn) -> None:
    """ONE RESOLVED mail when an activation that was mailed as deferred finally happens (overlap review D3)."""
    from core.notify import AlertEvent

    if not db._meta(_META_ALERT_SENT.format(fy=fy)) or db._meta(_META_RESOLVED_SENT.format(fy=fy)):
        return
    event = AlertEvent(
        key=f"recovered:overlap:activation:{fy}", kind="RECOVERED", mode="rollover",
        title=f"{fy} is being backed up now",
        message=f"The problem that kept {fy} from being backed up is gone: both year folders are copied every night.",
        status="RECOVERED", resolved_code="AAM-ROL-03", fy=fy)
    res = _call_notify(config, db, event, notify_fn)
    if getattr(res, "status", None) in ("SENT", "SUPPRESSED", "LOGGED_ONLY"):
        db._set_meta(_META_RESOLVED_SENT.format(fy=fy), pendulum.now(IST).isoformat())


def _nightly_words(config) -> str:
    """"the nightly backups" - or what is true when a leg has no nightly run (manual-only, owner 2026-11-23)."""
    from core import manual_leg

    names = {"cloud": "cloud", "lan": "NAS"}
    nightly = [names[leg] for leg in names if manual_leg.is_nightly(config, leg)]
    manual = [names[leg] for leg in names if manual_leg.is_manual_only(config, leg)]
    if not manual:
        return "the nightly backups copy both folders"
    by_hand = f"the {' and '.join(manual)} backup does so whenever it is started by hand"
    if not nightly:
        return f"every backup copies both folders whenever it is started by hand ({by_hand})"
    return f"the nightly {' and '.join(nightly)} backup copies both folders, and {by_hand}"


def _send_start_mail(db, config, fy: str, configured: str, folder: Path, created: bool, when_iso: str,
                     notify_fn) -> None:
    from core.notify import AUDIENCE_REPORTS, AlertEvent

    how = "was created for it" if created else "was already there and is used as it is"
    event = AlertEvent(
        key=f"overlap:start:{fy}", kind="INFO", mode="rollover",
        title=f"{fy} is now backed up as well",
        message=(f"Since {format_ist(when_iso)} {_nightly_words(config)}: {configured} "
                 f"(this year) and {fy} (next year, {folder}). The {fy} folder {how}. "
                 f"You can start saving next year's files in {fy} now; the backup never moves or "
                 f"changes your files. On 1 April the backups switch to {fy} by themselves."),
        audience=AUDIENCE_REPORTS, fy=fy)
    _call_notify(config, db, event, notify_fn)


def ensure_overlap_activated(config: AppConfig, run_date: _date, db=None, *,
                             clock_check=None, notify_fn=None, force: bool = False) -> dict:
    """Start backing up next year's folder when it is time (plan 5.1). Idempotent, needs no backup slot,
    never raises. Returns {'state': not_needed | already_active | activated | deferred, 'fy', 'reason'}.

    clock_check() -> object with .state/.reason (default: health.check_clock from config.health.*);
    notify_fn(config, event, db) (default: core.notify.notify). Every call re-checks a deferred activation.
    `force` (only `force_early_activation`, the owner's dashboard button) skips the January..March / switch
    timing check; every other rule (clock proof, link refusal, adoption, marker) is the same.
    """
    from core.health import CLOCK_OK
    from core.manifest import ManifestDB

    own_db = db is None
    fy = None
    hint = None
    try:
        if db is None:
            db = ManifestDB.from_config(config)
        configured = _configured(config)
        if configured is None or not (force or in_window(config, run_date) or switch_pending(config, db, run_date)):
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
                          notify_fn, fault_code=_UNUSABLE_FOLDER_CODE, immediate=True)
        if existing is not None and not existing.is_dir():
            return _defer(db, config, fy, f"{existing} exists but is not a folder", notify_fn,
                          fault_code=_UNUSABLE_FOLDER_CODE, immediate=True)

        created = existing is None
        pending_key = _META_CREATED_PENDING.format(fy=fy)
        if created:
            hint = (f"the folder {folder} could not be created: {{err}}; "
                    f"give the backup account the right to create folders in {parent}")
            folder.mkdir(exist_ok=False)          # never parents=True: a wrong drive is not invented
            with contextlib.suppress(Exception):
                db._set_meta(pending_key, str(folder))   # a later failure must not turn it into an "adopted" folder
        else:
            pending = db._meta(pending_key)
            if pending and os.path.normcase(os.path.normpath(pending)) == os.path.normcase(os.path.normpath(str(folder))):
                created = True                    # this app made it on an earlier run that did not finish
        hint = (f"the new folder {folder} could not be set up: {{err}}; "
                f"give the backup account the right to create and write files in {folder}")
        write_placeholder_if_empty(folder, fy, config.rollover.source_placeholder_name)

        hint = None                               # what follows is the database, not the folder
        when = pendulum.now(IST).isoformat()
        first = db.record_activation(fy, activated_at=when, source_path=str(folder),
                                     created_by_app=created, clock_state=clock.state)
        with contextlib.suppress(Exception):
            db._set_meta(pending_key, "")
        if not first:
            return _result(ALREADY_ACTIVE, fy, "activated by another run at the same moment")
        _send_start_mail(db, config, fy, configured, folder, created, when, notify_fn)
        _send_resolved_mail(db, config, fy, notify_fn)
        return _result(ACTIVATED, fy, f"{'created' if created else 'adopted'} {folder}")
    except Exception as e:  # noqa: BLE001 - activation must never break a backup flow
        logger.exception("FY overlap activation failed")
        err = f"{type(e).__name__}: {e}"
        reason = hint.replace("{err}", err) if hint else f"activation could not be carried out ({err})"
        if fy is not None and db is not None:
            try:                                  # a failure that repeats is escalated like any other deferral
                return _defer(db, config, fy, reason, notify_fn)
            except Exception:  # noqa: BLE001 - an error inside _defer itself never raises
                logger.exception("FY overlap activation: could not record the deferral")
        return _result(DEFERRED, fy, reason)
    finally:
        if own_db and db is not None:
            with contextlib.suppress(Exception):
                db.close()


# ---- sibling check (OV-2.6, plan 5.2; replaces the ROL-06 "unprotected" alarm) ---------------

def _start_year_is_newer(name: str, configured: str) -> bool:
    """Does the year pair read from a FY-looking `name` start AFTER the configured FY (so it could be next year
    or later)? 2-digit years are 20xx, compared modulo 100 so FY99-00 -> FY00-01 works (up to 50 years ahead);
    4-digit years are compared as they are. A pair that cannot be read counts as newer (better one alarm too many)."""
    m = _FY_LOOKING.search(name)
    nums = re.findall(r"\d+", m.group(0)) if m else []
    cfg = _CANONICAL_FY.match(configured.upper())
    if len(nums) < 2 or cfg is None or len(nums[0]) not in (2, 4):
        return True
    cfg_start = int(cfg.group(1))
    if len(nums[0]) == 4:
        return int(nums[0]) > 2000 + cfg_start
    return 1 <= (int(nums[0]) - cfg_start) % 100 <= 50


def sibling_findings(config: AppConfig, active: list[str], db=None, *, count: bool = True) -> list[dict]:
    """Folders next to the year folders that are NOT backed up although the office probably thinks they are.
    Pure read of the direct children of parent(paths.source_drive); never writes, never follows links.

    kind 'misnamed': the name looks like a year folder (`FY 27-28`, `FY2027-28`, `FY_27_28`) but is not the
    canonical `FYnn-nn` (any case) AND its years start after the configured FY (a name pointing at the
    configured year or the past, like an archive, is ignored), and the name it would be renamed to is not
    already a folder. kind 'unprotected': a canonical folder of a NEWER FY than the configured one,
    not in `active`, holding real files (placeholder and system files do not count). Past FYs, the configured FY,
    the active FYs and `rollover.sibling_ignore` are never reported.

    Returns [{'kind', 'name', 'fy' (canonical FY of an unprotected one, else None), 'files'}], sorted by name.
    `count=False` (dashboard refresh) stops at the first file: `files` is then None. `db` is accepted for the
    callers' symmetry and is not used.

    Raises SiblingReadError when the parent cannot be listed, an entry cannot be classified, or the files of a
    candidate folder cannot be read: a silent 'nothing found' would end the alarm with a false RESOLVED."""
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
        raise SiblingReadError(f"cannot list {parent}: {e}") from e
    names_up = {e.name.upper() for e in entries}
    for entry in entries:
        name = entry.name
        if name.lower() in ignore:
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError as e:
            raise SiblingReadError(f"cannot tell whether {name} is a folder: {e}") from e
        if _CANONICAL_FY.match(name.upper()):
            fy = name.upper()
            if fy in active_up or fy == configured.upper():
                continue
            if classify_rollover(configured, fy) not in (FORWARD, JUMP):
                continue                                   # a past FY: ignored (plan 5.2)
            if not new_fy_root_exists(config.paths.source_drive, name):
                continue                                   # a link (or gone): not a folder of files
            if count:
                files = new_fy_file_count(config.paths.source_drive, name, placeholder)
                if files is None:
                    raise SiblingReadError(f"cannot read the files of {name}")
                if not files:
                    continue                               # only the placeholder / system files
            else:
                has_files = new_fy_has_files(config.paths.source_drive, name, placeholder)
                if has_files is None:
                    raise SiblingReadError(f"cannot read the files of {name}")
                if not has_files:
                    continue
                files = None
            found.append({"kind": SIBLING_UNPROTECTED, "name": name, "fy": fy, "files": files})
        elif _FY_LOOKING.search(name):
            if not _start_year_is_newer(name, configured):
                continue                                   # the configured year or the past: an archive, not a new year
            if suggested_name(name).upper() in names_up:
                continue                                   # the right name is taken: nothing to rename it to
            found.append({"kind": SIBLING_MISNAMED, "name": name, "fy": None, "files": None})
    return found


def parent_path(config: AppConfig) -> Path:
    """The folder that holds the year folders."""
    return Path(_parent_path(config.paths.source_drive))


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
    return f"FY{nums[0][-2:].zfill(2)}-{nums[1][-2:].zfill(2)}"


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
        resolved = db.last_notification(f"recovered:{key}", status=("SENT", "LOGGED_ONLY"))
        if resolved is None or resolved["id"] < last_id:
            open_ones.append((key, last_id))
    return open_ones


# ---- NAS folder (OV-2.5) --------------------------------------------------------------------

def ensure_overlap_nas_folder(config: AppConfig, fy: str, *, established: bool = False) -> dict:
    """The NAS folder of `fy` for the LAN flow (NAS awake, before the first pipeline). Never raises.
    state: created | ok | marked | needs_confirmation | unreachable. Nothing is ever created above the
    share root; a non-empty folder without the marker is left for guard G5 to ask about.
    `established` (the year already has a LAN success): a vanished folder or an empty one without the
    marker is NOT re-created or re-marked; it returns needs_confirmation so G5 asks (AAM-NAS-09/10)."""
    nas = None
    try:
        if not config.lan.enabled:
            return {"state": "unreachable", "fy": fy, "path": None, "reason": "the NAS leg is switched off"}
        nas = Path(folder_view(config, fy).paths.lan_destination)
        if not nas.parent.is_dir():
            return {"state": "unreachable", "fy": fy, "path": str(nas),
                    "reason": f"the share {nas.parent} cannot be reached"}
        if not nas.exists():
            if established:
                return {"state": "needs_confirmation", "fy": fy, "path": str(nas),
                        "reason": "NAS folder of an established year vanished: nothing created (guard G5 asks)"}
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
            if established:
                return {"state": "needs_confirmation", "fy": fy, "path": str(nas),
                        "reason": "empty folder without marker in an established year: nothing written "
                                  "(guard G5 asks)"}
            (nas / MOUNT_MARKER).touch()
            return {"state": "marked", "fy": fy, "path": str(nas), "reason": "empty folder, marker written"}
        return {"state": "needs_confirmation", "fy": fy, "path": str(nas),
                "reason": "folder holds files but has no marker: left untouched (guard G5 asks)"}
    except Exception as e:  # noqa: BLE001 - the pipeline's own checks report the real problem
        logger.warning(f"FY overlap NAS folder check for {fy} failed: {e}")
        return {"state": "unreachable", "fy": fy, "path": str(nas) if nas else None,
                "reason": f"{type(e).__name__}: {e}"}


# ---- forced early activation (owner decision 2026-11-23, Settings button) ------------------------

REFUSED = "refused"


def _early_result(state: str, fy: str | None, message: str, nas: dict | None = None) -> dict:
    return {"state": state, "fy": fy, "message": message, "nas": nas}


def early_state(config: AppConfig, db, run_date: _date) -> dict:
    """What the Settings button "Create next year's folder now" may do: {'can_run', 'state', 'fy', 'message'}.
    state is 'ready', 'already_active' or 'refused'. Pure reading, creates nothing."""
    configured = _configured(config)
    if configured is None:
        return {"can_run": False, "state": REFUSED, "fy": None,
                "message": "The source / NAS folders do not end in an FY folder (e.g. FY26-27), so there is "
                           "no next year's folder to create."}
    fy = next_fy(configured)
    if not config.rollover.overlap_enabled:
        return {"can_run": False, "state": REFUSED, "fy": fy,
                "message": "Switched off: rollover.overlap_enabled is false in config.yaml on the server."}
    activation = db.get_activation(fy)
    if activation is not None:
        return {"can_run": False, "state": "already_active", "fy": fy,
                "message": f"{fy} is already backed up as well (since "
                           f"{format_ist(activation.get('activated_at'))}). Nothing to do."}
    if calendar_fy(run_date) != configured and not switch_pending(config, db, run_date):
        return {"can_run": False, "state": REFUSED, "fy": fy,
                "message": f"Not possible: the server date belongs to {calendar_fy(run_date)}, not to the "
                           f"backups' year {configured}. Check the server date, or use Run FY rollover now."}
    return {"can_run": True, "state": "ready", "fy": fy,
            "message": f"{fy} is not backed up yet. It can be started now instead of on 1 January."}


def _misnamed_next_year(config: AppConfig, fy: str) -> str | None:
    """A folder next to the year folders (source and, when the NAS leg is on, the NAS) that is clearly next
    year's but spelled in a way the backup does not use ('FY 27-28', 'FY2027-28'): its path, else None."""
    parents = [Path(_parent_path(config.paths.source_drive))]
    if config.lan.enabled:
        parents.append(Path(_parent_path(config.paths.lan_destination)))
    for parent in parents:
        if not parent.is_dir():
            continue
        for entry in parent.iterdir():
            if (entry.name.upper() != fy.upper() and _FY_LOOKING.search(entry.name)
                    and suggested_name(entry.name).upper() == fy.upper()):
                return str(entry)
    return None


def force_early_activation(config: AppConfig, db, *, run_date: _date | None = None,
                           clock_check=None, notify_fn=None) -> dict:
    """Start backing up next year's folder NOW (the owner's button), through the same code as the normal
    January activation (`ensure_overlap_activated`, then `ensure_overlap_nas_folder`). Idempotent. Never
    wakes the NAS, never switches the year, never deletes. Never raises.
    Returns {'state': activated | already_active | refused, 'fy', 'message', 'nas'}."""
    try:
        run_date = run_date or globals()["run_date"]()
        st = early_state(config, db, run_date)
        fy = st["fy"]
        if st["state"] == "already_active":
            return _early_result(ALREADY_ACTIVE, fy, st["message"])
        if not st["can_run"]:
            return _early_result(REFUSED, fy, st["message"])
        wrong = _misnamed_next_year(config, fy)
        if wrong:
            return _early_result(REFUSED, fy, f"Not started: {wrong} looks like next year's folder but is not "
                                              f"named {fy} (capital FY, two-digit years, a dash, no spaces). "
                                              f"Rename it to {fy} and press again.")
        if config.lan.enabled:
            nas_parent = Path(folder_view(config, fy).paths.lan_destination).parent
            if not nas_parent.is_dir():
                return _early_result(REFUSED, fy, f"Not started: the NAS share {nas_parent} cannot be reached. "
                                                  "Switch the NAS on and press again (this button never "
                                                  "wakes the NAS). Nothing was changed.")
        from core.health import CLOCK_OK
        clock = (clock_check or (lambda: _default_clock_check(config)))()
        if clock.state != CLOCK_OK:
            return _early_result(REFUSED, fy, "Not started: the server's clock could not be proven correct "
                                              f"({clock.state}: {clock.reason}). Nothing was changed.")
        res = ensure_overlap_activated(config, run_date, db, clock_check=lambda: clock, notify_fn=notify_fn,
                                       force=True)
        if res["state"] == ALREADY_ACTIVE:
            return _early_result(ALREADY_ACTIVE, fy, f"{fy} is already backed up as well. Nothing changed.")
        if res["state"] != ACTIVATED:
            return _early_result(REFUSED, fy, f"Not started: {res['reason']}. The backup keeps retrying; "
                                              "fix the cause and press again.")
        with contextlib.suppress(Exception):
            db.set_meta(FORCED_META.format(fy=fy), pendulum.now(IST).isoformat())
        nas = ensure_overlap_nas_folder(config, fy) if config.lan.enabled else None
        text = (f"{fy} is now backed up as well as {_configured(config)}: {res['reason']}. The year is not "
                "switched and nothing is deleted; the cloud folder appears at the next backup.")
        if nas is not None:
            if nas["state"] == "needs_confirmation":
                text += (f" The NAS folder {nas['path']} holds files but has no {MOUNT_MARKER} marker, so it was "
                         "left untouched (marker not written) and the NAS backup of that year will stop and ask "
                         "(it cannot be approved from the dashboard: check that it is the right folder).")
            elif nas["state"] == "unreachable":
                text += f" The NAS folder could not be prepared now ({nas['reason']}); the NAS backup does it."
            else:
                text += f" NAS folder: {nas['reason']}."
        return _early_result(ACTIVATED, fy, text, nas)
    except Exception as e:  # noqa: BLE001 - a button press never raises into the dashboard
        logger.exception("Forced FY activation failed")
        return _early_result(REFUSED, None, f"Not started: {type(e).__name__}: {e}")
