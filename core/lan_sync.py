"""LAN sync - rclone mirror to the NAS share (plan P1).

Builder, pure exit decision, plan (dry-run), the sync itself and the arrival check.
The LAN copier that ran before it (replaced in P1 C10a) is gone.
"""

import re
from pathlib import Path

from loguru import logger

from core import cloud_sync, cloud_verify
from core.guards import require_source_files
from core.permissions import PROBE_PATTERN
from core.process import resolve_binary
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool, tail_lines
from models.config import LanConfig, model_default

# ═══════════════════════════════════════════════════════════════
# rclone LAN mirror (plan P1): builder and exit decision.
# ═══════════════════════════════════════════════════════════════

# Never synced to the NAS: our mount marker, our write-probe files, NTFS system folders.
LAN_EXCLUDES = (
    ".AAM_TARGET_MOUNTED",
    PROBE_PATTERN,
    "System Volume Information/**",
    "$RECYCLE.BIN/**",
)


def build_lan_rclone_command(
    config: LanConfig,
    source: str,
    dest: str,
    log_path: str,
    *,
    max_delete: int | None = None,
    dry_run: bool = False,
    combined_path: str | None = None,
    max_duration_s: int | None = None,
) -> list[str]:
    """argv of the LAN `rclone sync` (plan P1 2.1).

    Real sync: --max-delete (0 is emitted too) and, when the budget limits the
    run, --max-duration with a SOFT cutoff; the G1 backstop refuses an empty
    source. Dry-run (the plan measurement): the same argv plus --dry-run and
    --combined, without --max-delete/--max-duration/--cutoff-mode, so the
    measurement describes exactly what the real sync decides.
    """
    if not dry_run:
        require_source_files(source)  # G1 backstop
    cmd = [
        resolve_binary("rclone") or "rclone", "sync", source, dest,
        "--check-first", "--create-empty-src-dirs", "--skip-links", "--ignore-case",
        "--modify-window", f"{int(config.modify_window_seconds)}s",
        "--transfers", str(int(config.transfers)),
        "--checkers", str(int(config.checkers)),
        "--retries", str(int(config.retry_count)),
        "--retries-sleep", f"{int(config.retry_wait_seconds)}s",
        "--use-json-log", "--log-level", "INFO",
        "--log-file", str(log_path),
    ]
    for pattern in LAN_EXCLUDES:
        cmd.extend(["--exclude", pattern])
    if config.track_renames:
        cmd.append("--track-renames")
    if dry_run:
        cmd.append("--dry-run")
        if combined_path:
            cmd.extend(["--combined", str(combined_path)])
        return cmd
    if max_delete is not None:
        cmd.extend(["--max-delete", str(int(max_delete))])
    if max_duration_s:
        cmd.extend(["--max-duration", f"{int(max_duration_s)}s", "--cutoff-mode", "SOFT"])
    return cmd


# rclone exit codes that never happen on a LAN mirror we start (plan 2.2 row 3).
_LAN_UNEXPECTED_EXITS = frozenset({2, 3, 7, 8, 9})
_LAN_PARTIAL_EXITS = frozenset({1, 4, 6})


