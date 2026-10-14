"""LAN sync — robocopy /MIR wrapper with exit code classification.

Reference: AAM_BACKUP_V2/core/robocopy.py — proven bitmask logic and flag set.
Exit code reference: https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/robocopy
"""

import re
import tempfile
from pathlib import Path

from loguru import logger

from core.process import resolve_binary
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool
from models.config import LanConfig

# ═══════════════════════════════════════════════════════════════
# Flag validation — /NC is FORBIDDEN
# Source: ConvertFrom-RobocopLog §Notes
# /NC suppresses file class labels (e.g. "New File", "Changed") which
# the downstream log parser relies on to categorise per-file outcomes.
# ═══════════════════════════════════════════════════════════════

def _validate_required_flags(flags: list[str]) -> None:
    for f in flags:
        if f.upper() in ("/NC", "-NC"):
            raise ValueError("/NC flag suppresses file class labels - parser has nothing to match")


_FAILED_LINE = re.compile(r"^\s*\*\*\s*FAILED:", re.MULTILINE)

# Integrity-verification contract: a robocopy run that terminated normally
# ALWAYS ends its /LOG with the job-summary table ("Files :" row + "Ended :"
# line). An externally killed run (taskkill, WMI Terminate, NtTerminateProcess,
# supervisor kill) is truncated mid-transfer and has no summary — even though
# the inherited exit code can be 0-3 (T04/A1). Summary presence is therefore
# the normal-vs-abnormal termination signal; the exit code is only interpreted
# when the log proves the process lived to finish.
_ENDED_LINE = re.compile(r"^\s*Ended\s*:", re.MULTILINE)

# Positional columns of the job-summary "Files:" row (fixed order, locale
# independent): Total Copied Skipped Mismatch FAILED Extras.
_SUMMARY_COLUMN_NAMES = ("total", "copied", "skipped", "mismatch", "failed", "extras")

# P1-COUNT (A-prime): the job-summary "Files:" row. Column ORDER is fixed by
# robocopy regardless of locale (Total Copied Skipped Mismatch FAILED Extras),
# so we parse positionally instead of matching localized header words.
_SUMMARY_FILES_ROW = re.compile(r"^\s*Files\s*:", re.MULTILINE)


def _summary_files_row_values(log_text: str) -> list[int] | None:
    """Return the numeric columns of the job-summary Files row, or None.

    Fails closed on anything non-numeric (localized separators are stripped;
    letters or missing columns abort parsing) so callers can fall back to the
    exit-bitmask floor instead of reporting a false 0.
    """
    match = _SUMMARY_FILES_ROW.search(log_text or "")
    if not match:
        return None
    line_end = log_text.find("\n", match.start())
    row = log_text[match.start(): line_end if line_end != -1 else len(log_text)]
    values: list[int] = []
    for token in row.split(":", 1)[1].split():
        cleaned = token.replace(",", "").replace(".", "")
        if not cleaned.isdigit():
            return None
        values.append(int(cleaned))
    return values or None


def failed_file_count(log_text: str, exit_code: int) -> int:
    """Authoritative failed-file count for a robocopy run.

    A-prime contract (IMPLEMENTATION_FIX_PLAN.md v1.1):
      1. Parse the summary "Files:" row positionally; FAILED is column index 4.
      2. Floor by the exit bitmask: bit 3 set means at least one file could
         not be copied - if the summary is absent/unparseable, report >= 1
         rather than the misleading 0 (/NJS blindness, LAN-06/LAN-15).
      3. Contradictory signals (summary says 0, bit3 set) resolve to the loud
         side: 1.
    """
    bit3_floor = 1 if (exit_code & 8) else 0
    values = _summary_files_row_values(log_text)
    parsed_failed = values[4] if values and len(values) > 4 else None
    if parsed_failed is None:
        return bit3_floor
    return max(parsed_failed, bit3_floor)


def count_failed_lines(log_text: str) -> int:
    """Count robocopy per-file failure lines ("** FAILED: <path>") in a log tail.

    Robocopy prints one such line per file that could not be copied. The count
    is taken over the captured tail only; if a run fails more files than fit
    in the tail window the true number is higher (the tail is the contract:
    bounded payload, actionable diagnostics).
    """
    return len(_FAILED_LINE.findall(log_text))


