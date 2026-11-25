"""Cloud sync — rclone sync wrapper with temp config and exit classification.

Reference: AAM_BACKUP_V2/core/rclone.py — proven classification and temp config pattern.
"""

import json
import re

from loguru import logger

from core.guards import require_source_files
from core.process import resolve_binary
from core.rclone_config import temp_rclone_config
from core.rclone_errors import find_transport_failure
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool, tail_lines
from models.config import CloudConfig, model_default

# P1-EXIT9: severity floor that disqualifies an exit-9 from "no changes".
_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
# R1 hardening: some fatal errors bypass the JSON logger (log.Fatalf paths,
# rclone issue #6038). These plaintext markers are treated as error signals.
_FATAL_PLAINTEXT_MARKERS = ("failed to", "notices: failed", "notice: failed")

# rclone v1.74.3 lib/exitcode (XR F1; 1 and 2 were documented swapped before).
RCLONE_EXIT_MEANING = {
    0: "success",
    1: "error not otherwise categorised",
    2: "syntax or usage error",
    3: "directory not found",
    4: "file not found",
    5: "temporary error (a retry may fix it)",
    6: "less serious error (no retry)",
    7: "fatal error (a retry will not fix it)",
    8: "--max-transfer limit reached",
    9: "no files transferred (--error-on-no-transfer)",
    10: "--max-duration limit reached",
}
# XR-6: exit codes a whole new sync can change, when rclone's own final
# stats do not mark the error fatal (F5). Everything else is final.
_RETRYABLE_EXITS = frozenset({1, 4, 5})
# rclone's own --retries loop (cmd/cmd.go Run) logs these at ERROR level,
# the success notice included (F4). Errors up to the last "failed" marker
# belong to attempts rclone already retried.
_ATTEMPT_FAILED = re.compile(r"^Attempt \d+/\d+ failed\b")
_ATTEMPT_SUCCEEDED = re.compile(r"^Attempt \d+/\d+ succeeded$")
# fs/accounting/stats.go errMaxDelete (XR-1): the --max-delete backstop tripped.
_MAX_DELETE_MSG = "--max-delete threshold reached"
# Stats fields that change the destination (XR-3): any non-zero is a change.
_CHANGE_STATS = ("transfers", "deletes", "deletedDirs", "renames")


