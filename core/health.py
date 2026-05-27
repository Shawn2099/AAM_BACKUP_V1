"""Pre-backup health checks — source drive, binaries, disk space, clock, GCS key."""

import http.client
import shutil
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from loguru import logger


class HealthError(RuntimeError):
    """Raised when a pre-backup health check fails."""


def check_source_drive(source_path: str, min_free_gb: int = 1) -> tuple[bool, str]:
    """Verify source drive exists, has files, and has free space.

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

    try:
        usage = shutil.disk_usage(str(source))
        free_gb = usage.free / (1024**3)
        if free_gb < min_free_gb:
            return False, (
                f"Source drive critically low on space: {free_gb:.1f} GB free "
                f"(minimum: {min_free_gb} GB)"
            )
        logger.debug(
            f"Source drive OK: {source} ({file_count} files, {free_gb:.1f} GB free)"
        )
    except OSError:
        logger.warning(f"Could not check disk space on {source} — skipping")

    return True, ""


def check_binary_exists(name: str) -> bool:
    """Check if binary is available in PATH."""
    return shutil.which(name) is not None


def check_gcs_key(key_path: str) -> tuple[bool, str]:
    """Verify GCS service account key file exists."""
    kp = Path(key_path)
    if not kp.exists():
        return False, f"GCS key file not found: {key_path}"
    if kp.stat().st_size == 0:
        return False, f"GCS key file is empty: {key_path}"
    return True, ""


def check_clock_skew(max_skew_seconds: int = 600) -> tuple[bool, str]:
    """Verify system clock is within acceptable skew for GCS JWT auth.

    Compares local UTC time against Google's HTTP Date header.
    GCS OAuth JWT tokens are rejected if clock skew >10 minutes.
    """
    try:
        conn = http.client.HTTPSConnection("www.googleapis.com", timeout=10)
        conn.request("HEAD", "/")
        resp = conn.getresponse()
        google_date_str = resp.getheader("Date") or resp.getheader("date")
        conn.close()

        if not google_date_str:
            return False, "Could not retrieve Date header from Google"

        google_time = parsedate_to_datetime(google_date_str)
        local_utc = datetime.now(UTC)
        difference = abs((local_utc - google_time).total_seconds())

        if difference > max_skew_seconds:
            return False, (
                f"System clock skew detected: {difference:.0f}s difference from Google time "
                f"(max allowed: {max_skew_seconds}s). Run 'w32tm /resync'."
            )

        logger.debug(f"Clock OK: {difference:.1f}s skew from Google (limit: {max_skew_seconds}s)")
        return True, ""
    except Exception as e:
        logger.warning(f"Could not verify clock skew (Google not reachable): {e} — skipping")
        return True, ""


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
