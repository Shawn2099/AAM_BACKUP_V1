"""Support attachments of a problem email (docs/EMAIL_CONTENT_SPEC.md §5,
audit 2026-10-18; owner D1: logs with client file names may be attached).

`build(config, event, db)` returns the attachments of one PROBLEM / ACTION
NEEDED / WARNING email:

* AAM-support_<yyyymmdd-hhmm>_<code>.txt - always: the cause, the settings in
  effect (no secrets), the last runs, data safety, server facts and the tail of
  each tool log (error lines first), plus where the full logs are on the server.
* AAM-logs_<yyyymmdd-hhmm>_<code>.zip - the run's full log folder (robocopy's
  UTF-16 logs as UTF-8, .gz unpacked), when alerts.attach_file_names is on and
  everything fits in alerts.attachments_max_total_kb.

Every text that leaves the server goes through `redact()` first. Never raises:
a broken bundle must not stop the email.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import shutil
import socket
import zipfile
from pathlib import Path

import pendulum
from loguru import logger

from core import faults
from core.email_compose import _data_safety, exit_meaning, software_version
from core.time_utils import IST, format_ist

KINDS_WITH_ATTACHMENTS = frozenset({"FAILURE", "ACTION_REQUIRED", "WARNING"})
TEXT_SUFFIXES = (".log", ".txt", ".json", ".csv")
REDACTED = "[REDACTED]"
_ERROR_LINE = re.compile(r"\berror\b|\bfailed\b|\bfatal\b|\bcritical\b|access is denied",
                         re.IGNORECASE)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
               re.DOTALL),
    re.compile(r'("(?:private_key|private_key_id|client_secret|refresh_token|access_token)"'
               r'\s*:\s*")[^"]*(")'),
    re.compile(r"(\b(?:password|passwd|pwd|api_key|token|secret)\s*[=:]\s*)\S+", re.IGNORECASE),
)
_TIME = "D MMM YYYY, h:mm A"


def redact(text: str, secrets: list[str]) -> str:
    """Remove the configured secrets and anything that looks like a key or token."""
    for secret in sorted((s for s in secrets if s and len(s) >= 4), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _SECRET_PATTERNS[0].sub(REDACTED, text)
    text = _SECRET_PATTERNS[1].sub(lambda m: m.group(1) + REDACTED + m.group(2), text)
    return _SECRET_PATTERNS[2].sub(lambda m: m.group(1) + REDACTED, text)


def _secrets(config) -> list[str]:
    return [config.notifications.smtp_password, config.dashboard.api_key,
            config.dashboard.admin_password_hash]


def _read_bytes(path: Path) -> bytes:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as f:
            return f.read()
    return path.read_bytes()


def _as_text(data: bytes) -> str:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")          # robocopy /UNILOG
    return data.decode("utf-8-sig", errors="replace")


def _log_files(run_dir: Path) -> list[Path]:
    if not run_dir.is_dir():
        return []
    return sorted(p for p in run_dir.rglob("*")
                  if p.is_file() and not p.name.startswith(".")
                  and (p.suffix in TEXT_SUFFIXES or p.name.endswith(tuple(s + ".gz" for s in TEXT_SUFFIXES))))


def _plain_name(path: Path, run_dir: Path) -> str:
    rel = path.relative_to(run_dir).as_posix()
    return rel[:-3] if rel.endswith(".gz") else rel


def _tail_errors_first(text: str, lines: int) -> str:
    rows = text.splitlines()
    errors = [r for r in rows if _ERROR_LINE.search(r)][-lines:]
    rest = rows[-max(lines - len(errors), 0):] if lines > len(errors) else []
    out = []
    if errors:
        out += ["  -- error lines --", *("  " + r for r in errors)]
    if rest:
        out += ["  -- last lines --", *("  " + r for r in rest)]
    return "\n".join(out) or "  (empty)"


def _settings(config) -> list[str]:
    c = config
    return [
        f"source {c.paths.source_drive} | LAN target {c.paths.lan_destination}",
        f"LAN enabled {c.lan.enabled} | cloud enabled {c.cloud.enabled} "
        f"(bucket {c.cloud.bucket or '-'}) | WoL {c.wol.enabled} | "
        f"NAS shutdown after backup {c.lan.shutdown_after_backup}",
        f"schedule LAN '{c.schedule.lan_cron}' cloud '{c.schedule.cloud_cron}' "
        f"audit '{c.schedule.audit_cron}' ({c.schedule.timezone})",
        f"budgets LAN {c.budget.lan_max_hours} h, cloud {c.budget.cloud_max_hours} h | "
        f"attempts LAN {c.lan.max_attempts}, cloud {c.cloud.max_attempts}",
        f"guards: max delete {c.guards.max_delete_files} files / {c.guards.max_delete_percent}%, "
        f"max source shrink {c.guards.max_source_shrink_percent}%",
    ]


def _last_runs(db, leg: str | None, count: int = 5) -> list[str]:
    try:
        runs = db.get_recent_runs(50) if db is not None else []
    except Exception:  # noqa: BLE001
        return ["(run history not readable)"]
    rows = [r for r in runs if leg is None or r.get("mode") == leg][:count]
    out = []
    for r in rows:
        try:
            code = (json.loads(r.get("extended_metrics") or "{}") or {}).get("fault_code")
        except (ValueError, AttributeError):
            code = None
        out.append(f"{format_ist(r.get('started_at'), _TIME)}  {r.get('mode')}  "
                   f"{r.get('status')}  files {r.get('files_copied') or 0}  "
                   f"exit {r.get('exit_code')}" + (f"  {code}" if code else ""))
    return out or ["(no runs recorded)"]


def _server(config) -> list[str]:
    lines = [f"host {socket.gethostname()}"]
    for label, path in (("source", config.paths.source_drive),
                        ("runtime", config.paths.runtime_dir)):
        try:
            free = shutil.disk_usage(Path(path).anchor or path).free / 1024 ** 3
            lines.append(f"{label} disk {Path(path).anchor or path}: {free:.1f} GB free")
        except OSError:
            lines.append(f"{label} disk {path}: free space not readable")
    try:
        import psutil

        for name in ("AamPrefectServer", "AamBackupAgent", "AamWatchdog"):
            try:
                lines.append(f"service {name}: {psutil.win_service_get(name).status()}")
            except Exception:  # noqa: BLE001 - not installed / not Windows
                lines.append(f"service {name}: not found")
    except (ImportError, AttributeError):
        pass
    return lines


def _report(config, event, db, run_dir: Path | None, zip_note: str) -> str:
    fault = faults.get(event.fault_code) if event.fault_code else None
    leg = event.mode if event.mode in ("lan", "cloud") else None
    when = format_ist(event.occurred_at, _TIME)
    lines = ["AAM BACKUP - SUPPORT REPORT", "=" * 60,
             f"Fault code   : {fault.code + '  ' + fault.title if fault else '-'}",
             f"Reference    : {event.run_id or event.key}",
             f"Firm / server: {config.firm_name} / {socket.gethostname()}",
             f"Software     : {software_version()}",
             f"Status       : {event.status or '-'}  (exit {event.exit_code if event.exit_code is not None else '-'}"
             f" = {exit_meaning(leg or '', event.exit_code)})",
             f"When         : {when}",
             f"Generated    : {pendulum.now(IST).format(_TIME)} IST", "",
             "CAUSE", event.attachment_text or event.message or "-", ""]
    if event.details:
        lines += ["DETAILS", *(f"{k}: {v}" for k, v in event.details.items()), ""]
    lines += ["SETTINGS IN EFFECT (no passwords or keys)", *_settings(config), "",
              f"LAST RUNS ({leg or 'all'})", *_last_runs(db, leg), "",
              "DATA SAFETY", *_data_safety(config, db, pendulum.now(IST)), "",
              "SERVER", *_server(config), ""]
    if run_dir is not None:
        lines.append(f"TOOL LOGS (last {config.alerts.support_log_tail_lines} lines each, "
                     "error lines first)")
        files = _log_files(run_dir)
        for path in files:
            try:
                text = _as_text(_read_bytes(path))
            except OSError as e:
                lines += [f"[{_plain_name(path, run_dir)}] not readable: {e}"]
                continue
            lines += [f"[{_plain_name(path, run_dir)}]",
                      _tail_errors_first(text, config.alerts.support_log_tail_lines)]
        if not files:
            lines.append("  (no tool logs in the run folder)")
        lines += ["", f"FULL LOGS on the server: {run_dir}  (kept "
                      f"{config.run_logs.retention_days} days)", zip_note]
    else:
        lines.append("FULL LOGS: no run folder for this event (see the agent log in "
                     f"{config.paths.log_directory}).")
    return redact("\n".join(lines) + "\n", _secrets(config))


def _logs_zip(config, run_dir: Path, event) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in _log_files(run_dir):
            try:
                text = _as_text(_read_bytes(path))
            except OSError as e:
                text = f"(not readable: {e})"
            z.writestr(_plain_name(path, run_dir), redact(text, _secrets(config)))
        z.writestr("event.json", redact(json.dumps({
            "fault_code": event.fault_code, "reference": event.run_id or event.key,
            "status": event.status, "exit_code": event.exit_code,
            "occurred_at": event.occurred_at, "message": event.message,
            "details": event.details}, indent=2, default=str), _secrets(config)))
    return buf.getvalue()


def build(config, event, db=None) -> list[dict]:
    """Attachments for one alert email ([] for RESOLVED / INFO). Never raises."""
    if event.kind not in KINDS_WITH_ATTACHMENTS:
        return []
    try:
        stamp = pendulum.parse(str(event.occurred_at), tz=IST).in_timezone(IST).format(
            "YYYYMMDD-HHmm")
    except Exception:  # noqa: BLE001
        stamp = pendulum.now(IST).format("YYYYMMDD-HHmm")
    code = event.fault_code or "NOCODE"
    cap = config.alerts.attachments_max_total_kb * 1024
    run_dir = Path(event.artefacts_dir) if event.artefacts_dir else None
    try:
        logs = None
        note = "Full logs: not attached (alerts.attach_file_names is off)."
        if run_dir is not None and config.alerts.attach_file_names:
            logs = _logs_zip(config, run_dir, event)
            note = "Full logs: attached as the .zip next to this report."
        report = _report(config, event, db, run_dir, note).encode("utf-8")
        if logs is not None and len(report) + len(logs) > cap:
            logs = None
            note = (f"Full logs: not attached - too large for one email "
                    f"({config.alerts.attachments_max_total_kb} KB); copy them from {run_dir}.")
            report = _report(config, event, db, run_dir, note).encode("utf-8")
        if len(report) > cap:
            report = report[-cap:]
        out = [{"filename": f"AAM-support_{stamp}_{code}.txt", "content": report}]
        if logs is not None:
            out.append({"filename": f"AAM-logs_{stamp}_{code}.zip", "content": logs})
        return out
    except Exception as e:  # noqa: BLE001 - the email must still go out
        logger.error(f"Support attachments could not be built for {event.key}: {e}")
        text = redact(f"Support report could not be built ({type(e).__name__}: {e}).\n\n"
                      f"{event.message or ''}", _secrets(config))
        return [{"filename": f"AAM-support_{stamp}_{code}.txt",
                 "content": text.encode("utf-8")[-cap:]}]
