"""Cloud sync — rclone sync wrapper with temp config and exit classification.

Reference: AAM_BACKUP_V2/core/rclone.py — proven classification and temp config pattern.
"""

import json

from loguru import logger

from core.process import resolve_binary
from core.rclone_config import temp_rclone_config
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool, tail_lines


# P1-EXIT9: severity floor that disqualifies an exit-9 from "no changes".
_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
# R1 hardening: some fatal errors bypass the JSON logger (log.Fatalf paths,
# rclone issue #6038). These plaintext markers are treated as error signals.
_FATAL_PLAINTEXT_MARKERS = ("failed to", "notices: failed", "notice: failed")


def scan_rclone_log_for_errors(log_text: str, max_lines: int = 10) -> tuple[bool, str]:
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
    return bool(bad_lines), "\n".join(bad_lines[-max_lines:] if max_lines > 0 else [])


def count_planned_deletes(log_text: str) -> int:
    """Files a real sync would delete, from an `rclone sync --dry-run` JSON log.

    rclone logs one NOTICE per planned file deletion with
    `"skipped":"delete"` — files inside folders that would be removed
    included; folder removals are separate `"remove directory"` lines and
    not counted (verified on rclone v1.74.3, 2026-10-14).
    """
    planned = 0
    for line in (log_text or "").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("skipped") == "delete":
            planned += 1
    return planned


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
    if type(code) is not int:
        return "CLOUD_FAILED"  # bool / None: never a usable exit code
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
    max_delete: int | None = None,
) -> list[str]:
    """Build rclone sync command with GCS-optimized flags.

    All tunable values are passed as parameters — caller draws them from config.
    max_delete: rclone's own `--max-delete` backstop (M2 2.2): the sync stops
    with a fatal error (exit 7) instead of deleting more files than G3 allows.
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
    if max_delete is not None:
        cmd.extend(["--max-delete", str(int(max_delete))])

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
    timeout: int = 18000,
    max_duration_seconds: int | None = None,
    artefacts: RunArtefacts | None = None,
    error_tail_lines: int = 100,
    dry_run: bool = False,
    max_delete: int | None = None,
) -> dict:
    """Execute rclone sync to mirror source -> GCS.

    dry_run=True runs the SAME command with `--dry-run` (nothing changes)
    and adds "planned_deletes" to the result: the number of files the real
    sync would delete, or None when the dry-run did not complete cleanly
    (guard G3 input). max_delete: `--max-delete` backstop for the real sync.

    The FULL rclone log is written to the run's artefact folder
    (`--log-file`, JSON lines) and kept (plan WP2). Operator-facing error
    text is bounded to the last `error_tail_lines` error lines.
    max_duration_seconds: C-A override; when None the cap is auto-derived
    from the subprocess timeout minus a 300s margin.

    Returns:
        {"status": str, "exit_code": int, "error": str | None,
         "suspect_reason": str | None, "log_path": str | None}

    status may be CLOUD_SUSPECT when exit 0 is contradicted by ERROR/CRITICAL
    log signals (C-DK-001 hardening). A clean kill with no log signals still
    exits 0 here — incompleteness is then caught by the flow's evidence gate.
    """
    artefacts = artefacts or RunArtefacts.ephemeral("cloud-sync")
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
            max_delete=None if dry_run else max_delete,
        )
        step = "rclone-dry-run" if dry_run else "rclone-sync"
        if dry_run:
            cmd.append("--dry-run")
        log_path = artefacts.new_path(step, "log")
        cmd.extend(["--log-file", str(log_path)])

        logger.info(f"Cloud {'dry-run' if dry_run else 'sync'}: {source} → "
                    f"{bucket}/{fy_prefix} (full log: {log_path})")
        result = run_tool(step, cmd, timeout_s=timeout,
                          artefacts=artefacts, log_path=log_path)

    base = {"suspect_reason": None, "log_path": str(log_path)}
    if dry_run:
        base["planned_deletes"] = None  # set below only for a clean, completed dry-run
    if result.spawn_error:
        missing = "FileNotFoundError" in result.spawn_error
        return {**base, "status": "CLOUD_FAILED", "exit_code": -1,
                "error": "rclone not found" if missing else result.spawn_error}
    if result.timed_out:
        # G7: a timeout is an interruption, not a data event — rclone sync
        # is resumable.
        return {**base, "status": "CLOUD_FAILED", "exit_code": -1, "error": (
            f"Timeout after {timeout}s — rclone sync is resumable; progress is "
            "preserved and the next run continues from where this one left off"
        )}

    code = result.exit_code
    status = classify_rclone_exit(code)
    # rclone writes its log to --log-file; anything printed before the log
    # file opened (fatal config/flag errors) lands on stderr. Scan both.
    log_text = "\n".join(t for t in (result.log_text(), result.stderr_text()) if t)
    has_error, error_tail = scan_rclone_log_for_errors(log_text, max_lines=error_tail_lines)

    error_msg = None
    suspect_reason = None
    if code == 0 and has_error:
        # C-DK-001: error signals on a "clean" exit are contradictory
        # evidence and must not pass silently.
        status = "CLOUD_SUSPECT"
        suspect_reason = (
            "rclone exited 0 but logged ERROR/CRITICAL signals - "
            "reclassified CLOUD_COMPLETE -> CLOUD_SUSPECT:\n"
            f"{error_tail}"
        )
        error_msg = suspect_reason
        logger.error(suspect_reason)
    elif code == 9:
        # P1-EXIT9: exit 9 only means "no files transferred" when the log
        # stream is clean; fatal errors also produce exit 9 under
        # --error-on-no-transfer.
        if has_error:
            status = "CLOUD_FAILED"
            error_msg = (
                f"rclone exited 9 but logged fatal errors - reclassified "
                f"CLOUD_NO_CHANGES_COMPLETE -> CLOUD_FAILED:\n{error_tail}"
            )
            logger.error(error_msg)
        else:
            logger.info("Cloud sync: no changes to transfer")
    elif code != 0:
        detail = error_tail or tail_lines(log_text, error_tail_lines)
        error_msg = (
            f"rclone exit {code}: {detail}" if detail
            else f"rclone exit {code} (no log output)"
        )
        logger.error(f"rclone error (exit {code}); full log: {log_path}\n{detail}")

    logger.info(f"Cloud {'dry-run' if dry_run else 'sync'} exit {code} -> {status}")
    if dry_run and status in ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"):
        # exit 0 = copies planned, 9 = none planned (--error-on-no-transfer);
        # planned deletions do not change the exit code (verified v1.74.3).
        base["planned_deletes"] = count_planned_deletes(result.log_text())
    return {**base, "status": status, "exit_code": code, "error": error_msg,
            "suspect_reason": suspect_reason}