def _read_log_tail(log_path: Path, max_bytes: int, encoding: str = "utf-8") -> str:
    """Read the tail of a robocopy log file, bounded to max_bytes.

    Robocopy writes summary and error details at the end of the log, so the
    tail always holds the most actionable diagnostics. F15: seek from the
    end instead of loading a multi-hundred-MB log into RAM.

    `encoding="utf-16"` reads robocopy /UNILOG output (UTF-16LE with BOM,
    verified on the target Windows build): the seek offset is aligned to a
    2-byte code unit and the tail is decoded as UTF-16LE.

    Returns the text tail, or a fallback message if the file is unreadable.
    """
    utf16 = encoding.lower().replace("_", "-") in ("utf-16", "utf-16le", "utf-16-le")
    try:
        size = log_path.stat().st_size
        if size <= max_bytes:
            text = log_path.read_bytes().decode(encoding, errors="replace").lstrip("﻿")
            return text.replace("\r\n", "\n")
        start = size - max_bytes
        if utf16 and start % 2:
            start += 1
        with open(log_path, "rb") as f:
            f.seek(start)
            data = f.read()
        text = data.decode("utf-16-le" if utf16 else encoding, errors="replace")
        # The cut point can land mid-sequence; drop the replacement char(s).
        # Newlines normalised to "\n" on both paths (robocopy writes CRLF).
        return text.lstrip("�﻿").replace("\r\n", "\n")
    except OSError as exc:
        return f"robocopy log unreadable: {exc}"


def log_shows_completed_summary(log_text: str) -> bool:
    """True only if the log ends with a completed robocopy job summary.

    Requires BOTH the positional "Files :" row and the "Ended :" line.
    A truncated (externally killed) log has neither, regardless of the
    exit code the terminated process happened to inherit.
    """
    text = log_text or ""
    return bool(_SUMMARY_FILES_ROW.search(text) and _ENDED_LINE.search(text))


def summarize_log_counts(log_text: str) -> dict | None:
    """Parse the job-summary "Files :" row into a counts dict, or None.

    Returns {"total","copied","skipped","mismatch","failed","extras"} or
    None when the summary is absent/unparseable (killed or corrupt log).
    """
    values = _summary_files_row_values(log_text)
    if not values or len(values) < len(_SUMMARY_COLUMN_NAMES):
        return None
    return dict(zip(_SUMMARY_COLUMN_NAMES, values[:len(_SUMMARY_COLUMN_NAMES)]))


