"""Cloud sync — rclone sync wrapper with temp config and exit classification.

Reference: AAM_BACKUP_V2/core/rclone.py — proven classification and temp config pattern.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from core.process import resolve_binary
from core.rclone_config import temp_rclone_config


# P1-EXIT9: severity floor that disqualifies an exit-9 from "no changes".
_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
# R1 hardening: some fatal errors bypass the JSON logger (log.Fatalf paths,
# rclone issue #6038). These plaintext markers are treated as error signals.
_FATAL_PLAINTEXT_MARKERS = ("failed to", "notices: failed", "notice: failed")


def scan_rclone_log_for_errors(log_text: str) -> tuple[bool, str]:
    """Scan rclone's --use-json-log stream for ERROR/CRITICAL signals.

    Returns (has_error_signal, error_tail). A line counts as an error signal if:
      * it parses as JSON and its "level" is ERROR or CRITICAL, OR
      * it does not parse as JSON and carries a known fatal marker
        ("Failed to ...") - the documented escape hatch of the JSON logger.

    The tail returned for flagged logs contains the offending lines so the
    operator sees WHY an exit-9 was reclassified instead of trusting it.
    """
    bad_lines: list[str] = []
    for raw_line in (log_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            level = str(entry.get("level", "")).upper()
            if _LOG_LEVELS.get(level, 0) >= _LOG_LEVELS["ERROR"]:
                bad_lines.append(line)
        except (json.JSONDecodeError, ValueError):
            lowered = line.lower()
            if any(marker in lowered for marker in _FATAL_PLAINTEXT_MARKERS):
                bad_lines.append(line)
    return bool(bad_lines), "\n".join(bad_lines[-10:])


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


def resolve_max_duration_seconds(timeout: int, configured: int | None) -> int | None:
    """C-A: compute the effective --max-duration for a cloud sync.

    Precedence:
      * configured value > 0  -> used as-is (operator override)
      * configured == 0       -> None (cap disabled)
      * configured is None    -> auto: timeout minus a 300s margin so rclone
        self-terminates BEFORE the hard subprocess kill; if the timeout is
        too small to hold the margin, the cap stays off rather than negative.
    """
    if configured is not None:
        return int(configured) if configured > 0 else None
    margin = 300
    auto = int(timeout) - margin
    return auto if auto > 0 else None


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
    max_duration_seconds: int | None = None,
) -> list[str]:
    """Build rclone sync command with GCS-optimized flags.

    All tunable values are passed as parameters — caller draws them from config.
    """
    from core.health import check_source_drive as _check_src
    ok, reason = _check_src(source)
    if not ok and "appears empty" in reason:
        raise ValueError(reason)
    dest = f"aam_gcs:{bucket}/{fy_prefix}"

    # M7: resolve the binary like every other rclone-calling module
    # (preflight, verify, size, manifest, diff) so a bundled deploy/bin copy
    # is preferred and preflight/sync can never disagree about the binary.
    rclone_exe = resolve_binary("rclone") or "rclone"

    cmd = [
        rclone_exe, "sync",
        source, dest,
        "--config", config_path,
        "--fast-list",
        "--gcs-no-check-bucket",
        "--gcs-storage-class", storage_class,
        "--error-on-no-transfer",
        "--modify-window", "2s",    # NTFS mtime granularity is 2 seconds.
                                     # Prevents false-positive re-uploads when a file
                                     # is saved twice within the same NTFS tick.
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

    # C-A: graceful self-termination inside the window. SOFT cutoff lets the
    # in-flight transfer finish and preserves .partial state; retries-sleep
    # is pinned above so a retry cannot silently reset the deadline.
    if max_duration_seconds:
        cmd.extend([
            "--max-duration", f"{int(max_duration_seconds)}s",
            "--cutoff-mode", "SOFT",
        ])

    return cmd


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
    max_duration_seconds: int | None = None,
) -> dict:
    """Execute rclone sync to mirror source → GCS.

    Creates temp config, executes sync, cleans up in finally.
    max_duration_seconds: C-A override; when None the cap is auto-derived
    from the subprocess timeout minus a 300s margin (see
    resolve_max_duration_seconds).

    Returns:
        {"status": str, "exit_code": int, "error": str | None}
    """
    stderr_path = None
    effective_max_duration = resolve_max_duration_seconds(
        timeout=timeout, configured=max_duration_seconds,
    )

    with temp_rclone_config(
        gcs_key_path, location, project_number, storage_class
    ) as config_path:
        cmd = build_rclone_sync_command(
            source, bucket, fy_prefix, config_path, storage_class,
            bwlimit, retries, transfers, checkers,
            buffer_size=buffer_size,
            max_duration_seconds=effective_max_duration,
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

            error_msg = None
            if result.returncode == 9:
                # P1-EXIT9: exit 9 only means "no files transferred" when the
                # log stream is clean. A fatal error (missing bucket, auth,
                # bad remote) ALSO produces exit 9 under --error-on-no-transfer;
                # trusting it blindly recorded CLOUD_NO_CHANGES_COMPLETE for
                # broken runs (CLOUD-06/07). Reclassify when signals exist.
                try:
                    stderr_text = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
                    has_error, error_tail = scan_rclone_log_for_errors(stderr_text)
                except OSError:
                    has_error, error_tail = False, ""
                if has_error:
                    status = "CLOUD_FAILED"
                    error_msg = (
                        f"rclone exited 9 but logged fatal errors - reclassified "
                        f"CLOUD_NO_CHANGES_COMPLETE -> CLOUD_FAILED:\n{error_tail}"
                    )
                    logger.error(error_msg)
                else:
                    logger.info("Cloud sync: no changes to transfer")
            elif result.returncode != 0:
                try:
                    error_msg = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
                    logger.error(f"rclone error: {error_msg}")
                except OSError:
                    error_msg = f"rclone exit {result.returncode} (stderr unreadable)"

            logger.info(f"Cloud sync exit {result.returncode} -> {status}")

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
