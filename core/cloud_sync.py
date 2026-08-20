"""Cloud sync — rclone sync wrapper with temp config and exit classification.

Reference: AAM_BACKUP_V2/core/rclone.py — proven classification and temp config pattern.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from core.process import resolve_binary
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
    buffer_size: str = "64M",
) -> list[str]:
    """Build rclone sync command with GCS-optimized flags.

    All tunable values are passed as parameters — caller draws them from config.
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"

    # M6/S2-13: resolve rclone the same way preflight/verify do (deploy/bin
    # first, then PATH). A bare "rclone" resolved through the OS PATH — and
    # in production that picked up a DIFFERENT version from
    # C:\Windows\system32 (1.74.2) while preflight/verify used deploy\bin
    # (1.74.3): the sync and the verify that follows it could disagree on
    # behavior.
    rclone_exe = resolve_binary("rclone") or "rclone"

    return [
        rclone_exe, "sync",
        source, dest,
        "--config", config_path,
        "--fast-list",
        "--gcs-no-check-bucket",
        "--gcs-storage-class", storage_class,
        "--error-on-no-transfer",
        # S2-30: --modify-window 2s REMOVED. With the window, rclone treated
        # a same-size resave whose mtime landed within 2 s of the GCS
        # object's mtime as UNCHANGED — and it stayed skipped on EVERY
        # subsequent run (reproduced on real GCS, session-2 E1: sync exit 9,
        # object byte-verified STALE). The "NTFS mtime granularity is 2 s"
        # rationale was wrong (NTFS FILETIME is 100 ns; 2 s is FAT). Without
        # the window, rclone compares size + exact mtime: GCS stores the
        # source mtime verbatim (x-gcs-mtime), so unchanged files still
        # match exactly (no re-upload storm — proven by test_cloud_02's
        # idempotent second run on real hardware) while changed files are
        # re-uploaded (proven by test_cloud_11, the E1 scenario).
        "--bwlimit", bwlimit,
        "--transfers", str(transfers),
        "--checkers", str(checkers),
        "--retries", str(retries),
        "--retries-sleep", "30s",
        "--check-first",            # Finish all stat/hash checks before any upload starts.
                                     # Separates random-seek metadata phase from sequential
                                     # read-for-upload phase — critical for HDD head efficiency.
        "--buffer-size", buffer_size,  # Upload read buffer per transfer slot.
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
    buffer_size: str = "64M",
    timeout: int = 21600,
) -> dict:
    """Execute rclone sync to mirror source → GCS.

    Creates temp config, executes sync, cleans up in finally.

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
            buffer_size=buffer_size,
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
            if result.returncode == 9:
                logger.info("Cloud sync: no changes to transfer")
            elif result.returncode != 0:
                try:
                    error_msg = Path(stderr_path).read_text(encoding="utf-8")
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
            # G7: make the operator-facing message accurate — rclone sync is
            # resumable, so a timeout is an interruption, not a data event.
            return {"status": "CLOUD_FAILED", "exit_code": -1, "error": (
                f"Timeout after {timeout}s — rclone sync is resumable; progress is "
                "preserved and the next run continues from where this one left off"
            )}
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