def decide_lan_result(exit_code: int, log_text: str) -> dict:
    """Transfer-result contract: exit code + log evidence -> final result.

    COMPLETE means "transfer terminated normally and its own evidence says
    the scheduled work finished" — NOT cryptographic verification (that is
    the weekly audit's job). Returns {"status","termination","log_complete",
    "counts","reason"} where status is LAN_COMPLETE | LAN_PARTIAL |
    LAN_SUSPECT | LAN_FAILED and termination is "normal" | "abnormal".

    Rules:
      * negative / unexpected codes, bit 4 (16+)  -> LAN_FAILED (abnormal).
      * no completed summary in the log          -> LAN_SUSPECT (abnormal):
        the process did not live to finish; its exit code (even 0-3)
        carries no information (T04/A1).
      * bit 3 (copy errors) with a complete log  -> LAN_PARTIAL.
      * bit 2 (mismatch/extras) with complete log-> LAN_PARTIAL.
      * 0-3 with a complete log                  -> LAN_COMPLETE, unless the
        summary contradicts the code (failed/mismatch > 0, FAILED markers
        present) -> LAN_SUSPECT (fail closed on contradictory evidence).
    """
    text = log_text or ""
    log_complete = log_shows_completed_summary(text)
    counts = summarize_log_counts(text)
    failed_markers = count_failed_lines(text)

    # type() is not isinstance(): bool is an int subclass, and True must never
    # be read as robocopy exit code 1 (W13.exit_code_bool). Same guard as
    # core/cloud_verify.decide_cloud_verify_result.
    if exit_code is None or type(exit_code) is not int or exit_code < 0:
        return {
            "status": "LAN_FAILED", "termination": "abnormal",
            "log_complete": log_complete, "counts": counts,
            "reason": f"unusable exit code {exit_code!r} (timeout/spawn failure sentinel)",
        }
    if exit_code & 16:
        return {
            "status": "LAN_FAILED", "termination": "abnormal",
            "log_complete": log_complete, "counts": counts,
            "reason": f"robocopy fatal error (exit {exit_code}, bit 4 set)",
        }
    if not log_complete:
        return {
            "status": "LAN_SUSPECT", "termination": "abnormal",
            "log_complete": False, "counts": counts,
            "reason": (
                f"robocopy exit {exit_code} WITHOUT a completed job summary - "
                "log is truncated (external kill/crash). Exit code is not "
                "evidence of success (T04/A1)."
            ),
        }
    if exit_code & 8:
        return {
            "status": "LAN_PARTIAL", "termination": "normal",
            "log_complete": True, "counts": counts,
            "reason": f"robocopy copy errors (exit {exit_code}, bit 3 set)",
        }
    if 0 <= exit_code <= 7:
        # BUG-05 analysis (post-pull adversarial campaign T1, INVALID):
        # RC 4-7 (bit 2: mismatches/extras, zero copy errors) with a
        # completed summary stays LAN_COMPLETE here. Rationale: robocopy
        # finished its /MIR pass with FAILED=0 and no bit 3 — nothing
        # failed to copy; the anomaly tail is retained for forensics and
        # a warning is logged, but no failure alert fires. The live T1
        # harness proved RC 4-7 UNREACHABLE in a real /MIR run (in-sync +
        # lock -> RC 2; divergent + lock -> RC 10 FAILED; type collision
        # -> RC 11), so re-mapping this branch to PARTIAL would invent
        # production behavior (alerts + PartialRun + optional NAS
        # shutdown) with no live evidence. COMPLETE here explicitly does
        # NOT mean cryptographically verified — that is the weekly
        # integrity audit's independent job. Failed files, however, can
        # never be COMPLETE: any contradiction fails closed to SUSPECT.
        contradiction = (
            (counts is not None and counts.get("failed", 0) > 0)
            or failed_markers > 0
        )
        if contradiction:
            return {
                "status": "LAN_SUSPECT", "termination": "abnormal",
                "log_complete": True, "counts": counts,
                "reason": (
                    f"robocopy exit {exit_code} contradicts its own log "
                    f"(summary={counts}, FAILED markers={failed_markers})"
                ),
            }
        reason = f"robocopy mismatches/extras (exit {exit_code})" if exit_code >= 4 else None
        return {
            "status": "LAN_COMPLETE", "termination": "normal",
            "log_complete": True, "counts": counts, "reason": reason,
        }
    return {
        "status": "LAN_FAILED", "termination": "abnormal",
        "log_complete": log_complete, "counts": counts,
        "reason": f"unexpected robocopy exit code {exit_code}",
    }


def classify_exit_code(code: int) -> str:
    """Classify a robocopy exit code using official Microsoft bitmask rules.

    Robocopy exit codes are a bitmask of independent status flags.
    Each bit is independent and multiple can be set simultaneously.

    Official bit definitions (MS Docs):
        Bit 0 (1):  One or more files were copied successfully.
        Bit 1 (2):  Extra files or directories detected on destination.
                    No copy errors — purely informational.
        Bit 2 (4):  Mismatched files detected (size/time differ, not overwritten).
                    No copy errors — the files remain on destination as-is.
        Bit 3 (8):  Some files or directories could not be copied (copy errors,
                    retry limit exceeded). Backup is incomplete. Needs attention.
        Bit 4 (16): Serious error. Robocopy did not copy any files. Usage error
                    or insufficient access privileges on source/destination.

    Classification mapping (aligned with decide_lan_result, which is
    authoritative wherever a job log exists — this helper is only a
    no-log fallback, e.g. FY rollover when run_lan_sync yields no status):
        Codes 0–7   → LAN_COMPLETE  (no copy errors; bit-2 mismatches/extras
                                     with a finished pass are not failures)
        Codes 8–15  → LAN_PARTIAL   (bit 3 set: copy errors — sync incomplete.
                                     Fatal for affected files. Needs attention.)
        Code 16+    → LAN_FAILED    (bit 4 set: fatal process error — nothing copied.)

    Callers MUST use `result.returncode & 8` (not `status`) to distinguish between
    anomalies and copy errors. See run_lan_sync() for the enforcement of this contract.

    Returns: LAN_COMPLETE | LAN_PARTIAL | LAN_FAILED
    """
    if type(code) is not int or code < 0:
        return "LAN_FAILED"  # bool / None / sentinel: never a usable exit code
    if code & 16:
        return "LAN_FAILED"
    if code & 8:
        return "LAN_PARTIAL"
    if 0 <= code <= 7:
        return "LAN_COMPLETE"
    # Negative codes (-1 timeout sentinel) and any unexpected values → failed
    return "LAN_FAILED"


