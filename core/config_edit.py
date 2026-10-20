"""Edit config.yaml from the dashboard (owner decision DEC-9, plan 4.7).

Rules (owner, non-negotiable - reliability, no guesses, a human decides):
  * Nothing is written unless the whole candidate file loads exactly as the
    services will load it (`load_config`: strict keys, bounds, the WP14
    schedule/run-budget check). Errors come back per key, in plain words.
  * Risky keys - WHAT or WHERE is backed up, data-safety guards, NAS power -
    need a typed confirmation phrase, like an approval.
  * Lock-out keys (dashboard sign-in, bind address/port; runtime/database/log
    paths) are never editable here: a mistake there locks everyone out of
    the dashboard or orphans the database.
  * Secrets are write-only: never returned, never put in the history.
  * Refused while a backup / FY rollover holds the backup lock (the rollover
    rewrites config.yaml itself).
  * Write = timestamped backup + atomic, comment-preserving replace (shared
    with the FY rollover, core.config_file), then the result is loaded once
    more; if that ever fails the backup is put back.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError

from core import approvals, messages
from core.config_help import SECTION_TITLES, label_and_help
from core.config_file import (ConfigWriteError, config_transaction, load_round_trip,
                               prune_backups, set_dotted)

LOCKED = frozenset({
    "dashboard.bind_address", "dashboard.port",
    "dashboard.admin_password_hash", "dashboard.login_password_hash",
    "dashboard.firewall_remote_addresses",
    "paths.runtime_dir", "paths.database_path", "paths.log_directory",
})
SECRET = frozenset({"notifications.smtp_password"})
# Never listed at all: changed only through "Change admin password".
HIDDEN = frozenset({"dashboard.admin_password_hash", "dashboard.login_password_hash"})
# the whole email section: it decides who hears about a failure (owner 2026-10-19)
_RISKY_SECTIONS = frozenset({"guards", "approvals", "notifications"})
_RISKY_KEYS = frozenset({
    # what / where is backed up
    "paths.source_drive", "paths.lan_destination", "paths.gcs_key_path",
    "cloud.enabled", "cloud.bucket", "cloud.project_number", "cloud.location",
    "cloud.storage_class", "lan.enabled", "rollover.source_placeholder_name",
    # data safety
    "lan.use_backup_mode", "health.clock_unverified_policy", "cloud.write_probe_enabled",
    # what counts as "unchanged": a wide window skips same-size edits
    "cloud.modify_window_seconds", "audit.modify_window_seconds",
    # silences routine failure emails (decisions/overdue are always sent)
    "notifications.send_on_failure",
    # NAS power
    "wol.enabled", "wol.server_ip", "wol.mac_address", "wol.broadcast_address",
    "lan.shutdown_after_backup", "lan.shutdown_on_all_retries_exhausted",
})

# Read once when the backup service starts (serve.py / launch.py), not at every run.
_START_ONLY_KEYS = frozenset({"cloud.enabled", "lan.enabled"})

NEXT_RUN = "next_run"
AGENT_RESTART = "agent_restart"
WATCHDOG_RELOAD = "watchdog_reload"
DASHBOARD = "dashboard"
EFFECT_TEXT = {
    NEXT_RUN: "Takes effect from the next backup.",
    AGENT_RESTART: "Takes effect when you press Apply changes (restarts the backup service).",
    WATCHDOG_RELOAD: "The watchdog picks it up on its next check (log size and log "
                     "retention: after the next server restart).",
    DASHBOARD: "The dashboard uses it within 5 minutes.",
}

# What people look for first (the timetable), then the rest.
_SECTION_ORDER = ("general", "schedule", "notifications", "paths", "lan", "wol", "cloud", "budget",
                  "dashboard", "alerts", "guards", "approvals", "health", "audit", "rollover",
                  "maintenance", "run_logs", "watchdog")

_WRITE_LOCK = threading.Lock()

# Owner 2026-10-15: the settings page opens on these - what a client may
# reasonably change. Everything else is under "Advanced" (support team).
BASIC_KEYS = frozenset({
    "firm_name",
    "schedule.lan_cron", "schedule.cloud_cron", "schedule.weekly_cron",
    "schedule.monthly_cron", "schedule.audit_cron",
    "notifications.recipients", "notifications.sender", "notifications.smtp_host",
    "notifications.smtp_port", "notifications.smtp_username", "notifications.smtp_password",
    "notifications.smtp_security",
    "notifications.weekly_enabled", "notifications.monthly_enabled",
    "lan.enabled", "cloud.enabled", "paths.source_drive", "paths.lan_destination",
    "lan.shutdown_after_backup", "wol.enabled", "wol.mac_address", "wol.server_ip",
    "budget.lan_max_hours", "budget.cloud_max_hours", "budget.audit_max_hours",
    "cloud.bandwidth_limit", "dashboard.public_url",
    # Owner 2026-10-20: the deletion limits (one pair, Cloud and Office copy) are a client setting;
    # changing them still needs the typed phrase (the whole `guards` section is risky).
    "guards.max_delete_files", "guards.max_delete_percent",
})

# Values the models only accept from a fixed set (enforced by a validator, so
# the catalog cannot read them from a pattern): shown as a drop-down.
_FIXED_CHOICES = {
    "cloud.storage_class": ["STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"],
}
# How the page should draw a field (the server still validates every value).
_WIDGETS = {
    "notifications.recipients": "email_list",
    "notifications.sender": "email",
    "audit.network_error_patterns": "text_list",
    "schedule.timezone": "timezone",
}


# Owner 2026-10-15: units are fixed labels next to the box, never typed.
_UNIT_SUFFIXES = (("_seconds", "seconds"), ("_minutes", "minutes"), ("_hours", "hours"),
                  ("_days", "days"), ("_gb", "GB"), ("_mb", "MB"), ("_kb", "KB"),
                  ("_ms", "milliseconds"), ("_percent", "%"))
_UNITS = {
    "lan.retry_count": "times", "lan.max_attempts": "times", "cloud.retry_count": "times",
    "cloud.max_attempts": "times", "audit.retries": "times", "notifications.retry_attempts": "times",
    "lan.preflight_smb_attempts": "times", "wol.wake_retry_count": "times",
    "lan.preflight_retries": "times", "cloud.preflight_retries": "times",
    "cloud.verify_retries": "times", "cloud.probe_retries": "times",
    "wol.shutdown_verify_closed_probes": "checks in a row", "watchdog.failure_threshold": "checks in a row",
    "watchdog.max_deferrals": "times", "watchdog.max_transfer_deferrals": "times",
    "watchdog.max_starts_per_window": "starts", "lan.mt_threads": "threads",
    "cloud.transfers": "files at once", "cloud.checkers": "files at once", "audit.checkers": "files at once",
    "cloud.error_tail_lines": "lines", "alerts.body_max_chars": "characters",
    "alerts.support_log_tail_lines": "lines",
    "dashboard.rate_limit_triggers": "requests", "dashboard.rate_limit_logins": "requests",
    "dashboard.rate_limit_reports": "requests", "dashboard.rate_limit_settings": "requests",
    "dashboard.rate_limit_decisions": "requests",
    "dashboard.admin_password_min_length": "characters", "dashboard.history_nights": "nights",
    "maintenance.sqlite_vacuum_freelist_threshold": "pages", "maintenance.config_backups_keep": "copies",
    "maintenance.config_replace_retries": "times", "maintenance.lock_release_retries": "times",
    "rollover.archive_alert_after_days": "days",
    "guards.max_delete_files": "files", "audit.detail_max_paths": "paths",
    "cloud.bandwidth_limit": "MB/s", "cloud.buffer_size": "MB",
}
# rclone size strings ("10M", "500k") edited as a number with a fixed unit
_SIZE_KEYS = frozenset({"cloud.bandwidth_limit", "cloud.buffer_size"})


def unit_of(key: str) -> str | None:
    if key in _UNITS:
        return _UNITS[key]
    for suffix, unit in _UNIT_SUFFIXES:
        if key.endswith(suffix):
            return unit
    return None


def _widget(key: str, kind: str) -> str:
    if key in _WIDGETS:
        return _WIDGETS[key]
    if key in _SIZE_KEYS:
        return "size_mb"
    if key.startswith("schedule.") and key.endswith("_cron"):
        return "schedule"
    return {"bool": "toggle", "list": "text_list"}.get(kind, kind)


class ConfigEditError(RuntimeError):
    """A change request that cannot be applied; `status` is the HTTP code."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def is_risky(key: str) -> bool:
    return key in _RISKY_KEYS or key.split(".", 1)[0] in _RISKY_SECTIONS


