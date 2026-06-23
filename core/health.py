"""Pre-backup health checks — source drive, binaries, disk space, clock, GCS key."""

import http.client
import shutil
from email.utils import parsedate_to_datetime
from pathlib import Path

import pendulum
from loguru import logger

from core.process import resolve_binary


class HealthError(RuntimeError):
    """Raised when a pre-backup health check fails."""


def check_source_drive(source_path: str, min_free_gb: int = 1) -> tuple[bool, str]:
    """Verify source drive exists, has files, and has free space.

    Args:
        min_free_gb: Minimum free space required (GB). Override via config.health.min_free_source_gb.

    Returns:
        (True, "") if healthy.
        (False, "reason") if check failed.
    """
    source = Path(source_path)
    if not source.exists():
        return False, f"Source drive not accessible: {source}"

    try:
        has_files = any(source.iterdir())
    except PermissionError:
        return False, f"Source drive permission denied: {source}"
    except OSError as e:
        return False, f"Source drive error: {e}"

    if not has_files:
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
            f"Source drive OK: {source} (contains files, {free_gb:.1f} GB free)"
        )
    except OSError:
        logger.warning(f"Could not check disk space on {source} — skipping")

    return True, ""


def check_binary_exists(name: str) -> bool:
    """Check if binary is available locally or in PATH."""
    return resolve_binary(name) is not None


def check_gcs_key(key_path: str) -> tuple[bool, str]:
    """Verify GCS service account key file exists."""
    kp = Path(key_path)
    if not kp.exists():
        return False, f"GCS key file not found: {key_path}"
    if kp.stat().st_size == 0:
        return False, f"GCS key file is empty: {key_path}"
    return True, ""


def check_clock_skew(
    max_skew_seconds: int = 600,
    connection_timeout: int = 10,
) -> tuple[bool, str]:
    """Verify system clock is within acceptable skew for GCS JWT auth.

    Compares local UTC time against Google's HTTP Date header.
    GCS OAuth JWT tokens are rejected if clock skew >10 minutes.

    Args:
        max_skew_seconds: Override via config.health.max_clock_skew_seconds.
        connection_timeout: Override via config.health.clock_check_timeout_seconds.
    """
    try:
        conn = http.client.HTTPSConnection("www.googleapis.com", timeout=connection_timeout)
        conn.request("HEAD", "/")
        resp = conn.getresponse()
        google_date_str = resp.getheader("Date")
        conn.close()

        if not google_date_str:
            return False, "Could not retrieve Date header from Google"

        google_time = parsedate_to_datetime(google_date_str)
        local_utc = pendulum.now("UTC")
        difference = abs((local_utc - google_time).total_seconds())

        if difference > max_skew_seconds:
            return False, (
                f"System clock skew detected: {difference:.0f}s difference from Google time "
                f"(max allowed: {max_skew_seconds}s). Run 'w32tm /resync'."
            )

        logger.debug(f"Clock OK: {difference:.1f}s skew from Google (limit: {max_skew_seconds}s)")
        return True, ""
    except OSError as e:
        logger.warning(f"Could not verify clock skew (Google not reachable): {e} — skipping")
        return True, ""
    except ValueError as e:
        logger.warning(f"Could not parse Google Date header: {e} — skipping")
        return True, ""


def pre_backup_health(
    source_path: str,
    mode: str,
    gcs_key_path: str | None = None,
    min_free_source_gb: int = 1,
    max_clock_skew_seconds: int = 600,
    clock_check_timeout_seconds: int = 10,
) -> None:
    """Run all pre-backup health checks. Raises HealthError on failure.

    Args:
        source_path: Source drive root path.
        mode: "cloud", "lan", or "all"
        gcs_key_path: Path to GCS service account key. Required for cloud mode.
        min_free_source_gb: Override via config.health.min_free_source_gb.
        max_clock_skew_seconds: Override via config.health.max_clock_skew_seconds.
        clock_check_timeout_seconds: Override via config.health.clock_check_timeout_seconds.

    Raises:
        HealthError: If any critical check fails — key missing, clock skewed,
            rclone/robocopy not found, or source drive inaccessible.
    """
    valid_modes = {"cloud", "lan", "all"}
    if mode not in valid_modes:
        raise HealthError(f"Invalid mode '{mode}' — expected one of: {', '.join(sorted(valid_modes))}")

    ok, reason = check_source_drive(source_path, min_free_gb=min_free_source_gb)
    if not ok:
        raise HealthError(reason)

    if mode in ("cloud", "all"):
        if not check_binary_exists("rclone"):
            raise HealthError("rclone not found in PATH")
        if gcs_key_path:
            key_ok, key_reason = check_gcs_key(gcs_key_path)
            if not key_ok:
                raise HealthError(f"GCS key check failed: {key_reason}")
        clock_ok, clock_reason = check_clock_skew(
            max_skew_seconds=max_clock_skew_seconds,
            connection_timeout=clock_check_timeout_seconds,
        )
        if not clock_ok:
            # Clock skew >10 min causes GCS JWT authentication to be rejected
            raise HealthError(f"Clock skew exceeds limit: {clock_reason}")

    if mode in ("lan", "all"):
        if not check_binary_exists("robocopy"):
            raise HealthError("robocopy not found in PATH")

    logger.info(f"Pre-backup health check passed (mode={mode})")