def decide_lan_rclone_result(
    exit_code,
    log_text: str,
    *,
    error_tail_lines: int = model_default(LanConfig, "error_tail_lines"),
) -> dict:
    """Pure decision of a finished LAN `rclone sync` (plan P1 2.2, rows in order).

    Returns {status, reason, stats, abnormal, retryable, max_delete_reached,
    time_limit_stopped, files_failed, failed_paths, deletes_held, error}.
    A locked file is LAN_PARTIAL (rclone deletes nothing after a read error);
    PARTIAL is never retried. Only exit 5 without a fatal error is retryable.
    """
    stats = cloud_sync.final_stats(log_text)
    out = {
        "status": "LAN_FAILED", "reason": None, "stats": stats, "abnormal": False,
        "retryable": False, "max_delete_reached": False, "time_limit_stopped": False,
        "files_failed": 0, "failed_paths": [], "deletes_held": False, "error": None,
    }

    def done(status: str, reason: str, **extra) -> dict:
        out.update(status=status, reason=reason, **extra)
        if status != "LAN_COMPLETE":
            out["error"] = reason
        return out

    if type(exit_code) is not int or exit_code < 0:
        return done("LAN_FAILED", f"rclone exit code unusable: {exit_code!r}", abnormal=True)
    meaning = cloud_sync.RCLONE_EXIT_MEANING.get(exit_code, "unknown exit code")
    if cloud_sync.max_delete_reached(log_text):
        return done("LAN_FAILED", f"rclone exit {exit_code}: --max-delete backstop reached "
                    "(more deletions than the guard allowed)", max_delete_reached=True)
    if exit_code in _LAN_UNEXPECTED_EXITS or exit_code not in (0, 1, 4, 5, 6, 10):
        note = ": unexpected on LAN" if exit_code == 9 else ""
        return done("LAN_FAILED", f"rclone exit {exit_code} ({meaning}){note}")
    if exit_code != 0 and stats is None:
        return done("LAN_FAILED", f"rclone exit {exit_code} ({meaning}) and no final stats: "
                    "the outcome is unknown", abnormal=True)

    has_error, error_tail = cloud_sync.scan_rclone_log_for_errors(log_text, max_lines=error_tail_lines)
    stats_errors = stats.get("errors") if isinstance(stats, dict) else None
    errors_n = stats_errors if type(stats_errors) is int else 0

    if exit_code == 0:
        if stats is None:
            return done("LAN_SUSPECT", "rclone exited 0 but wrote no final stats: rclone did not finish")
        if errors_n == 0 and not has_error:
            changed = any(stats.get(k) != 0 for k in ("transfers", "deletes", "renames"))
            return done("LAN_COMPLETE", "mirror complete" if changed else "no changes")
        return done("LAN_SUSPECT", "rclone exited 0 but reported errors: "
                    f"{errors_n} in its final stats; {error_tail or 'ERROR lines in the log'}")
    if exit_code == 10:
        return done("LAN_PARTIAL", "rclone stopped at its time limit (--max-duration)",
                    time_limit_stopped=True)
    if stats.get("fatalError") is True:
        return done("LAN_FAILED", f"rclone exit {exit_code} ({meaning}): fatal error"
                    + (f": {error_tail}" if error_tail else ""))
    if exit_code == 5:
        return done("LAN_FAILED", f"rclone exit 5 ({meaning})"
                    + (f": {error_tail}" if error_tail else ""), retryable=True)
    # exits 1, 4, 6: some files could not be copied
    paths = cloud_sync.failed_object_paths(log_text, error_tail_lines)
    held = cloud_sync.deletes_were_held(log_text)
    failed = max(errors_n, 1)
    reason = f"rclone exit {exit_code}: {failed} file(s) could not be copied"
    if paths:
        reason += f": {', '.join(paths)}"
    return done("LAN_PARTIAL", reason, files_failed=failed, failed_paths=paths, deletes_held=held)


# ═══════════════════════════════════════════════════════════════
# rclone LAN wrappers: sync, plan (dry-run), arrival check (plan P1 2.1-2.3)
# ═══════════════════════════════════════════════════════════════

def run_lan_mirror(
    config: LanConfig,
    source: str,
    dest: str,
    artefacts_dir: str,
    *,
    max_delete: int,
    max_duration_s: int | None = None,
    timeout: int | None = None,
) -> dict:
    """Run the real LAN `rclone sync` and return decide_lan_rclone_result's dict
    plus exit_code (None on spawn error/timeout, never a sentinel), timed_out,
    log_path and, when rclone is missing, fault_code AAM-SYS-04.

    The full JSON log is kept as `<...>_rclone-lan-sync.log` in the run folder.
    """
    artefacts = RunArtefacts.open(artefacts_dir)
    step = "rclone-lan-sync"
    timeout = config.subprocess_timeout_seconds if timeout is None else timeout
    log_path = artefacts.new_path(step, "log")
    cmd = build_lan_rclone_command(config, source, dest, str(log_path),
                                   max_delete=max_delete, max_duration_s=max_duration_s)
    logger.info(f"LAN rclone sync: {source} -> {dest} (full log: {log_path})")
    result = run_tool(step, cmd, timeout_s=timeout, artefacts=artefacts, log_path=log_path)

    base = {"exit_code": None, "timed_out": False, "log_path": str(log_path)}
    if result.spawn_error:
        missing = "FileNotFoundError" in result.spawn_error
        reason = "rclone not found" if missing else result.spawn_error
        out = decide_lan_rclone_result(None, "", error_tail_lines=config.error_tail_lines)
        out.update(base, reason=reason, error=reason)
        if missing:
            out["fault_code"] = "AAM-SYS-04"
        return out
    log_text = "\n".join(t for t in (result.log_text(), result.stderr_text()) if t)
    if result.timed_out:
        out = decide_lan_rclone_result(None, log_text, error_tail_lines=config.error_tail_lines)
        reason = (f"Timeout after {timeout}s - rclone sync is resumable; progress is "
                  "preserved and the next run continues from where this one left off")
        out.update(base, timed_out=True, reason=reason, error=reason)
        return out
    out = decide_lan_rclone_result(result.exit_code, log_text, error_tail_lines=config.error_tail_lines)
    out.update(base, exit_code=result.exit_code)
    return out


