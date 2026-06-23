"""LAN sync — robocopy /MIR wrapper with exit code classification.

Reference: AAM_BACKUP_V2/core/robocopy.py — proven bitmask logic and flag set.
Exit code reference: https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/robocopy
"""

import os
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from models.config import LanConfig
from core.process import resolve_binary

# ═══════════════════════════════════════════════════════════════
# Flag validation — /NC is FORBIDDEN
# Source: ConvertFrom-RobocopLog §Notes
# /NC suppresses file class labels (e.g. "New File", "Changed") which
# the downstream log parser relies on to categorise per-file outcomes.
# ═══════════════════════════════════════════════════════════════

# Tail sizes for log capture — enough for full context, bounded to prevent
# oversized payloads in the result dict.
_ERROR_LOG_TAIL = 100_000   # bytes — full error context for real failures (codes 8-15, 16+)
_ANOMALY_LOG_TAIL = 5_000   # bytes — limited context for anomalies (codes 4-7); no alert, just forensics


def _validate_required_flags(flags: list[str]) -> None:
    for f in flags:
        if f.upper() in ("/NC", "-NC"):
            raise ValueError("/NC flag suppresses file class labels — parser has nothing to match")


def _read_log_tail(log_path: Path, max_bytes: int) -> str:
    """Read the tail of a robocopy log file, bounded to max_bytes.

    Robocopy writes summary and error details at the end of the log.
    Reading the tail rather than the head ensures we always get the
    most actionable diagnostic data regardless of log file size.

    Returns the raw text tail or a fallback message if the file is unreadable.
    """
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        return log_text[-max_bytes:] if len(log_text) > max_bytes else log_text
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
        /NJS    — No job summary (we parse exit code, not summary text).
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
        "/NJH", "/NJS", "/NDL", "/NP",
        "/XD", "System Volume Information",
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

        if status == "LAN_FAILED" or (result.returncode & 8):
            # Real failure: bit 4 (fatal) or bit 3 (copy errors) set.
            # Capture full log tail for alert system and operator triage.
            error_msg = _read_log_tail(log_path, _ERROR_LOG_TAIL)
            logger.error(
                f"LAN sync FAILED (exit {result.returncode}) — "
                f"{len(error_msg)} bytes of log captured in result['error']"
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
        }

    except subprocess.TimeoutExpired:
        logger.error(f"LAN sync timed out after {lan_config.subprocess_timeout_seconds}s")
        return {
            "status": "LAN_FAILED",
            "exit_code": -1,
            "error": f"Timeout after {lan_config.subprocess_timeout_seconds}s — robocopy process killed",
            "anomaly_details": None,
        }
    except FileNotFoundError as exc:
        logger.error("robocopy.exe not found — is this running on Windows Server?")
        return {
            "status": "LAN_FAILED",
            "exit_code": -1,
            "error": f"robocopy.exe not found: {exc}",
            "anomaly_details": None,
        }
    except OSError as exc:
        logger.error(f"LAN sync OS error: {exc}")
        return {
            "status": "LAN_FAILED",
            "exit_code": -1,
            "error": str(exc),
            "anomaly_details": None,
        }
    finally:
        if log_path and log_path.exists():
            try:
                log_path.unlink()
            except OSError:
                pass  # Temp file cleanup is best-effort; OS will eventually reclaim it