def _json_entries(log_text: str):
    """(raw line, parsed dict or None) for every non-empty log line."""
    for raw_line in (log_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        yield line, entry if isinstance(entry, dict) else None


def count_failed_attempts(log_text: str) -> int:
    """How many of rclone's own --retries attempts failed in this process."""
    return sum(1 for _, e in _json_entries(log_text)
               if e is not None and _ATTEMPT_FAILED.match(str(e.get("msg", ""))))


def _final_attempt_start(entries: list) -> int:
    """Index after which the log lines belong to the FINAL attempt (XR-4).

    The final attempt starts after the PREVIOUS "Attempt k/N failed" marker
    (or after the last one when a later "succeeded" notice follows it);
    -1 when rclone never retried.
    """
    last_failed = max((i for i, (_, e) in enumerate(entries)
                       if e is not None and _ATTEMPT_FAILED.match(str(e.get("msg", "")))),
                      default=None)
    earlier_failed = None
    if last_failed is not None:
        succeeded_after = any(
            e is not None and _ATTEMPT_SUCCEEDED.match(str(e.get("msg", "")))
            for _, e in entries[last_failed + 1:])
        if succeeded_after:
            earlier_failed = last_failed
        else:
            earlier_failed = max((i for i, (_, e) in enumerate(entries[:last_failed])
                                  if e is not None
                                  and _ATTEMPT_FAILED.match(str(e.get("msg", "")))),
                                 default=None)
    return -1 if earlier_failed is None else earlier_failed


def scan_rclone_log_for_errors(log_text: str, max_lines: int = model_default(CloudConfig, "error_tail_lines")) -> tuple[bool, str]:
    """Scan rclone's --use-json-log stream for ERROR/CRITICAL signals of the
    FINAL attempt (XR-4).

    Returns (has_error_signal, error_tail). A line counts as an error signal if:
      * it parses as JSON, its "level" is ERROR or CRITICAL, it comes after
        rclone's last "Attempt k/N failed" line (earlier errors belong to
        attempts rclone itself retried) and it is not rclone's own
        "Attempt k/N succeeded" notice, OR
      * it does not parse as JSON and carries a known fatal marker
        ("Failed to ...") - the documented escape hatch of the JSON logger.
        These are kept wherever they appear.

    When every attempt failed, the last "failed" line itself is the final
    attempt's verdict and is kept with that attempt's errors.
    """
    entries = list(_json_entries(log_text))
    start = _final_attempt_start(entries)
    bad_lines: list[str] = []
    for i, (line, entry) in enumerate(entries):
        if entry is None:
            lowered = line.lower()
            if any(marker in lowered for marker in _FATAL_PLAINTEXT_MARKERS):
                bad_lines.append(line)
            continue
        if i <= start:
            continue
        level = str(entry.get("level", "")).upper()
        msg = str(entry.get("msg", ""))
        if _LOG_LEVELS.get(level, 0) >= _LOG_LEVELS["ERROR"] and not _ATTEMPT_SUCCEEDED.match(msg):
            bad_lines.append(line)
    return bool(bad_lines), "\n".join(bad_lines[-max_lines:] if max_lines > 0 else [])


def max_delete_reached(log_text: str) -> bool:
    """True when rclone stopped deleting at the --max-delete backstop (XR-1)."""
    return any(e is not None and _MAX_DELETE_MSG in str(e.get("msg", ""))
               for _, e in _json_entries(log_text))


def rclone_retryable(code: int, stats: dict | None, *, max_delete_reached: bool) -> bool:
    """XR-6: can a whole new `rclone sync` change this failed outcome?

    Only exits 1/4/5 that rclone itself did not mark fatal. Never after the
    --max-delete backstop (a human decides, XR-1) and never when the final
    stats are missing - the deletions of the attempt are then unknown, so
    a repeat could not be bounded (fail closed).
    """
    if max_delete_reached or not isinstance(stats, dict):
        return False
    return type(code) is int and code in _RETRYABLE_EXITS and stats.get("fatalError") is not True


def stats_prove_no_changes(stats: dict | None) -> bool:
    """XR-3: exit 9 means no uploads; "no changes" needs every change count 0."""
    return isinstance(stats, dict) and all(stats.get(k) == 0 for k in _CHANGE_STATS)


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


def count_planned_moves(log_text: str) -> int:
    """Rename moves rclone `--track-renames` planned: `"skipped":"move to <name>"`
    lines (verified on rclone v1.74.3, 2026-11-25). Each is a file a delete-and-copy
    mirror would delete (old name) and copy again (new name)."""
    planned = 0
    for line in (log_text or "").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and str(entry.get("skipped", "")).startswith("move"):
            planned += 1
    return planned


def combined_counts(path) -> dict:
    """Count the lines of an rclone `--combined` file by their first character.

    Keys "=", "*", "+", "-", "!" and "?" (any other first character). Streams
    the file (memory stays flat for millions of files). Combined lists files
    only, never folders (rclone v1.74.3, plan P1 C0b).
    """
    counts = {key: 0 for key in ("=", "*", "+", "-", "!", "?")}
    with open(path, encoding="utf-8", errors="replace", newline=None) as f:
        for line in f:
            if not line.strip():
                continue
            counts[line[0] if line[0] in counts else "?"] += 1
    return counts


def _copy_entries(log_text: str) -> list[dict]:
    return [e for _, e in _json_entries(log_text)
            if e is not None and e.get("skipped") == "copy"]


def planned_copy_count(log_text: str) -> int:
    """Files a real sync would copy, from a `--dry-run` JSON log."""
    return len(_copy_entries(log_text))


def planned_copy_bytes(log_text: str) -> int | None:
    """Bytes a real sync would copy: the sum of the `size` of every planned copy.

    None when any copy entry has no int size, so the free-space check fails
    closed instead of trusting a partial sum.
    """
    total = 0
    for entry in _copy_entries(log_text):
        size = entry.get("size")
        if type(size) is not int:
            return None
        total += size
    return total


def failed_object_paths(log_text: str, limit: int) -> list[str]:
    """Objects of the FINAL attempt's "Failed to copy" errors (unique, in log
    order, at most `limit`)."""
    entries = list(_json_entries(log_text))
    start = _final_attempt_start(entries)
    paths: list[str] = []
    for i, (_, entry) in enumerate(entries):
        if i <= start or entry is None:
            continue
        if str(entry.get("level", "")).upper() not in ("ERROR", "CRITICAL"):
            continue
        obj = entry.get("object")
        if (str(entry.get("msg", "")).startswith("Failed to copy")
                and isinstance(obj, str) and obj and obj not in paths):
            paths.append(obj)
            if len(paths) >= limit:
                break
    return paths


_DELETES_HELD_MSG = "not deleting files as there were IO errors"


def deletes_were_held(log_text: str) -> bool:
    """True when rclone skipped all deletions because of read/IO errors."""
    return any(e is not None and _DELETES_HELD_MSG in str(e.get("msg", ""))
               for _, e in _json_entries(log_text))


def rclone_deletes_of(result: dict, max_delete: int) -> int | None:
    """Files a finished rclone sync deleted: its own stats.deletes; after a
    --max-delete stop without that number, the cap; otherwise None (unknown)."""
    stats = result.get("stats")
    deletes = stats.get("deletes") if isinstance(stats, dict) else None
    if type(deletes) is int:
        return deletes
    return max_delete if result.get("max_delete_reached") else None


def measure_planned_deletes(
    source: str,
    dest: str,
    *,
    config_path: str | None = None,
    fast_list: bool = False,
    ignore_case: bool = False,
    track_renames: bool = model_default(CloudConfig, "track_renames"),
    modify_window_seconds: int = model_default(CloudConfig, "modify_window_seconds"),
    retries: int = model_default(CloudConfig, "retry_count"),
    retries_sleep_seconds: int = model_default(CloudConfig, "sync_retries_sleep_seconds"),
    excludes: tuple[str, ...] = (),
    timeout: int = model_default(CloudConfig, "dry_run_timeout_seconds"),
    artefacts: RunArtefacts | None = None,
    step: str = "rclone-dry-run",
    error_tail_lines: int = model_default(CloudConfig, "error_tail_lines"),
) -> dict:
    """G3 input for BOTH legs: files `rclone sync source dest` would delete.

    One shared measurement (owner decision, AAM-DEL-01 on a folder rename):
    `rclone sync --dry-run --use-json-log --track-renames`. A file rclone
    matches by size/hash/modtime under another name is a MOVE and is not
    counted as a delete - its data still exists in the source. Cloud passes
    its temp rclone config and fast_list=True; the LAN leg passes none (local
    backend, UNC destination) plus the names the LAN mirror keeps (LAN_EXCLUDES).

    Differences from the real upload command (build_rclone_sync_command): no
    --gcs-no-check-bucket, --check-first or --gcs-storage-class. Those only
    change how objects are written (bucket check, ordering, storage class);
    they do not change which files rclone decides to delete or move, so the
    dry-run's delete count equals the real sync's.
    ignore_case: LAN leg - rclone filters are case-sensitive, the NTFS share is not.

    Returns {"planned_deletes": int | None, "moves": int, "exit_code", "error",
    "log_path"};
    planned_deletes is None when the dry-run did not complete cleanly (the
    guard then fails the run closed).
    """
    artefacts = artefacts or RunArtefacts.ephemeral("measure-deletes")
    cmd = [resolve_binary("rclone") or "rclone", "sync", source, dest,
           "--dry-run", "--use-json-log", "--log-level", "INFO",
           "--modify-window", f"{int(modify_window_seconds)}s",
           "--retries", str(int(retries)),
           "--retries-sleep", f"{int(retries_sleep_seconds)}s"]
    if config_path:
        cmd.extend(["--config", config_path])
    if fast_list:
        cmd.append("--fast-list")
    if track_renames:
        cmd.append("--track-renames")
    if ignore_case:
        cmd.append("--ignore-case")
    for pattern in excludes:
        cmd.extend(["--exclude", pattern])
    log_path = artefacts.new_path(step, "log")
    cmd.extend(["--log-file", str(log_path)])
    logger.info(f"Delete measurement: {source} -> {dest} (full log: {log_path})")
    result = run_tool(step, cmd, timeout_s=timeout, artefacts=artefacts, log_path=log_path)

    out = {"planned_deletes": None, "moves": 0, "exit_code": result.exit_code,
           "error": None, "log_path": str(log_path)}
    if result.spawn_error:
        out["error"] = result.spawn_error
        return out
    if result.timed_out:
        out["error"] = f"Timeout after {timeout}s"
        return out
    log_text = "\n".join(t for t in (result.log_text(), result.stderr_text()) if t)
    has_error, error_tail = scan_rclone_log_for_errors(log_text, max_lines=error_tail_lines)
    stats = final_stats(result.log_text())
    stats_errors = stats.get("errors") if isinstance(stats, dict) else None
    if result.exit_code != 0 or has_error or (type(stats_errors) is int and stats_errors > 0):
        out["error"] = (f"rclone exit {result.exit_code}: "
                        f"{error_tail or tail_lines(log_text, error_tail_lines) or 'no log output'}")
        return out
    out["planned_deletes"] = count_planned_deletes(result.log_text())
    out["moves"] = count_planned_moves(result.log_text())
    return out


def final_stats(log_text: str) -> dict | None:
    """The run's own totals: the LAST JSON log line carrying "stats".

    rclone prints it at exit with --use-json-log (verified v1.74.3):
    transfers (files uploaded), bytes, deletes, errors, checks. None when
    the log has no stats line (killed / never started) - counts unknown.
    """
    for line in reversed((log_text or "").splitlines()):
        if '"stats"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        stats = entry.get("stats") if isinstance(entry, dict) else None
        if isinstance(stats, dict):
            return stats
    return None


def classify_rclone_exit(code: int) -> str:
    """Classify rclone exit code (meanings: RCLONE_EXIT_MEANING).

    0  → CLOUD_COMPLETE  (all files synced)
    1  → CLOUD_FAILED     (error not otherwise categorised)
    2  → CLOUD_FAILED     (syntax/usage)
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


MAX_DURATION_MARGIN_S = 300   # auto --max-duration ends this long before the hard kill


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
    auto = int(timeout) - MAX_DURATION_MARGIN_S
    return auto if auto > 0 else None


def build_rclone_sync_command(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
    storage_class: str,
    bwlimit: str = model_default(CloudConfig, "bandwidth_limit"),
    retries: int = model_default(CloudConfig, "retry_count"),
    transfers: int = model_default(CloudConfig, "transfers"),
    checkers: int = model_default(CloudConfig, "checkers"),
    buffer_size: str = model_default(CloudConfig, "buffer_size"),
    modify_window_seconds: int = model_default(CloudConfig, "modify_window_seconds"),
    retries_sleep_seconds: int = model_default(CloudConfig, "sync_retries_sleep_seconds"),
    stats_interval_seconds: int = model_default(CloudConfig, "stats_interval_seconds"),
    max_duration_seconds: int | None = None,
    max_delete: int | None = None,
    track_renames: bool = model_default(CloudConfig, "track_renames"),
) -> list[str]:
    """Build rclone sync command with GCS-optimized flags.

    All tunable values are passed as parameters — caller draws them from config.
    max_delete: rclone's own `--max-delete` backstop (M2 2.2): the sync stops
    with a fatal error (exit 7) instead of deleting more files than G3 allows.
    """
    require_source_files(source)  # G1 backstop (M2 2.2)
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
        "--modify-window", f"{int(modify_window_seconds)}s",  # NTFS mtime granularity is 2 s.
                                     # Prevents false-positive re-uploads when a file
                                     # is saved twice within the same NTFS tick.
        "--bwlimit", bwlimit,
        "--transfers", str(transfers),
        "--checkers", str(checkers),
        "--retries", str(retries),
        "--retries-sleep", f"{int(retries_sleep_seconds)}s",
        "--check-first",            # Finish all stat/hash checks before any upload starts.
                                     # Separates random-seek metadata phase from sequential
                                     # read-for-upload phase — critical for HDD head efficiency.
        "--buffer-size", buffer_size,  # Upload read buffer per transfer slot.
                                       # 2 transfers × 64M = 128M total. Matches GCS multipart
                                       # chunk sizing without wasting RAM. (256M was too large;
                                       # --use-mmap removed — documented as unstable on Windows.)
        "--use-json-log",
        "--log-level", "INFO",
        "--stats", f"{int(stats_interval_seconds)}s",
    ]

    # C-A: graceful self-termination inside the window. SOFT cutoff lets the
    # in-flight transfer finish and preserves .partial state; retries-sleep
    # is set explicitly above (cloud.sync_retries_sleep_seconds) so a retry
    # cannot silently reset the deadline.
    if max_duration_seconds:
        cmd.extend([
            "--max-duration", f"{int(max_duration_seconds)}s",
            "--cutoff-mode", "SOFT",
        ])
    if max_delete is not None:
        cmd.extend(["--max-delete", str(int(max_delete))])
    if track_renames:
        # A renamed file is moved server-side instead of deleted + uploaded; the
        # G3 measurement (measure_planned_deletes) uses the same flag, so its
        # prediction matches what this sync really does.
        cmd.append("--track-renames")

    return cmd


def run_cloud_sync(
    source: str,
    bucket: str,
    fy_prefix: str,
    gcs_key_path: str,
    project_number: str,
    storage_class: str,
    location: str = model_default(CloudConfig, "location"),
    bwlimit: str = model_default(CloudConfig, "bandwidth_limit"),
    retries: int = model_default(CloudConfig, "retry_count"),
    transfers: int = model_default(CloudConfig, "transfers"),
    checkers: int = model_default(CloudConfig, "checkers"),
    buffer_size: str = model_default(CloudConfig, "buffer_size"),
    modify_window_seconds: int = model_default(CloudConfig, "modify_window_seconds"),
    retries_sleep_seconds: int = model_default(CloudConfig, "sync_retries_sleep_seconds"),
    stats_interval_seconds: int = model_default(CloudConfig, "stats_interval_seconds"),
    timeout: int = model_default(CloudConfig, "subprocess_timeout_seconds"),
    max_duration_seconds: int | None = None,
    artefacts: RunArtefacts | None = None,
    error_tail_lines: int = model_default(CloudConfig, "error_tail_lines"),
    max_delete: int | None = None,
    track_renames: bool = model_default(CloudConfig, "track_renames"),
) -> dict:
    """Execute rclone sync to mirror source -> GCS.

    max_delete: `--max-delete` backstop for the real sync. The guard G3
    measurement is `measure_planned_deletes` (shared with the LAN leg).

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
            modify_window_seconds=modify_window_seconds,
            retries_sleep_seconds=retries_sleep_seconds,
            stats_interval_seconds=stats_interval_seconds,
            max_duration_seconds=effective_max_duration,
            max_delete=max_delete, track_renames=track_renames,
        )
        step = "rclone-sync"
        log_path = artefacts.new_path(step, "log")
        cmd.extend(["--log-file", str(log_path)])

        logger.info(f"Cloud sync: {source} → "
                    f"{bucket}/{fy_prefix} (full log: {log_path})")
        result = run_tool(step, cmd, timeout_s=timeout,
                          artefacts=artefacts, log_path=log_path)

    base = {"suspect_reason": None, "log_path": str(log_path), "stats": None,
            "rclone_failed_attempts": 0, "max_delete_reached": False, "retryable": False}
    if result.spawn_error:
        missing = "FileNotFoundError" in result.spawn_error
        return {**base, "status": "CLOUD_FAILED", "exit_code": -1,
                "error": "rclone not found" if missing else result.spawn_error,
                **({"fault_code": "AAM-SYS-04"} if missing else {})}
    if result.timed_out:
        # G7: a timeout is an interruption, not a data event — rclone sync
        # is resumable. Not retried (XR-6): rclone overran its own
        # --max-duration, and its deletions are unknown.
        return {**base, "status": "CLOUD_FAILED", "exit_code": -1, "timed_out": True,
                "stats": final_stats(result.log_text()), "error": (
            f"Timeout after {timeout}s — rclone sync is resumable; progress is "
            "preserved and the next run continues from where this one left off"
        )}

    code = result.exit_code
    status = classify_rclone_exit(code)
    # rclone writes its log to --log-file; anything printed before the log
    # file opened (fatal config/flag errors) lands on stderr. Scan both.
    log_text = "\n".join(t for t in (result.log_text(), result.stderr_text()) if t)
    has_error, error_tail = scan_rclone_log_for_errors(log_text, max_lines=error_tail_lines)
    stats = final_stats(result.log_text())
    base["stats"] = stats
    base["rclone_failed_attempts"] = count_failed_attempts(log_text)
    base["max_delete_reached"] = max_delete_reached(log_text)
    if base["rclone_failed_attempts"]:
        logger.warning(f"rclone retried internally: {base['rclone_failed_attempts']} "
                       f"attempt(s) failed (full log: {log_path})")
    # XR-4: rclone resets its error count between its own attempts, so a
    # non-zero final count is an error of the attempt whose result counts.
    stats_errors = stats.get("errors") if isinstance(stats, dict) else None
    if type(stats_errors) is int and stats_errors > 0 and code in (0, 9):
        has_error = True
        error_tail = "\n".join(t for t in (
            error_tail, f"rclone final stats report {stats_errors} error(s)") if t)

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
        elif not stats_prove_no_changes(stats):
            # XR-3: exit 9 = nothing uploaded; deletions do not change it (F3).
            status = "CLOUD_COMPLETE"
            logger.info(f"Cloud sync: nothing uploaded, but rclone's "
                        "stats do not prove an unchanged destination "
                        f"({ {k: (stats or {}).get(k) for k in _CHANGE_STATS} }) - "
                        "recorded as complete, not 'no changes'")
        else:
            logger.info("Cloud sync: no changes to transfer")
    elif code != 0:
        detail = error_tail or tail_lines(log_text, error_tail_lines)
        error_msg = (
            f"rclone exit {code}: {detail}" if detail
            else f"rclone exit {code} (no log output)"
        )
        logger.error(f"rclone error (exit {code}); full log: {log_path}\n{detail}")

    if status == "CLOUD_FAILED":
        # fault code evidence (audit 2026-10-18): TLS interception / DNS / auth
        cause = find_transport_failure(log_text)
        base["transport_category"] = cause.category if cause else None
        base["retryable"] = rclone_retryable(code, stats,
                                             max_delete_reached=base["max_delete_reached"])
    logger.info(f"Cloud sync exit {code} "
                f"({RCLONE_EXIT_MEANING.get(code, 'unknown exit code')}) -> {status}")
    return {**base, "status": status, "exit_code": code, "error": error_msg,
            "suspect_reason": suspect_reason}