def plan_lan_mirror(
    config: LanConfig,
    source: str,
    dest: str,
    artefacts_dir: str,
    *,
    timeout: int | None = None,
) -> dict:
    """Measure what the real LAN sync would do (`--dry-run --combined`).

    Planned deletes/moves/copies/bytes come from the dry-run JSON log (a
    --track-renames move shows as '+' and '-' in combined, plan P1 C0b);
    source/dest file counts come from the combined file. ok is False on any
    sign the measurement is not trustworthy, so the guards fail closed.
    """
    artefacts = RunArtefacts.open(artefacts_dir)
    step = "rclone-lan-plan"
    timeout = config.rclone_measure_timeout_seconds if timeout is None else timeout
    log_path = artefacts.new_path(step, "log")
    combined_path = artefacts.new_path(f"{step}-combined", "txt")
    cmd = build_lan_rclone_command(config, source, dest, str(log_path),
                                   dry_run=True, combined_path=str(combined_path))
    logger.info(f"LAN rclone plan: {source} -> {dest} (full log: {log_path})")
    result = run_tool(step, cmd, timeout_s=timeout, artefacts=artefacts, log_path=log_path)

    out = {"ok": False, "error": None, "exit_code": result.exit_code,
           "source_files": None, "dest_files": None, "planned_deletes": None,
           "moves": 0, "planned_copies": None, "planned_bytes": None,
           "log_path": str(log_path), "combined_path": str(combined_path)}
    if result.spawn_error:
        out["error"] = result.spawn_error
        return out
    if result.timed_out:
        out["error"] = f"Timeout after {timeout}s"
        return out
    json_log = result.log_text()
    log_text = "\n".join(t for t in (json_log, result.stderr_text()) if t)
    has_error, error_tail = cloud_sync.scan_rclone_log_for_errors(
        log_text, max_lines=config.error_tail_lines)
    stats = cloud_sync.final_stats(json_log)
    stats_errors = stats.get("errors") if isinstance(stats, dict) else None
    if result.exit_code != 0 or has_error or (type(stats_errors) is int and stats_errors > 0):
        out["error"] = (f"rclone exit {result.exit_code}: "
                        f"{error_tail or tail_lines(log_text, config.error_tail_lines) or 'no log output'}")
        return out
    try:
        counts = cloud_sync.combined_counts(combined_path)
    except OSError as exc:
        out["error"] = f"rclone wrote no combined file: {exc}"
        return out
    if counts["!"] or counts["?"]:
        out["error"] = (f"rclone dry-run reported {counts['!']} unreadable and "
                        f"{counts['?']} unknown entries")
        return out
    out.update(
        ok=True,
        source_files=counts["="] + counts["*"] + counts["+"],
        dest_files=counts["="] + counts["*"] + counts["-"],
        planned_deletes=cloud_sync.count_planned_deletes(json_log),
        moves=cloud_sync.count_planned_moves(json_log),
        planned_copies=cloud_sync.planned_copy_count(json_log),
        planned_bytes=cloud_sync.planned_copy_bytes(json_log),
    )
    return out


_MATCHING_RE = re.compile(r":\s*(\d+) matching files\s*$", re.MULTILINE)
_ERRORS_WHILE_CHECKING_RE = re.compile(r"(\d+) errors? while checking")


