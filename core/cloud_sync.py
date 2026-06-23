"""Cloud sync — rclone sync wrapper with temp config and exit classification.

Reference: AAM_BACKUP_V2/core/rclone.py — proven classification and temp config pattern.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from core.rclone_config import temp_rclone_config


def classify_rclone_exit(code: int) -> str:
    """Classify rclone exit code per official documentation.

    0  → CLOUD_COMPLETE  (all files synced)
    1  → CLOUD_FAILED     (syntax/usage)
    2  → CLOUD_FAILED     (error not otherwise categorised)
    3  → CLOUD_FAILED     (directory not found)
    4  → CLOUD_PARTIAL    (file not found — transient)
    5  → CLOUD_PARTIAL    (temporary — network, retryable)
    6  → CLOUD_FAILED     (less serious — NoRetry errors)
    7  → CLOUD_FAILED     (fatal — auth, bucket, critical)
    8  → CLOUD_FAILED     (transfer limit exceeded)
    9  → CLOUD_NO_CHANGES_COMPLETE (no files transferred — requires --error-on-no-transfer)
    10 → CLOUD_PARTIAL    (duration limit hit)
    """
    mapping = {
        0: "CLOUD_COMPLETE",
        1: "CLOUD_FAILED",
        2: "CLOUD_FAILED",
        3: "CLOUD_FAILED",
        4: "CLOUD_PARTIAL",
        5: "CLOUD_PARTIAL",
        6: "CLOUD_FAILED",
        7: "CLOUD_FAILED",
        8: "CLOUD_FAILED",
        9: "CLOUD_NO_CHANGES_COMPLETE",
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
    transfers: int = 2,
    checkers: int = 4,
    max_delete_percent: int = 45,
) -> list[str]:
    """Build rclone sync command with GCS-optimized flags.

    max_delete_percent: ransomware kill-switch — rclone aborts the entire sync
    (exit code 8, CLOUD_FAILED) if deletions would exceed this % of destination
    file count. Nothing is written or deleted when it triggers.
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"

    return [
        "rclone", "sync",
        source, dest,
        "--config", config_path,
        "--fast-list",
        "--gcs-no-check-bucket",
        "--gcs-storage-class", storage_class,
        "--error-on-no-transfer",
        "--modify-window", "1s",
        "--bwlimit", bwlimit,
        "--transfers", str(transfers),
        "--checkers", str(checkers),
        "--retries", str(retries),
        "--retries-sleep", "30s",
        "--track-renames",
        "--max-delete", str(max_delete_percent),
        "--check-first",         # Finish all stat/hash checks before any upload starts.
                                  # Separates random-seek metadata phase from sequential
                                  # read-for-upload phase — critical for HDD head efficiency.
        "--buffer-size", "64M",  # Upload read buffer per transfer slot.
                                  # 2 transfers × 64M = 128M total. Matches GCS multipart
                                  # chunk sizing without wasting RAM. (256M was too large;
                                  # --use-mmap removed — documented as unstable on Windows.)
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
    transfers: int = 2,
    checkers: int = 4,
    max_delete_percent: int = 45,
    timeout: int = 21600,
) -> dict:
    """Execute rclone sync to mirror source → GCS.

    Creates temp config, executes sync, cleans up in finally.

    max_delete_percent: ransomware kill-switch threshold. If rclone would delete
    more than this % of destination files it aborts with exit code 8 (CLOUD_FAILED)
    and leaves the bucket untouched.

    Returns:
        {"status": str, "exit_code": int, "error": str | None}
    """
    stderr_path = None

    with temp_rclone_config(
        gcs_key_path, location, project_number, storage_class
    ) as config_path:
        cmd = build_rclone_sync_command(
            source, bucket, fy_prefix, config_path, storage_class,
            bwlimit, retries, transfers, checkers,
            max_delete_percent=max_delete_percent,
        )

        logger.info(f"Cloud sync: {source} → {bucket}/{fy_prefix}")

        stderr_fd, stderr_path = tempfile.mkstemp(suffix=".log", prefix="cloud_sync_stderr_")
        os.close(stderr_fd)
        try:
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
            if result.returncode != 0 and result.returncode != 9:
                try:
                    stderr_text = Path(stderr_path).read_text(encoding="utf-8")
                    error_msg = stderr_text[:100000] if len(stderr_text) > 100000 else stderr_text
                    logger.error(f"rclone error: {error_msg}")
                except OSError:
                    error_msg = f"rclone exit {result.returncode} (stderr unreadable)"

            return {
                "status": status,
                "exit_code": result.returncode,
                "error": error_msg,
            }

        except subprocess.TimeoutExpired:
            logger.error(f"Cloud sync timed out after {timeout}s")
            return {"status": "CLOUD_FAILED", "exit_code": -1, "error": f"Timeout after {timeout}s"}
        except FileNotFoundError:
            logger.error("rclone not found")
            return {"status": "CLOUD_FAILED", "exit_code": -1, "error": "rclone not found"}
        except OSError as e:
            logger.error(f"Cloud sync OS error: {e}")
            return {"status": "CLOUD_FAILED", "exit_code": -1, "error": str(e)}
        finally:
            if stderr_path:
                try:
                    Path(stderr_path).unlink()
                except OSError:
                    pass
