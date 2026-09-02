"""LAN sync — robocopy /MIR wrapper with exit code classification.

Reference: AAM_BACKUP_V2/core/robocopy.py — proven bitmask logic and flag set.
Exit code reference: https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/robocopy
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from core.process import resolve_binary
from models.config import LanConfig

# ═══════════════════════════════════════════════════════════════
# Flag validation — /NC is FORBIDDEN
# Source: ConvertFrom-RobocopLog §Notes
# /NC suppresses file class labels (e.g. "New File", "Changed") which
# the downstream log parser relies on to categorise per-file outcomes.
# ═══════════════════════════════════════════════════════════════

# Tail sizes for log capture — enough for full context, bounded to prevent
# oversized payloads in the result dict.
_ERROR_LOG_TAIL = 100_000   # bytes — full error context for real failures (codes 8-15, 16+)
_ANOMALY_LOG_TAIL = 100_000 # bytes — full context for anomalies (codes 4-7); no alert, but complete forensics


def _validate_required_flags(flags: list[str]) -> None:
    for f in flags:
        if f.upper() in ("/NC", "-NC"):
            raise ValueError("/NC flag suppresses file class labels - parser has nothing to match")


_FAILED_LINE = re.compile(r"^\s*\*\*\s*FAILED:", re.MULTILINE)

# P1-COUNT (A-prime): the job-summary "Files:" row. Column ORDER is fixed by
# robocopy regardless of locale (Total Copied Skipped Mismatch FAILED Extras),
# so we parse positionally instead of matching localized header words.
_SUMMARY_FILES_ROW = re.compile(r"^\s*Files\s*:", re.MULTILINE)


def _summary_files_row_values(log_text: str) -> list[int] | None:
    """Return the numeric columns of the job-summary Files row, or None.

    Fails closed on anything non-numeric (localized separators are stripped;
    letters or missing columns abort parsing) so callers can fall back to the
    exit-bitmask floor instead of reporting a false 0.
    """
    match = _SUMMARY_FILES_ROW.search(log_text or "")
    if not match:
        return None
    line_end = log_text.find("\n", match.start())
    row = log_text[match.start(): line_end if line_end != -1 else len(log_text)]
    values: list[int] = []
    for token in row.split(":", 1)[1].split():
        cleaned = token.replace(",", "").replace(".", "")
        if not cleaned.isdigit():
            return None
        values.append(int(cleaned))
    return values or None


def failed_file_count(log_text: str, exit_code: int) -> int:
    """Authoritative failed-file count for a robocopy run.

    A-prime contract (IMPLEMENTATION_FIX_PLAN.md v1.1):
      1. Parse the summary "Files:" row positionally; FAILED is column index 4.
      2. Floor by the exit bitmask: bit 3 set means at least one file could
         not be copied - if the summary is absent/unparseable, report >= 1
         rather than the misleading 0 (/NJS blindness, LAN-06/LAN-15).
      3. Contradictory signals (summary says 0, bit3 set) resolve to the loud
         side: 1.
    """
    bit3_floor = 1 if (exit_code & 8) else 0
    values = _summary_files_row_values(log_text)
    parsed_failed = values[4] if values and len(values) > 4 else None
    if parsed_failed is None:
        return bit3_floor
    return max(parsed_failed, bit3_floor)


def count_failed_lines(log_text: str) -> int:
    """Count robocopy per-file failure lines ("** FAILED: <path>") in a log tail.

    Robocopy prints one such line per file that could not be copied. The count
    is taken over the captured tail only; if a run fails more files than fit
    in the tail window the true number is higher (the tail is the contract:
    bounded payload, actionable diagnostics).
    """
    return len(_FAILED_LINE.findall(log_text))


def _read_log_tail(log_path: Path, max_bytes: int) -> str:
    """Read the tail of a robocopy log file, bounded to max_bytes.

    Robocopy writes summary and error details at the end of the log.
    Reading the tail rather than the head ensures we always get the
    most actionable diagnostic data regardless of log file size.

    F15: seek from the end instead of reading the whole file. At 1M+ files
    with /V /TS /FP the log can be several hundred MB; the old
    read-entire-then-slice path loaded all of it into RAM for a 100KB tail.

    Returns the raw text tail or a fallback message if the file is unreadable.
    """
    try:
        size = log_path.stat().st_size
        if size <= max_bytes:
            return log_path.read_text(encoding="utf-8", errors="replace")
        with open(log_path, "rb") as f:
            f.seek(size - max_bytes)
            data = f.read()
        # The cut point can land mid UTF-8 sequence; errors=replace yields a
        # leading U+FFFD which we strip so the tail starts on a clean char.
        return data.decode("utf-8", errors="replace").lstrip("\ufffd")
    except OSError as exc:
        return f"robocopy log unreadable: {exc}"


def classify_exit_code(code: int) -> str:
    """Classify a robocopy exit code using official Microsoft bitmask rules.

    Robocopy exit codes are a bitmask of independent status flags.
    Each bit is independent and multiple can be set simultaneously.

    Official bit definitions (MS Docs):
        Bit 0 (1):  One or more files were copied successfully.
        Bit 1 (2):  Extra files or directories detected on destination.
                    No copy errors — purely informational.
        Bit 2 (4):  Mismatched files detected (size/time differ, not overwritten).
                    No copy errors — the files remain on destination as-is.
        Bit 3 (8):  Some files or directories could not be copied (copy errors,
                    retry limit exceeded). Backup is incomplete. Needs attention.
        Bit 4 (16): Serious error. Robocopy did not copy any files. Usage error
                    or insufficient access privileges on source/destination.

    Classification mapping:
        Codes 0–3   → LAN_COMPLETE  (bits 0–1 only: success, extras — no anomaly)
        Codes 4–7   → LAN_PARTIAL   (bit 2 set: mismatches/extras — sync completed,
                                     but anomalies present. Non-fatal. Investigate later.)
        Codes 8–15  → LAN_PARTIAL   (bit 3 set: copy errors — sync incomplete. Fatal
                                     for affected files. Needs immediate attention.)
        Code 16+    → LAN_FAILED    (bit 4 set: fatal process error — nothing copied.)

    Note: Codes 4–7 and 8–15 both map to LAN_PARTIAL but have different severity.
    Callers MUST use `result.returncode & 8` (not `status`) to distinguish between
    anomalies and copy errors. See run_lan_sync() for the enforcement of this contract.

    Returns: LAN_COMPLETE | LAN_PARTIAL | LAN_FAILED
    """
    if code & 16:
        return "LAN_FAILED"
    if code & 8:
        return "LAN_PARTIAL"
    if code in (0, 1, 2, 3):
        return "LAN_COMPLETE"
    if 4 <= code <= 7:
        # Bit 2 set: mismatches or extras flagged — sync completed with anomalies
        return "LAN_PARTIAL"
    # Negative codes (-1 timeout sentinel) and any unexpected values → failed
    return "LAN_FAILED"


# G8: prefix used for robocopy /LOG temp files. Normal runs delete their log
# in a finally block; hard-killed runs (SCM stop, timeout kill, crash) strand
# them in %TEMP%. cleanup_orphaned_robocopy_logs() reclaims those at the start
# of each backup flow, age-gated so a live run's log is never touched.
ROBOCOPY_LOG_PREFIX = "robocopy_sync_"


def cleanup_orphaned_robocopy_logs(max_age_hours: int = 24) -> int:
    """Delete orphaned robocopy temp logs older than max_age_hours.

    Returns the number of files removed. Never raises — cleanup is
    best-effort and must not block a backup.
    """
    import time as _time
    cutoff = _time.time() - max_age_hours * 3600
    removed = 0
    tempdir = Path(tempfile.gettempdir())
    try:
        candidates = tempdir.glob(f"{ROBOCOPY_LOG_PREFIX}*.log")
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.warning(f"Cleaned up {removed} orphaned robocopy log(s) older than {max_age_hours}h from {tempdir}")
    return removed


def _assert_source_not_empty(source: str) -> None:
    from pathlib import Path as _Path
    from core.health import check_source_drive as _check_src
    ok, reason = _check_src(source)
    if not ok and "appears empty" in reason:
        raise ValueError(reason)


def build_robocopy_command(source: str, dest: str, lan_config: LanConfig) -> list[str]:
    """Build robocopy /MIR command with production-verified flags.

    Flag rationale:
        /MIR    — Mirror source to dest (equivalent to /E /PURGE). Ensures
                  dest is an exact replica; orphaned destination files are pruned.
        /Z      — Restartable mode. Partially-copied files resume on retry instead
                  of starting over (important for large files over unstable LAN).
        /ZB     — Falls back to Backup mode if a file is access-denied. Requires
                  SeBackupPrivilege on the service account for full effectiveness.
        /XJ     — Exclude junction points (both files and dirs). Prevents infinite
                  recursion through Windows symlinks / volume mount points.
        /MT:n   — Multi-threaded copy. Tuned via config; default 4 matches the
                  target NAS HDD's optimal concurrency for sequential I/O.
        /R:n    — Per-file retry count on transient network errors.
        /W:n    — Wait seconds between retries.
        /V      — Verbose per-file logging (required for log parser).
        /TS     — Include source file timestamps in log output.
        /FP     — Include full file paths in log (critical for failure diagnosis).
        /NJH    — No job header (reduces log noise).
        /NDL    — No directory list (individual file lines are sufficient).
        /NP     — No progress percentage (meaningless in log files).
        NOTE: /NJS was REMOVED (P1-COUNT) — it suppressed the job summary,
        which is now the authoritative positional source for files_failed.
        /XD     — Exclude "System Volume Information" to avoid access errors on
                  NTFS system directories.
    """
    flags = [
        "/MIR",
        "/Z",
        "/ZB",
        "/XJ",
        f"/MT:{lan_config.mt_threads}",
        f"/R:{lan_config.retry_count}",
        f"/W:{lan_config.retry_wait_seconds}",
        "/V", "/TS", "/FP",
        "/NJH", "/NDL", "/NP",
        "/XF", ".AAM_TARGET_MOUNTED",
        "/XD", "System Volume Information", "$RECYCLE.BIN",
    ]

    _validate_required_flags(flags)
    _assert_source_not_empty(source)
    robocopy_exe = resolve_binary("robocopy") or "robocopy"
    return [robocopy_exe, source, dest, *flags]


def run_lan_sync(source: str, dest: str, lan_config: LanConfig) -> dict:
    """Execute robocopy /MIR mirror sync and return a structured result dict.

    Robocopy writes all output to the /LOG:path file when that flag is set.
    stdout/stderr from the subprocess are empty and are discarded via DEVNULL.
    Exit code and diagnostic details are read from the log file.

    Return dict schema:
        {
            "status":          str        — "LAN_COMPLETE" | "LAN_PARTIAL" | "LAN_FAILED"
            "exit_code":       int        — Raw robocopy exit code (or -1 for exceptions)
            "error":           str|None   — Log tail (up to 100KB) for genuine failures
                                           (exit codes 8–15 and 16+). None otherwise.
            "anomaly_details": str|None   — Log tail (up to 5KB) for anomaly-only runs
                                           (exit codes 4–7: mismatches/extras, no copy
                                           failure). None on clean success or real errors
                                           (real errors are captured in `error` instead).
        }

    Severity contract:
        - `error` populated      → alert system MUST notify. Backup is incomplete.
        - `anomaly_details` set  → log a warning, investigate later. Backup is complete.
        - Both None              → clean success.

    Args:
        source:     Source drive path (e.g. "D:\\").
        dest:       LAN UNC destination (e.g. "\\\\10.0.0.5\\Backups").
        lan_config: LAN configuration with retry/timeout/thread settings.
    """
    cmd = build_robocopy_command(source, dest, lan_config)
    log_path = None

    try:
        log_fd, log_path_str = tempfile.mkstemp(suffix=".log", prefix="robocopy_sync_")
        os.close(log_fd)  # Release handle so robocopy can open and write the file
        log_path = Path(log_path_str)

        cmd.extend([f"/LOG:{log_path}"])

        logger.info(f"LAN sync: {' '.join(cmd[:4])}... (log: {log_path})")

        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,  # Robocopy writes all output via /LOG:path
            stderr=subprocess.DEVNULL,  # stderr is also empty when /LOG: is used
            timeout=lan_config.subprocess_timeout_seconds,
        )

        status = classify_exit_code(result.returncode)
        logger.info(f"LAN sync exit {result.returncode} → {status}")

        error_msg = None
        anomaly_details = None
        files_failed = 0

        if status == "LAN_FAILED" or (result.returncode & 8):
            # Real failure: bit 4 (fatal) or bit 3 (copy errors) set.
            # Capture full log tail for alert system and operator triage.
            error_msg = _read_log_tail(log_path, _ERROR_LOG_TAIL)
            # P1-COUNT (A-prime): positional summary parse + bit-3 floor.
            # The old per-marker count reported 0 under /NJS because the
            # "** FAILED:" lines and summary never reached the tail.
            files_failed = failed_file_count(error_msg, result.returncode)
            marker_count = count_failed_lines(error_msg)
            logger.error(
                f"LAN sync FAILED (exit {result.returncode}) - "
                f"{len(error_msg)} bytes of log captured in result['error'], "
                f"files_failed={files_failed} "
                f"({marker_count} '** FAILED:' marker(s) visible in tail)"
            )

        elif 4 <= result.returncode <= 7:
            # Anomaly only: bit 2 set (mismatches/extras), no copy errors.
            # Sync completed. Capture a short log tail so operators can diagnose
            # the anomaly if the warning is investigated — but do NOT set `error`
            # so alert systems are not triggered.
            anomaly_details = _read_log_tail(log_path, _ANOMALY_LOG_TAIL)
            logger.warning(
                f"LAN sync anomalies detected (exit {result.returncode}) - "
                f"mismatches or extra destination files found. "
                f"Backup is complete. Check result['anomaly_details'] for context."
            )

        return {
            "status": status,
            "exit_code": result.returncode,
            "error": error_msg,
            "anomaly_details": anomaly_details,
            # F12: failed-file count from the tail (0 on clean/anomaly runs);
            # persisted to run_history for reports and the dashboard.
            "files_failed": files_failed,
        }

    except subprocess.TimeoutExpired:
        logger.error(f"LAN sync timed out after {lan_config.subprocess_timeout_seconds}s")
        return {
            "status": "LAN_FAILED",
            "exit_code": -1,
            "error": f"Timeout after {lan_config.subprocess_timeout_seconds}s — robocopy process killed",
            "anomaly_details": None,
            "files_failed": 0,
        }
    except FileNotFoundError as exc:
        logger.error("robocopy.exe not found - is this running on Windows Server?")
        return {
            "status": "LAN_FAILED",
            "exit_code": -1,
            "error": f"robocopy.exe not found: {exc}",
            "anomaly_details": None,
            "files_failed": 0,
        }
    except OSError as exc:
        logger.error(f"LAN sync OS error: {exc}")
        return {
            "status": "LAN_FAILED",
            "exit_code": -1,
            "error": str(exc),
            "anomaly_details": None,
            "files_failed": 0,
        }
    finally:
        if log_path and log_path.exists():
            try:
                log_path.unlink()
            except OSError:
                pass  # Temp file cleanup is best-effort; OS will eventually reclaim it
