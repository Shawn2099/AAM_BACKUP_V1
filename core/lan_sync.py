"""LAN sync — robocopy /MIR wrapper with exit code classification.

Reference: AAM_BACKUP_V2/core/robocopy.py — proven bitmask logic and flag set.
"""

import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from models.config import LanConfig

# ═══════════════════════════════════════════════════════════════
# Flag validation — /NC is FORBIDDEN
# Source: ConvertFrom-RobocopLog §Notes
# ═══════════════════════════════════════════════════════════════
_FORBIDDEN_FLAGS = ["/NC"]


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

    /V /TS /FP /BYTES — verbose per-file logging with full paths and timestamps.
    /NJH /NJS /NDL /NP — suppress headers, summaries, directory lists, and progress.
    """
    flags = [
        "/MIR",
        "/Z",
        "/XJ",
        "/MT:8",
        f"/R:{lan_config.retry_count}",
        f"/W:{lan_config.retry_wait_seconds}",
        "/V", "/TS", "/FP", "/BYTES",
        "/NJH", "/NJS", "/NDL", "/NP",
        "/XD", "System Volume Information",
    ]

    _validate_required_flags(flags)
    return ["robocopy", source, dest, *flags]


def run_lan_sync(source: str, dest: str, lan_config: LanConfig) -> dict:
    """Execute robocopy /MIR mirror sync.

    Writes output to temp log file, classifies exit code, cleans up in finally.

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
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".log",
            prefix="robocopy_sync_",
            delete=False,
        ) as log_file:
            log_path = Path(log_file.name)

        cmd.extend([f"/LOG:{log_path}"])

        logger.info(f"LAN sync: {' '.join(cmd[:4])}... (log: {log_path})")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=lan_config.subprocess_timeout_seconds,
        )

        status = classify_exit_code(result.returncode)
        logger.info(f"LAN sync exit {result.returncode} → {status}")

        return {
            "status": status,
            "exit_code": result.returncode,
            "log_path": str(log_path),
            "error": None,
        }

    except subprocess.TimeoutExpired:
        logger.error(f"LAN sync timed out after {lan_config.subprocess_timeout_seconds}s")
        return {"status": "LAN_FAILED", "exit_code": -1, "log_path": None, "error": "Timeout"}
    except FileNotFoundError:
        logger.error("robocopy.exe not found")
        return {"status": "LAN_FAILED", "exit_code": -1, "log_path": None, "error": "robocopy.exe not found"}
    except OSError as e:
        logger.error(f"LAN sync OS error: {e}")
        return {"status": "LAN_FAILED", "exit_code": -1, "log_path": None, "error": str(e)}
    finally:
        if log_path and log_path.exists():
            try:
                log_path.unlink()
            except OSError:
                pass