def effect_of(key: str) -> str:
    section = key.split(".", 1)[0]
    # the scheduler registers its deployments (schedules, enabled legs) once, at start
    if section == "schedule" or key in _START_ONLY_KEYS:
        return AGENT_RESTART
    if section == "watchdog":
        return WATCHDOG_RELOAD
    if section == "dashboard":
        return DASHBOARD
    return NEXT_RUN


def _sections(app_model) -> list[tuple[str, type[BaseModel] | None]]:
    out = []
    for name, f in app_model.model_fields.items():
        ann = f.annotation
        out.append((name, ann if isinstance(ann, type) and issubclass(ann, BaseModel) else None))
    return out


def _kind(annotation) -> tuple[str, bool]:
    """('bool'|'int'|'float'|'str'|'list', optional?) for the form input."""
    optional = False
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        optional = type(None) in args
        annotation = next(a for a in args if a is not type(None))
    if typing.get_origin(annotation) is list:
        return "list", optional
    for t in (bool, int, float, str):
        if annotation is t:
            return t.__name__, optional
    return "str", optional


def _bounds_and_choices(info) -> dict:
    out: dict = {}
    for m in info.metadata:
        for attr in ("ge", "le", "gt", "lt", "min_length"):
            if getattr(m, attr, None) is not None:
                out[attr] = getattr(m, attr)
        pattern = getattr(m, "pattern", None)
        if pattern and pattern.startswith("^(") and pattern.endswith(")$") and "\\" not in pattern:
            out["choices"] = pattern[2:-2].split("|")
    return out