# G8: prefix used for robocopy /LOG temp files. Normal runs delete their log
# in a finally block; hard-killed runs (SCM stop, timeout kill, crash) strand
# them in %TEMP%. cleanup_orphaned_robocopy_logs() reclaims those at the start
# of each backup flow, age-gated so a live run's log is never touched.
ROBOCOPY_LOG_PREFIX = "robocopy_sync_"


def cleanup_orphaned_robocopy_logs(max_age_hours: int = 24) -> int:
    """Delete orphaned robocopy temp logs older than max_age_hours.

    Returns the number of files removed. Never raises — cleanup is
    best-effort and must not block a backup.
    """
    import time as _time
    cutoff = _time.time() - max_age_hours * 3600
    removed = 0
    tempdir = Path(tempfile.gettempdir())
    try:
        candidates = tempdir.glob(f"{ROBOCOPY_LOG_PREFIX}*.log")
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        logger.warning(f"Cleaned up {removed} orphaned robocopy log(s) older than {max_age_hours}h from {tempdir}")
    return removed


def _assert_source_not_empty(source: str) -> None:
    from pathlib import Path as _Path
    from core.health import check_source_drive as _check_src
    ok, reason = _check_src(source)
    if not ok and "appears empty" in reason:
        raise ValueError(reason)


def build_robocopy_command(source: str, dest: str, lan_config: LanConfig) -> list[str]:
    """Build robocopy /MIR command with production-verified flags.

    Flag rationale:
        /MIR    — Mirror source to dest (equivalent to /E /PURGE). Ensures
                  dest is an exact replica; orphaned destination files are pruned.
        /Z      — Restartable mode. Partially-copied files resume on retry instead
                  of starting over (important for large files over unstable LAN).
        /ZB     — Falls back to Backup mode if a file is access-denied. Requires
                  SeBackupPrivilege on the service account for full effectiveness.
        /XJ     — Exclude junction points (both files and dirs). Prevents infinite
                  recursion through Windows symlinks / volume mount points.
        /MT:n   — Multi-threaded copy. Tuned via config; default 4 matches the
                  target NAS HDD's optimal concurrency for sequential I/O.
        /R:n    — Per-file retry count on transient network errors.
        /W:n    — Wait seconds between retries.
        /V      — Verbose per-file logging (required for log parser).
        /TS     — Include source file timestamps in log output.
        /FP     — Include full file paths in log (critical for failure diagnosis).
        /NJH    — No job header (reduces log noise).
        /NDL    — No directory list (individual file lines are sufficient).
        /NP     — No progress percentage (meaningless in log files).
        NOTE: /NJS was REMOVED (P1-COUNT) — it suppressed the job summary,
        which is now the authoritative positional source for files_failed.
        /XD     — Exclude "System Volume Information" to avoid access errors on
                  NTFS system directories.
    """
    flags = [
        "/MIR",
        "/Z",
        "/ZB",
        "/XJ",
        f"/MT:{lan_config.mt_threads}",
        f"/R:{lan_config.retry_count}",
        f"/W:{lan_config.retry_wait_seconds}",
        "/V", "/TS", "/FP",
        "/NJH", "/NDL", "/NP",
        "/XF", ".AAM_TARGET_MOUNTED",
        "/XD", "System Volume Information", "$RECYCLE.BIN",
    ]

    _validate_required_flags(flags)
    _assert_source_not_empty(source)
    robocopy_exe = resolve_binary("robocopy") or "robocopy"
    return [robocopy_exe, source, dest, *flags]


