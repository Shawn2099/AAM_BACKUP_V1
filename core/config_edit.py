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

from core import approvals
from core.config_help import SECTION_TITLES, label_and_help
from core.config_file import (backup_config_file, load_round_trip, prune_backups, set_dotted,
                               write_atomic)

LOCKED = frozenset({
    "dashboard.auth_enabled", "dashboard.api_key", "dashboard.bind_address", "dashboard.port",
    "dashboard.admin_password_hash",
    "paths.runtime_dir", "paths.database_path", "paths.log_directory",
})
SECRET = frozenset({"notifications.smtp_password"})
# Never listed at all: changed only through "Change admin password".
HIDDEN = frozenset({"dashboard.admin_password_hash"})
_RISKY_SECTIONS = frozenset({"guards", "approvals"})
_RISKY_KEYS = frozenset({
    # what / where is backed up
    "paths.source_drive", "paths.lan_destination", "paths.gcs_key_path",
    "cloud.enabled", "cloud.bucket", "cloud.project_number", "cloud.location",
    "cloud.storage_class", "lan.enabled", "rollover.source_placeholder_name",
    # data safety
    "lan.use_backup_mode", "health.clock_unverified_policy", "cloud.write_probe_enabled",
    # NAS power
    "wol.enabled", "wol.server_ip", "wol.mac_address", "wol.broadcast_address",
    "lan.shutdown_after_backup", "lan.shutdown_on_all_retries_exhausted",
})

NEXT_RUN = "next_run"
AGENT_RESTART = "agent_restart"
WATCHDOG_RESTART = "watchdog_restart"
DASHBOARD = "dashboard"
EFFECT_TEXT = {
    NEXT_RUN: "Takes effect from the next backup.",
    AGENT_RESTART: "Takes effect after the backup service is restarted (on the server: "
                   "deploy\\07_restart_services.bat).",
    WATCHDOG_RESTART: "Takes effect after the watchdog service is restarted.",
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
    "notifications.smtp_security", "notifications.send_on_failure",
    "notifications.weekly_enabled", "notifications.monthly_enabled",
    "lan.enabled", "cloud.enabled", "paths.source_drive", "paths.lan_destination",
    "lan.shutdown_after_backup", "wol.enabled", "wol.mac_address", "wol.server_ip",
    "budget.lan_max_hours", "budget.cloud_max_hours", "budget.audit_max_hours",
    "cloud.bandwidth_limit", "dashboard.public_url",
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
    "wol.shutdown_verify_closed_probes": "checks in a row", "watchdog.failure_threshold": "checks in a row",
    "watchdog.max_deferrals": "times", "watchdog.max_transfer_deferrals": "times",
    "watchdog.max_starts_per_window": "starts", "lan.mt_threads": "threads",
    "cloud.transfers": "files at once", "cloud.checkers": "files at once", "audit.checkers": "files at once",
    "cloud.error_tail_lines": "lines", "alerts.body_max_chars": "characters",
    "dashboard.admin_password_min_length": "characters",
    "maintenance.sqlite_vacuum_freelist_threshold": "pages", "maintenance.config_backups_keep": "copies",
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
    if section == "schedule":
        return AGENT_RESTART
    if section == "watchdog":
        return WATCHDOG_RESTART
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
        "risky": is_risky(key), "secret": secret, "locked": key in LOCKED,
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
    if len(risky_keys) == 1:
        return f"CHANGE {risky_keys[0]}"
    return f"CHANGE {len(risky_keys)} SETTINGS"


def _validation_messages(err: ValidationError) -> list[dict]:
    out = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()))
        msg = str(e.get("msg", "invalid value"))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        out.append({"key": loc or None, "message": msg})
    return out


def _load_candidate(config_path: str, changes: dict):
    """Write the candidate to a temp copy and load it exactly like service start."""
    from models.config import load_config

    yaml, doc = load_round_trip(config_path)
    for key, value in changes.items():
        set_dotted(doc, key, value)
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
        raise ConfigEditError(400, "No changes were sent.")
    known = _known_keys()
    unknown = sorted(k for k in changes if k not in known)
    if unknown:
        raise ConfigEditError(400, f"Unknown setting(s): {', '.join(unknown)}")
    locked = sorted(k for k in changes if k in LOCKED)
    if locked:
        raise ConfigEditError(403, f"{', '.join(locked)} can only be changed in config.yaml on "
                                   "the server (a mistake here could lock everyone out).")
    advanced = sorted(k for k in changes if k not in BASIC_KEYS)
    if advanced and not allow_advanced:
        raise ConfigEditError(403, f"{', '.join(advanced)}: Advanced settings need the "
                                   "Advanced login.")
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
        raise ConfigEditError(400, "Not saved - the new settings are not valid.",
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
    from models.config import load_config

    with _WRITE_LOCK:
        current, typed = _prepare(config_path, changes, allow_advanced)
        if not typed:
            return SaveResult()

        risky = sorted(k for k in typed if is_risky(k))
        if risky:
            expected = confirmation_phrase(risky)
            if not approvals.phrase_matches(expected, phrase or ""):
                raise ConfigEditError(409, f"These settings change what or where is backed up, "
                                           f"a data-safety check or the NAS power. To save, type "
                                           f"exactly: {expected}",
                                      confirm_phrase=expected, risky=risky)
        busy = lock_busy()
        if busy:
            raise ConfigEditError(409, f"Not saved - {busy}. Try again when it has finished.")

        backup = backup_config_file(config_path)
        yaml, doc = load_round_trip(config_path)
        for key, value in typed.items():
            set_dotted(doc, key, value)
        write_atomic(config_path, yaml, doc, tmp_prefix=".config_edit_")
        try:
            saved = load_config(config_path)
        except Exception as e:  # noqa: BLE001 - must never leave an unloadable file
            shutil.copy2(backup, config_path)
            raise ConfigEditError(500, f"The written file did not load ({e}); the previous "
                                       f"config.yaml was restored from {backup.name}.") from e
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


def set_admin_password_hash(config_path: str, new_hash: str, *, actor: str, db=None,
                            now_iso=None) -> str:
    """Store a new Advanced-login hash the same safe way as any setting:
    backup, atomic comment-preserving write, load check (restore on failure),
    history entry with the value hidden. Returns the backup file name."""
    from models.config import load_config

    with _WRITE_LOCK:
        backup = backup_config_file(config_path)
        yaml, doc = load_round_trip(config_path)
        set_dotted(doc, "dashboard.admin_password_hash", new_hash)
        write_atomic(config_path, yaml, doc, tmp_prefix=".config_edit_")
        try:
            saved = load_config(config_path)
        except Exception as e:  # noqa: BLE001 - must never leave an unloadable file
            shutil.copy2(backup, config_path)
            raise ConfigEditError(500, f"The written file did not load ({e}); the previous "
                                       f"config.yaml was restored from {backup.name}.") from e
        try:
            prune_backups(config_path, keep=saved.maintenance.config_backups_keep)
        except OSError:
            pass
        if db is not None:
            db.record_config_change({
                "changed_at": now_iso(), "changed_by": actor,
                "key": "dashboard.admin_password_hash", "old_value": "'(hidden)'",
                "new_value": "'(hidden)'", "risky": 1, "backup_file": backup.name,
            })
        return backup.name