def _field_entry(key: str, info, value) -> dict:
    kind, optional = _kind(info.annotation)
    default = info.get_default(call_default_factory=True)
    secret = key in SECRET
    label, help_text = label_and_help(key, (info.description or "").strip())
    entry = {
        "key": key, "type": kind, "optional": optional,
        "label": label, "help": help_text,
        "description": (info.description or "").strip(),
        "default": None if secret or default is ... or repr(default) == "PydanticUndefined"
        else default,
        "risky": is_risky(key), "note": note_for(key), "secret": secret, "locked": key in LOCKED,
        "effect": effect_of(key), "effect_text": EFFECT_TEXT[effect_of(key)],
        "widget": _widget(key, kind),
        "level": "basic" if key in BASIC_KEYS else "advanced",
        "unit": unit_of(key),
        **_bounds_and_choices(info),
    }
    if key in _FIXED_CHOICES:
        entry["choices"] = _FIXED_CHOICES[key]
    if entry["widget"] == "timezone":
        import zoneinfo
        entry["choices"] = sorted(zoneinfo.available_timezones())
    if secret:
        entry["value"] = None
        entry["is_set"] = bool(value)
    else:
        entry["value"] = value
    return entry


def catalog(config, include_advanced: bool = True) -> list[dict]:
    """Every setting with its current (effective) value and metadata, by section.

    include_advanced=False (no Advanced login): only the basic settings - the
    advanced values are not even sent to the browser."""
    from models.config import AppConfig

    sections = [{"name": "general", "title": SECTION_TITLES["general"], "fields": [
        _field_entry("firm_name", AppConfig.model_fields["firm_name"], config.firm_name)]}]
    for name, model in _sections(AppConfig):
        if model is None:
            continue
        current = getattr(config, name)
        entries = [_field_entry(f"{name}.{k}", info, getattr(current, k))
                   for k, info in model.model_fields.items() if f"{name}.{k}" not in HIDDEN]
        entries = [e for e in entries if include_advanced or e["level"] == "basic"]
        if entries:
            sections.append({"name": name, "title": SECTION_TITLES.get(name, name),
                             "fields": entries})
    order = {n: i for i, n in enumerate(_SECTION_ORDER)}
    return sorted(sections, key=lambda sec: order.get(sec["name"], len(order)))


def _current_value(config, key: str):
    if "." not in key:
        return getattr(config, key)
    section, k = key.split(".", 1)
    return getattr(getattr(config, section), k)


def _known_keys() -> set[str]:
    from models.config import AppConfig

    keys = {"firm_name"}
    for name, model in _sections(AppConfig):
        if model is not None:
            keys |= {f"{name}.{k}" for k in model.model_fields}
    return keys


def confirmation_phrase(risky_keys: list[str]) -> str:
    """What is typed to save an important setting: one word, as for every approval."""
    return approvals.APPROVE_PHRASE


_EMAIL_DELIVERY = frozenset({
    "notifications.recipients", "notifications.sender", "notifications.smtp_host",
    "notifications.smtp_port", "notifications.smtp_username", "notifications.smtp_password",
    "notifications.smtp_security"})