def decide_lan_arrival(exit_code, stderr: str, combined_text: str, changed_since) -> dict:
    """Pure verdict of the LAN arrival check (plan P1 2.3).

    Verified only when the check finished (summary line present, exit 0 or 1),
    has no '!'/'?' entries, its difference count equals the '+' (missing) plus
    '*' (differs) entries of the combined file, and every such file was changed
    on the source during the backup (`changed_since(path)`): those are files
    the source moved under the sync, not lost data.
    """
    decision = cloud_verify.decide_cloud_verify_result(exit_code, stderr, "")
    missing_paths, differ_paths = [], []
    bad = 0
    for line in (combined_text or "").splitlines():
        if not line.strip():
            continue
        kind, path = line[0], line[2:].strip()
        if kind == "+":
            missing_paths.append(path)
        elif kind == "*":
            differ_paths.append(path)
        elif kind in "!?":
            bad += 1
    changed = [p for p in missing_paths + differ_paths if changed_since(p)]
    unverified = [p for p in missing_paths + differ_paths if p not in changed]
    matches = _MATCHING_RE.findall(stderr or "")
    errors = _ERRORS_WHILE_CHECKING_RE.findall(stderr or "")
    out = {
        "verified": False, "reason": None,
        "matching": int(matches[-1]) if matches else None,
        "missing": len(missing_paths), "differ": len(differ_paths),
        "changed_during_backup": len(changed), "unverified_paths": unverified,
    }
    differences = decision["differences"]
    if not decision["completion"]:
        out["reason"] = decision["reason"]
    elif type(exit_code) is not int or exit_code not in (0, 1):
        out["reason"] = f"rclone check exit {exit_code!r} is not 0 or 1"
    elif bad:
        out["reason"] = f"rclone check listed {bad} unreadable or unknown entries"
    elif differences != len(missing_paths) + len(differ_paths):
        out["reason"] = (f"rclone check summary says {differences} difference(s) but its combined "
                         f"file lists {len(missing_paths) + len(differ_paths)}")
    elif differences == 0 and not decision["verified"]:
        out["reason"] = decision["reason"]
    elif errors and int(errors[-1]) > differences:
        out["reason"] = f"rclone check reported {errors[-1]} error(s) beyond its {differences} difference(s)"
    elif unverified:
        out["reason"] = (f"{len(unverified)} file(s) differ on the NAS and were not changed during "
                         "the backup")
    else:
        out["verified"] = True
    return out


def check_lan_arrival(
    config: LanConfig,
    source: str,
    dest: str,
    artefacts_dir: str,
    *,
    changed_after_epoch: float,
    timeout: int | None = None,
) -> dict:
    """Run `rclone check --one-way --size-only` source -> NAS and decide with
    decide_lan_arrival. A file counts as changed during the backup when
    max(st_mtime, st_ctime) of the source file >= changed_after_epoch -
    lan.modify_window_seconds (OSError counts as not changed)."""
    artefacts = RunArtefacts.open(artefacts_dir)
    step = "rclone-lan-check"
    timeout = config.verify_timeout_seconds if timeout is None else timeout
    combined_path = artefacts.new_path(f"{step}-combined", "txt")
    cmd = [
        resolve_binary("rclone") or "rclone", "check", source, dest,
        "--one-way", "--size-only", "--ignore-case", "--skip-links",
        "--checkers", str(int(config.checkers)),
        "--combined", str(combined_path),
    ]
    for pattern in LAN_EXCLUDES:
        cmd.extend(["--exclude", pattern])
    logger.info(f"LAN arrival check: {source} -> {dest}")
    result = run_tool(step, cmd, timeout_s=timeout, artefacts=artefacts)

    def failed(reason: str) -> dict:
        return {"verified": False, "reason": reason, "matching": None, "missing": 0,
                "differ": 0, "changed_during_backup": 0, "unverified_paths": []}

    if result.spawn_error:
        return failed(f"arrival check could not start: {result.spawn_error}")
    if result.timed_out:
        return failed(f"arrival check timed out after {timeout}s")
    try:
        combined_text = Path(combined_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        combined_text = ""
    threshold = changed_after_epoch - config.modify_window_seconds

    def changed_since(rel_path: str) -> bool:
        try:
            st = (Path(source) / rel_path).stat()
        except OSError:
            return False
        return max(st.st_mtime, st.st_ctime) >= threshold

    return decide_lan_arrival(result.exit_code, result.stderr_text(), combined_text, changed_since)