def run_lan_sync(
    source: str,
    dest: str,
    lan_config: LanConfig,
    artefacts: RunArtefacts | None = None,
) -> dict:
    """Execute robocopy /MIR mirror sync and return a structured result dict.

    The FULL robocopy log is written with /UNILOG (UTF-16, so non-ASCII
    file names survive) into the run's artefact folder and kept (plan WP2);
    only a bounded tail (`lan.log_tail_kb`) is carried in the result for
    decisions, alerts and the database.

    Return dict schema:
        {
            "status":          "LAN_COMPLETE" | "LAN_PARTIAL" |
                               "LAN_SUSPECT" | "LAN_FAILED"
            "exit_code":       raw robocopy exit code (-1 when the process
                               never produced one: timeout / spawn failure)
            "error":           log tail for genuine failures (8-15, 16+) and
                               SUSPECT runs; None otherwise
            "anomaly_details": log tail for anomaly-only runs (4-7); None
                               otherwise
            "files_failed":    robocopy per-file failure count
            "termination":     "normal" | "abnormal"
            "log_complete":    completed job summary present in the log
            "counts":          parsed Files-row counters when available
            "log_retained":    path of the full robocopy log (always kept)
        }

    COMPLETE means "transfer terminated normally and its own evidence says
    the scheduled work finished" — it does NOT mean independently verified
    (see the weekly integrity audit).
    """
    artefacts = artefacts or RunArtefacts.ephemeral("lan-sync")
    tail_bytes = int(lan_config.log_tail_kb) * 1024
    cmd = build_robocopy_command(source, dest, lan_config)
    log_path = artefacts.new_path("robocopy", "log")
    cmd.append(f"/UNILOG:{log_path}")

    logger.info(f"LAN sync: {' '.join(cmd[:4])}... (full log: {log_path})")
    result = run_tool(
        "robocopy", cmd,
        timeout_s=lan_config.subprocess_timeout_seconds,
        artefacts=artefacts, log_path=log_path,
    )

    def _no_exit(error: str) -> dict:
        return {
            "status": "LAN_FAILED", "exit_code": -1, "error": error,
            "anomaly_details": None, "files_failed": 0,
            "termination": "abnormal", "log_complete": False,
            "counts": None, "log_retained": str(log_path),
        }

    if result.spawn_error:
        if "FileNotFoundError" in result.spawn_error:
            logger.error("robocopy.exe not found - is this running on Windows Server?")
            return _no_exit(f"robocopy.exe not found: {result.spawn_error}")
        logger.error(f"LAN sync OS error: {result.spawn_error}")
        return _no_exit(result.spawn_error)
    if result.timed_out:
        logger.error(f"LAN sync timed out after {lan_config.subprocess_timeout_seconds}s")
        return _no_exit(
            f"Timeout after {lan_config.subprocess_timeout_seconds}s — robocopy "
            f"process killed (full log kept: {log_path})"
        )

    code = result.exit_code
    # Integrity contract: the exit code is only interpreted together with
    # the log's own completion evidence (decide_lan_result). A killed
    # process can inherit exit 0-3 with a truncated log — that is SUSPECT,
    # never COMPLETE (T04/A1).
    decision_tail = _read_log_tail(log_path, tail_bytes, encoding="utf-16")
    decision = decide_lan_result(code, decision_tail)
    status = decision["status"]
    logger.info(
        f"LAN sync exit {code} → {status} "
        f"(termination={decision['termination']}, "
        f"log_complete={decision['log_complete']})"
    )

    error_msg = None
    anomaly_details = None
    files_failed = 0

    if status == "LAN_SUSPECT":
        error_msg = (
            f"{decision['reason']}\n"
            f"--- robocopy log tail ({len(decision_tail)} chars; full log: {log_path}) ---\n"
            f"{decision_tail}"
        )
        files_failed = failed_file_count(decision_tail, code)
        logger.error(f"LAN sync SUSPECT (exit {code}) - {decision['reason']}")
    elif status == "LAN_FAILED" or (code & 8):
        # Real failure: bit 4 (fatal) or bit 3 (copy errors) set.
        error_msg = decision_tail
        # P1-COUNT (A-prime): positional summary parse + bit-3 floor.
        files_failed = failed_file_count(decision_tail, code)
        marker_count = count_failed_lines(decision_tail)
        logger.error(
            f"LAN sync FAILED (exit {code}) - files_failed={files_failed} "
            f"({marker_count} '** FAILED:' marker(s) visible in tail); "
            f"full log: {log_path}"
        )
    elif 4 <= code <= 7:
        # Anomaly only: bit 2 set (mismatches/extras), no copy errors.
        anomaly_details = decision_tail
        logger.warning(
            f"LAN sync anomalies detected (exit {code}) - mismatches or extra "
            f"destination files found. Backup is complete. Full log: {log_path}"
        )

    return {
        "status": status,
        "exit_code": code,
        "error": error_msg,
        "anomaly_details": anomaly_details,
        "files_failed": files_failed,
        "termination": decision["termination"],
        "log_complete": decision["log_complete"],
        "counts": decision["counts"],
        "log_retained": str(log_path),
    }
