"""LAN sync — robocopy /MIR wrapper with exit code classification.

Reference: AAM_BACKUP_V2/core/robocopy.py — proven bitmask logic and flag set.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from models.config import LanConfig

# ═══════════════════════════════════════════════════════════════
# Flag validation — /NC is FORBIDDEN
# Source: ConvertFrom-RobocopLog §Notes
# ═══════════════════════════════════════════════════════════════

def _validate_required_flags(flags: list[str]) -> None:
    for f in flags:
        if f.upper() in ("/NC", "-NC"):
            raise ValueError("/NC flag suppresses file class labels — parser has nothing to match")


def classify_exit_code(code: int) -> str:
    """Classify robocopy exit code using bitmask rules.

    Bit 0 (1): Files copied successfully
    Bit 1 (2): Extra files/directories detected
    Bit 2 (4): Mismatched files detected
    Bit 3 (8): Copy errors (some files failed)
    Bit 4 (16): Fatal error

    Returns: LAN_COMPLETE | LAN_PARTIAL | LAN_FAILED
    """
    if code & 16:
        return "LAN_FAILED"
    if code & 8:
        return "LAN_PARTIAL"
    if 0 <= code <= 7:
        return "LAN_COMPLETE"
    return "LAN_FAILED"


def build_robocopy_command(source: str, dest: str, lan_config: LanConfig) -> list[str]:
    """Build robocopy /MIR command with production-verified flags.

    /V /TS /FP — verbose per-file logging with full paths and timestamps.
    /NJH /NJS /NDL /NP — suppress headers, summaries, directory lists, and progress.
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
    return ["robocopy", source, dest, *flags]


def run_lan_sync(source: str, dest: str, lan_config: LanConfig) -> dict:
    """Execute robocopy /MIR mirror sync.

    Robocopy writes all output directly to the /LOG:path file.
    stdout/stderr from subprocess are discarded (they are empty when /LOG is set).
    Exit code and error details are read from the log file on failure.

    Args:
        source: Source drive path.
        dest: LAN UNC destination.
        lan_config: LAN configuration with retry/timeout settings.

    Returns:
        {"status": str, "exit_code": int, "error": str | None}
    """
    cmd = build_robocopy_command(source, dest, lan_config)
    log_path = None

    try:
        log_fd, log_path_str = tempfile.mkstemp(suffix=".log", prefix="robocopy_sync_")
        os.close(log_fd)  # Close handle so robocopy can write to it
        log_path = Path(log_path_str)

        cmd.extend([f"/LOG:{log_path}"])

        logger.info(f"LAN sync: {' '.join(cmd[:4])}... (log: {log_path})")

        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,  # robocopy writes via /LOG:path — stdout is empty
            stderr=subprocess.DEVNULL,  # same — no pipe buffer accumulation on long /MIR runs
            timeout=lan_config.subprocess_timeout_seconds,
        )

        status = classify_exit_code(result.returncode)
        logger.info(f"LAN sync exit {result.returncode} → {status}")

        error_msg = None
        if status == "LAN_FAILED":
            try:
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
                error_msg = log_text[-500:] if len(log_text) > 500 else log_text
            except OSError:
                error_msg = f"robocopy exit {result.returncode} (log unreadable)"

        return {
            "status": status,
            "exit_code": result.returncode,
            "error": error_msg,
        }

    except subprocess.TimeoutExpired as e:
        logger.error(f"LAN sync timed out after {lan_config.subprocess_timeout_seconds}s")
        return {"status": "LAN_FAILED", "exit_code": -1, "error": f"Timeout after {lan_config.subprocess_timeout_seconds}s"}
    except FileNotFoundError as e:
        logger.error("robocopy.exe not found")
        return {"status": "LAN_FAILED", "exit_code": -1, "error": f"robocopy.exe not found: {e}"}
    except OSError as e:
        logger.error(f"LAN sync OS error: {e}")
        return {"status": "LAN_FAILED", "exit_code": -1, "error": str(e)}
    finally:
        if log_path and log_path.exists():
            try:
                log_path.unlink()
            except OSError:
                pass
