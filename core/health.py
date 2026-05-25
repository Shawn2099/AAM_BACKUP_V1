"""Pre-backup health checks — source drive, required binaries."""

import shutil
from pathlib import Path

from loguru import logger


class HealthError(RuntimeError):
    """Raised when a pre-backup health check fails."""


def check_source_drive(source_path: str) -> tuple[bool, str]:
    """Verify source drive exists and has files.

    Returns:
        (True, "") if healthy.
        (False, "reason") if check failed.
    """
    source = Path(source_path)
    if not source.exists():
        return False, f"Source drive not accessible: {source}"

    try:
        file_count = sum(1 for _ in source.rglob("*") if _.is_file())
    except PermissionError:
        return False, f"Source drive permission denied: {source}"
    except OSError as e:
        return False, f"Source drive error: {e}"

    if file_count == 0:
        return False, f"Source drive appears empty: {source}"

    logger.debug(f"Source drive OK: {source} ({file_count} files)")
    return True, ""


def check_binary_exists(name: str) -> bool:
    """Check if binary is available in PATH."""
    return shutil.which(name) is not None


def pre_backup_health(source_path: str, mode: str) -> None:
    """Run all pre-backup health checks. Raises HealthError on failure.

    Args:
        source_path: Source drive root path.
        mode: "cloud", "lan", or "all"

    Raises:
        HealthError: If any check fails.
    """
    ok, reason = check_source_drive(source_path)
    if not ok:
        raise HealthError(reason)

    if mode in ("cloud", "all"):
        if not check_binary_exists("rclone"):
            raise HealthError("rclone not found in PATH")

    if mode in ("lan", "all"):
        if not check_binary_exists("robocopy"):
            raise HealthError("robocopy not found in PATH")

    logger.info(f"Pre-backup health check passed (mode={mode})")
