"""Cloud sync — rclone sync wrapper with temp config and exit classification.

Reference: AAM_BACKUP_V2/core/rclone.py — proven classification and temp config pattern.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from core.rclone_config import write_temp_config


def classify_rclone_exit(code: int) -> str:
    """Classify rclone exit code per official documentation.

    0  → CLOUD_COMPLETE  (all files synced)
    1  → CLOUD_FAILED     (uncategorised error)
    2  → CLOUD_FAILED     (syntax/usage)
    3  → CLOUD_FAILED     (directory not found)
    4  → CLOUD_PARTIAL    (file not found — transient)
    5  → CLOUD_PARTIAL    (temporary — network, retryable)
    6  → CLOUD_PARTIAL    (less serious — partial transfer)
    7  → CLOUD_FAILED     (fatal — auth, bucket, critical)
    8  → CLOUD_FAILED     (transfer limit exceeded)
    9  → CLOUD_COMPLETE   (no files to transfer)
    10 → CLOUD_PARTIAL    (duration limit hit)
    """
    mapping = {
        0: "CLOUD_COMPLETE",
        1: "CLOUD_FAILED",
        2: "CLOUD_FAILED",
        3: "CLOUD_FAILED",
        4: "CLOUD_PARTIAL",
        5: "CLOUD_PARTIAL",
        6: "CLOUD_PARTIAL",
        7: "CLOUD_FAILED",
        8: "CLOUD_FAILED",
        9: "CLOUD_COMPLETE",
        10: "CLOUD_PARTIAL",
    }
    return mapping.get(code, "CLOUD_FAILED")


def build_rclone_sync_command(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
    storage_class: str,
    bwlimit: str = "10M",
    retries: int = 3,
    transfers: int = 4,
    checkers: int = 16,
) -> list[str]:
    """Build rclone sync command with GCS-optimized flags."""
    dest = f"aam_gcs:{bucket}/{fy_prefix}"

    return [
        "rclone", "sync",
        source, dest,
        "--config", config_path,
        "--fast-list",
        "--gcs-no-check-bucket",
        "--gcs-storage-class", storage_class,
        "--modify-window", "1s",
        "--bwlimit", bwlimit,
        "--transfers", str(transfers),
        "--checkers", str(checkers),
        "--retries", str(retries),
        "--retries-sleep", "30s",
        "--track-renames",
        "--no-traverse",
        "--use-json-log",
        "--log-level", "INFO",
        "--stats", "60s",
    ]


def run_cloud_sync(
    source: str,
    bucket: str,
    fy_prefix: str,
    gcs_key_path: str,
    project_number: str,
    storage_class: str,
    location: str = "asia-south1",
    bwlimit: str = "10M",
    retries: int = 3,
    transfers: int = 4,
    checkers: int = 16,
    timeout: int = 21600,
) -> dict:
    """Execute rclone sync to mirror source → GCS.

    Creates temp config, executes sync, cleans up in finally.

    Returns:
        {"status": str, "exit_code": int, "error": str | None}
    """
    config_path = None
    stderr_path = None

    try:
        config_path = write_temp_config(
            gcs_key_path=gcs_key_path,
            location=location,
            project_number=project_number,
            storage_class=storage_class,
        )
        cmd = build_rclone_sync_command(source, bucket, fy_prefix, config_path, storage_class, bwlimit, retries, transfers, checkers)

        logger.info(f"Cloud sync: {source} → {bucket}/{fy_prefix}")

        stderr_fd, stderr_path = tempfile.mkstemp(suffix=".log", prefix="cloud_sync_stderr_")
        os.close(stderr_fd)

        with open(stderr_path, "w", encoding="utf-8") as stderr_file:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
                text=True,
                timeout=timeout,
            )

        status = classify_rclone_exit(result.returncode)
        logger.info(f"Cloud sync exit {result.returncode} → {status}")

        error_msg = None
        if result.returncode != 0:
            try:
                stderr_text = Path(stderr_path).read_text(encoding="utf-8")
                error_msg = stderr_text[:2000] if len(stderr_text) > 2000 else stderr_text
                logger.error(f"rclone error: {error_msg}")
            except OSError:
                error_msg = f"rclone exit {result.returncode} (stderr unreadable)"

        return {
            "status": status,
            "exit_code": result.returncode,
            "error": error_msg,
        }

    except subprocess.TimeoutExpired as e:
        logger.error(f"Cloud sync timed out after {timeout}s")
        return {"status": "CLOUD_FAILED", "exit_code": -1, "error": f"Timeout after {timeout}s"}
    except FileNotFoundError as e:
        logger.error("rclone not found")
        return {"status": "CLOUD_FAILED", "exit_code": -1, "error": f"rclone not found: {e}"}
    except OSError as e:
        logger.error(f"Cloud sync OS error: {e}")
        return {"status": "CLOUD_FAILED", "exit_code": -1, "error": str(e)}
    finally:
        if config_path:
            try:
                Path(config_path).unlink()
            except OSError:
                pass
        if stderr_path:
            try:
                Path(stderr_path).unlink()
            except OSError:
                pass
