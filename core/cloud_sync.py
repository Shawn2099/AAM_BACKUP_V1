"""Cloud sync — rclone sync wrapper with temp config and exit classification.

Reference: AAM_BACKUP_V2/core/rclone.py — proven classification and temp config pattern.
"""

import subprocess
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
    bwlimit: str = "10M",
    retries: int = 3,
    storage_class: str = "COLDLINE",
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
    location: str = "asia-south1",
    project_number: str = "920173882190",
    bwlimit: str = "10M",
    retries: int = 3,
    storage_class: str = "COLDLINE",
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

    try:
        config_path = write_temp_config(gcs_key_path, location, project_number, storage_class)
        cmd = build_rclone_sync_command(source, bucket, fy_prefix, config_path, bwlimit, retries, storage_class, transfers, checkers)

        logger.info(f"Cloud sync: {source} → {bucket}/{fy_prefix}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        status = classify_rclone_exit(result.returncode)
        logger.info(f"Cloud sync exit {result.returncode} → {status}")

        return {
            "status": status,
            "exit_code": result.returncode,
            "error": None,
        }

    except subprocess.TimeoutExpired:
        logger.error(f"Cloud sync timed out after {timeout}s")
        return {"status": "CLOUD_FAILED", "exit_code": -1, "error": "Timeout"}
    except FileNotFoundError:
        logger.error("rclone not found")
        return {"status": "CLOUD_FAILED", "exit_code": -1, "error": "rclone not found"}
    except OSError as e:
        logger.error(f"Cloud sync error: {e}")
        return {"status": "CLOUD_FAILED", "exit_code": -1, "error": str(e)}
    finally:
        if config_path:
            try:
                Path(config_path).unlink()
            except OSError:
                pass