# Not dangerous, but worth a second look before saving: shown with a note in the review dialog.
_NOTABLE_NOTES = (
    (lambda k: k.startswith("schedule."), "Changes when backups and reports run."),
    (lambda k: k.startswith("budget."), "Changes how long a backup or check may run."),
)
_RISKY_NOTES = (
    (lambda k: k.split(".", 1)[0] in ("guards", "approvals"), "Changes a data-safety check."),
    (lambda k: k.startswith(("wol.", "lan.shutdown")), "Changes how the NAS is switched on or off."),
    (lambda k: k.startswith("paths."), "Changes what is backed up or where it is stored."),
    (lambda k: k.startswith(("cloud.", "lan.")), "Changes where a copy is stored or whether it runs."),
    (lambda k: k == "notifications.send_on_failure", "Switches off failure emails."),
    (lambda k: k in _EMAIL_DELIVERY, "Changes who receives alerts and how they are sent. Send the test email afterwards."),
    (lambda k: k.startswith("notifications."), "Changes which emails are sent."),
)


def note_for(key: str) -> str:
    """One plain sentence on why a change matters ('' for an ordinary setting)."""
    for test, text in (_RISKY_NOTES if is_risky(key) else _NOTABLE_NOTES):
        if test(key):
            return text
    return "Changes a data-safety setting." if is_risky(key) else ""


def _validation_messages(err: ValidationError) -> list[dict]:
    out = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()))
        msg = str(e.get("msg", "invalid value"))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        elif e.get("type") != "value_error":
            msg = messages.validation_text(str(e.get("type")), e.get("ctx"), msg,
                                           unit_of(loc) or "")
        out.append({"key": loc or None, "message": msg})
    return out


def _load_candidate(config_path: str, changes: dict):
    """Write the candidate to a temp copy and load it exactly like service start."""
    from models.config import load_config

    yaml, doc = load_round_trip(config_path)
    for key, value in changes.items():
        set_dotted(doc, key, _stored_form(key, value))     # a secret is encrypted even in the check copy
    tmp_dir = tempfile.mkdtemp(prefix="aam_cfg_check_")
    try:
        candidate = Path(tmp_dir) / "config.yaml"
        with open(candidate, "w", encoding="utf-8") as f:
            yaml.dump(doc, f)
        return load_config(str(candidate))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@dataclass
class SaveResult:
    changed: list[dict] = field(default_factory=list)
    backup: str | None = None
    effects: list[str] = field(default_factory=list)


def _prepare(config_path: str, changes: dict, allow_advanced: bool = True):
    """(current config, {key: typed new value}) - raises ConfigEditError when the
    request is not allowed or the resulting config would not load. Shared by
    the live check and the save, so both always give the same verdict."""
    from models.config import load_config

    if not isinstance(changes, dict) or not changes:
        raise ConfigEditError(400, messages.act("Change a setting first", "No changes were sent"))
    known = _known_keys()
    unknown = sorted(k for k in changes if k not in known)
    if unknown:
        raise ConfigEditError(400, messages.act("Reload the page and try again", f"This version does not know the setting(s): {', '.join(unknown)}"))
    locked = sorted(k for k in changes if k in LOCKED)
    if locked:
        raise ConfigEditError(403, messages.act(
            f"Ask support to change {', '.join(locked)} in config.yaml on the server",
            "A mistake here could lock everyone out, so it cannot be changed from this page"))
    advanced = sorted(k for k in changes if k not in BASIC_KEYS)
    if advanced and not allow_advanced:
        raise ConfigEditError(403, messages.act(
            "Open the Advanced tab and sign in with the Advanced password",
            f"{', '.join(advanced)} can only be changed there"))
    current = load_config(config_path)
    # an empty secret field means "keep the stored one"
    wanted = {k: v for k, v in changes.items()
              if not (k in SECRET and (v is None or v == ""))
              and not (k not in SECRET and _current_value(current, k) == v)}
    if not wanted:
        return current, {}
    try:
        candidate = _load_candidate(config_path, wanted)
    except ValidationError as e:
        raise ConfigEditError(400, messages.act("Fix the highlighted settings, then save again", "Nothing was saved because a value is not valid"),
                              errors=_validation_messages(e)) from e
    # the typed values the services will actually see ("3" -> 3)
    typed = {k: _current_value(candidate, k) for k in wanted}
    return current, {k: v for k, v in typed.items()
                     if k in SECRET or v != _current_value(current, k)}


def validate_changes(config_path: str, changes: dict, allow_advanced: bool = True) -> dict:
    """Live check for the settings page: the same verdict a save would give,
    without writing anything (and without the lock / phrase steps)."""
    try:
        _, typed = _prepare(config_path, changes, allow_advanced)
    except ConfigEditError as e:
        return {"ok": False, "detail": str(e), "errors": e.extra.get("errors")
                or [{"key": None, "message": str(e)}]}
    risky = sorted(k for k in typed if is_risky(k))
    return {"ok": True, "changes": len(typed), "risky": risky,
            "confirm_phrase": confirmation_phrase(risky) if risky else None}


