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
            raise ValueError("/NC flag suppresses file class labels — parser has nothing to match")


_FAILED_LINE = re.compile(r"^\s*\*\*\s*FAILED:", re.MULTILINE)

# F12-fix (verified against real robocopy output, 2026-08-20): a file that
# cannot be copied does NOT produce a "** FAILED: <path>" line. Sharing
# violations and access denials print per-file
#     "ERROR <n> (0x...) Copying File <path>"
# blocks plus "ERROR: RETRY LIMIT EXCEEDED." — the legacy regex above matches
# none of them and always returned 0 in production. The authoritative count is
# robocopy's own job summary, which ends with:
#     Files :   <total> <copied> <skipped> <mismatch> <FAILED> <extras>
# Parsing it requires the job summary to be in the log — so /NJS must NOT be
# set (removed from build_robocopy_command; the ~10 summary lines are cheap).
_SUMMARY_FILES_LINE = re.compile(
    r"^\s*Files\s*:\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
    re.MULTILINE,
)


def count_failed_lines(log_text: str) -> int:
    """Legacy fallback: count "** FAILED: <path>" lines (see F12-fix note).

    Real robocopy output rarely contains such lines; kept because some log
    variants/tools do, and as a secondary signal when the job summary is
    absent from the captured tail.
    """
    return len(_FAILED_LINE.findall(log_text))


def count_failed_files_from_summary(log_text: str) -> int | None:
    """Return the FAILED count from robocopy's job summary (Files line).

    Returns None when no job summary is present in the text (caller should
    fall back to count_failed_lines). Uses the LAST summary line so a log
    containing multiple runs still reports the final one.
    """
    matches = _SUMMARY_FILES_LINE.findall(log_text)
    if not matches:
        return None
    return int(matches[-1][4])


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
        (the job summary is INTENTIONALLY kept — F12-fix: the FAILED column of
        the "Files :" summary line is the authoritative failed-file count; do
        NOT add /NJS back)
        /NDL    — No directory list (individual file lines are sufficient).
        /NP     — No progress percentage (meaningless in log files).
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
            # F12 (fixed): count which files actually failed. Primary source is
            # robocopy's own job summary ("Files : ... FAILED ..." line —
            # authoritative, verified against real output); the legacy
            # "** FAILED:" line count is only a fallback for logs without a
            # summary. The operator gets a number (and the per-file ERROR
            # blocks in the tail) instead of an opaque exit code.
            summary_failed = count_failed_files_from_summary(error_msg)
            if summary_failed is not None:
                files_failed = summary_failed
            else:
                files_failed = count_failed_lines(error_msg)
            logger.error(
                f"LAN sync FAILED (exit {result.returncode}) — "
                f"{len(error_msg)} bytes of log captured in result['error'], "
                f"{files_failed} failed file line(s) counted in the tail"
            )

        elif 4 <= result.returncode <= 7:
            # Anomaly only: bit 2 set (mismatches/extras), no copy errors.
            # Sync completed. Capture a short log tail so operators can diagnose
            # the anomaly if the warning is investigated — but do NOT set `error`
            # so alert systems are not triggered.
            anomaly_details = _read_log_tail(log_path, _ANOMALY_LOG_TAIL)
            logger.warning(
                f"LAN sync anomalies detected (exit {result.returncode}) — "
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
        logger.error("robocopy.exe not found — is this running on Windows Server?")
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