def apply_changes(config_path: str, changes: dict, *, phrase: str | None, actor: str,
                  lock_busy, db=None, now_iso=None, allow_advanced: bool = True) -> SaveResult:
    """Validate, confirm, back up and write `changes` ({dotted key: value}).

    lock_busy: callable -> str | None; a reason when a backup/rollover holds
    the backup lock (then nothing is written).
    """
    with _WRITE_LOCK:
        current, typed = _prepare(config_path, changes, allow_advanced)
        if not typed:
            return SaveResult()

        risky = sorted(k for k in typed if is_risky(k))
        if risky:
            expected = confirmation_phrase(risky)
            if not approvals.phrase_matches(expected, phrase or ""):
                raise ConfigEditError(409, messages.act(
                    f"Type exactly: {expected}, then save again",
                    "These settings change what or where is backed up, a safety check or the NAS power"),
                                      confirm_phrase=expected, risky=risky)
        busy = lock_busy()
        if busy:
            raise ConfigEditError(409, messages.act("Save again when the run has finished", f"Nothing was saved because {busy}"))

        try:
            with config_transaction(config_path, tmp_prefix=".config_edit_") as txn:
                for key, value in typed.items():
                    set_dotted(txn.doc, key, _stored_form(key, value))
        except ConfigWriteError as e:
            raise ConfigEditError(409 if e.busy else 500, str(e)) from e
        backup, saved = txn.backup, txn.saved
        try:
            prune_backups(config_path, keep=saved.maintenance.config_backups_keep)
        except OSError:  # housekeeping must never fail a saved change
            pass
        result = SaveResult(backup=str(backup))
        for key in sorted(typed):
            secret = key in SECRET
            entry = {
                "key": key, "risky": is_risky(key), "effect": effect_of(key),
                "old": "(hidden)" if secret else _current_value(current, key),
                "new": "(hidden)" if secret else typed[key],
            }
            result.changed.append(entry)
            if db is not None:
                db.record_config_change({
                    "changed_at": now_iso(), "changed_by": actor, "key": key,
                    "old_value": repr(entry["old"]), "new_value": repr(entry["new"]),
                    "risky": int(entry["risky"]), "backup_file": backup.name,
                })
        result.effects = sorted({EFFECT_TEXT[c["effect"]] for c in result.changed})
        return result


def _stored_form(key: str, value):
    """What goes into config.yaml: a secret is written encrypted (DPAPI, plan S2) when this
    machine can; an empty value stays empty."""
    if key in SECRET and value:
        from core import secrets as secrets_store
        if secrets_store.available() and not secrets_store.is_protected(str(value)):
            return secrets_store.protect(str(value))
    return value


PASSWORD_KEYS = frozenset({"dashboard.admin_password_hash", "dashboard.login_password_hash"})


def set_admin_password_hash(config_path: str, new_hash: str, *, actor: str, db=None,
                            now_iso=None) -> str:
    return set_password_hash(config_path, "dashboard.admin_password_hash", new_hash,
                             actor=actor, db=db, now_iso=now_iso)


def set_password_hash(config_path: str, key: str, new_hash: str, *, actor: str, db=None,
                      now_iso=None) -> str:
    """Store a new login hash (`dashboard.login_password_hash` or
    `dashboard.admin_password_hash`) the same safe way as any setting: backup, atomic
    comment-preserving write, load check (restore on failure), history entry with the value
    hidden. Returns the backup file name."""
    if key not in PASSWORD_KEYS:
        raise ValueError(f"{key} is not a password hash key")
    with _WRITE_LOCK:
        try:
            with config_transaction(config_path, tmp_prefix=".config_edit_") as txn:
                set_dotted(txn.doc, key, new_hash)
        except ConfigWriteError as e:
            raise ConfigEditError(409 if e.busy else 500, str(e)) from e
        backup, saved = txn.backup, txn.saved
        try:
            prune_backups(config_path, keep=saved.maintenance.config_backups_keep)
        except OSError:
            pass
        if db is not None:
            db.record_config_change({
                "changed_at": now_iso(), "changed_by": actor,
                "key": key, "old_value": "'(hidden)'",
                "new_value": "'(hidden)'", "risky": 1, "backup_file": backup.name,
            })
        return backup.name
