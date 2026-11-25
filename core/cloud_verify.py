"""Cloud verification — rclone check --one-way post-sync integrity.

Runs after cloud_sync to confirm source matches GCS. Uses size-only
comparison (not MD5 hash) to avoid 2-hour HDD re-hashing of 500GB.

Completion contract (F-08): an exit code alone carries no integrity
information — a killed process can inherit exit 0 via TerminateProcess
with a controlled code. VERIFIED therefore requires BOTH exit code 0
AND the process's own completion summary in its log stream
("N differences found" with N == 0, no failure verdict, no ERROR
lines). Measured on rclone v1.74.2 (Windows): a clean check ends stderr
with `NOTICE: ...: 0 differences found`; a diverged check ends with
`N differences found` + `errors while checking` + `Failed to check`
(exit 1). Rclone logs are English-only, so the match is deterministic
for this toolchain.

Exit codes (rclone check):
    0 = verified — source and GCS file counts and sizes agree
    1 = mismatch — something didn't sync or sizes diverged
    2+ = error — connection failure, invalid config, etc.
"""

import os
from pathlib import Path
import re

from loguru import logger

from core.cloud_reporter import parse_combined_diff
from core.process import resolve_binary
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool

# rclone check exit codes
_EXIT_VERIFIED = 0
_EXIT_MISMATCH = 1

# Completion evidence: rclone check's own final verdict line, e.g.
# "NOTICE: ...: 0 differences found" (clean) or "Failed to check: 1
# differences found" (diverged). Absent on killed/crashed/truncated runs.
_SUMMARY_RE = re.compile(r"(\d+)\s+differences found")
# Rclone's own failure verdict ("Failed to check: ...").
_FAILED_CHECK_RE = re.compile(r"Failed to check", re.IGNORECASE)
# Anchored log-level lines only ("2026/09/06 18:18:52 ERROR : ...") so a
# filename containing "error:" can never count as contradictory evidence.
_ERROR_LINE_RE = re.compile(r"(?m)^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} (?:ERROR|CRITICAL)\s*:")


def decide_cloud_verify_result(exit_code: int, stderr_text: str, stdout_text: str = "") -> dict:
    """Exit code + log evidence -> verification verdict.

    VERIFIED means "the check process lived to completion AND its own
    evidence says source matches GCS" — NOT merely "the OS reported
    exit 0" (F-08: TerminateProcess(pid, 0) forges exit 0 on a killed
    run, whose truncated log has no completion summary).

    Returns {"verified", "termination", "completion", "differences",
    "reason"} where termination is "normal" | "abnormal", completion
    reports whether rclone emitted its summary, differences is the
    parsed summary count (None when absent), and reason explains any
    non-verified outcome. Contradictory evidence fails closed.
    """
    log_text = f"{stderr_text or ''}\n{stdout_text or ''}"
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code < 0:
        return {
            "verified": False, "termination": "abnormal",
            "completion": False, "differences": None,
            "reason": f"unusable exit code {exit_code!r} (timeout/spawn-failure sentinel)",
        }
    matches = _SUMMARY_RE.findall(log_text)
    if not matches:
        return {
            "verified": False, "termination": "abnormal",
            "completion": False, "differences": None,
            "reason": (
                "no rclone completion summary ('N differences found') — "
                "process did not live to finish (killed/crashed/truncated, F-08)"
            ),
        }
    differences = int(matches[-1])
    failed_verdict = bool(_FAILED_CHECK_RE.search(log_text))
    error_lines = bool(_ERROR_LINE_RE.search(log_text))
    if differences == 0 and exit_code == _EXIT_VERIFIED and not failed_verdict and not error_lines:
        return {
            "verified": True, "termination": "normal",
            "completion": True, "differences": 0, "reason": None,
        }
    if differences == 0 and exit_code == _EXIT_VERIFIED:
        return {
            "verified": False, "termination": "abnormal",
            "completion": True, "differences": 0,
            "reason": (
                "exit 0 with a clean summary contradicts its own log "
                "(failure verdict or ERROR lines present)"
            ),
        }
    if differences == 0:
        return {
            "verified": False, "termination": "normal",
            "completion": True, "differences": 0,
            "reason": (
                f"rclone exited {exit_code} with a clean summary — "
                "verification failed (process/transport error)"
            ),
        }
    if exit_code == _EXIT_VERIFIED:
        return {
            "verified": False, "termination": "abnormal",
            "completion": True, "differences": differences,
            "reason": f"exit 0 contradicts its own summary ({differences} difference(s) found)",
        }
    return {
        "verified": False, "termination": "normal",
        "completion": True, "differences": differences,
        "reason": f"rclone reported {differences} difference(s) (exit {exit_code})",
    }


def verify_cloud_integrity(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
    *,
    timeout: int,
    checkers: int,
    modify_window_seconds: int,
    artefacts: RunArtefacts | None = None,
) -> dict:
    """Run rclone check --one-way --size-only to verify source matches GCS.

    Args:
        source: Source drive path.
        bucket: GCS bucket name.
        fy_prefix: Fiscal year folder prefix.
        config_path: Path to rclone config file.
        timeout: Max seconds for the check (cloud.verify_timeout_seconds).
        checkers: rclone --checkers (cloud.checkers).
        modify_window_seconds: rclone --modify-window (cloud.modify_window_seconds).
        artefacts: Run folder for the check's stdout/stderr (run_tool).

    Returns:
        {"verified": bool, "exit_code": int, "error": str | None,
         "termination": "normal" | "abnormal", "completion": bool,
         "differences": int | None, "reason": str | None}
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"

    rclone_exe = resolve_binary("rclone") or "rclone"
    cmd = [
        rclone_exe, "check",
        source, dest,
        "--one-way",               # Only check source→GCS, not reverse
        "--fast-list",             # Fewer GCS API calls (uses more memory but faster)
        "--size-only",             # Compare sizes only — avoids expensive MD5 re-hashing on HDD
        "--modify-window", f"{int(modify_window_seconds)}s",  # NTFS mtime has 2s granularity
        # NOTE: --check-first and --transfers are intentionally omitted here.
        # rclone check does no file transfers, so both flags are no-ops on this command.
        "--checkers", str(int(checkers)),  # Concurrent metadata checkers (GCS API rate limits)
        "--config", config_path,
        "--gcs-no-check-bucket",   # Bucket already verified by preflight; skip redundant check
    ]

    logger.info(f"Cloud verify: checking {source} <-> {bucket}/{fy_prefix}")

    # plan 5.1: output streams to files in the run folder; a timeout kills
    # the process tree and is reported as timed_out, never as an exit code.
    result = run_tool("rclone-verify", cmd, timeout_s=timeout,
                      artefacts=artefacts or RunArtefacts.ephemeral("cloud-verify"))

    if result.timed_out:
        logger.error(f"Cloud verify timed out after {timeout}s")
        return {
            "verified": False, "exit_code": -1, "error": f"Timeout after {timeout}s",
            "termination": "abnormal", "completion": False,
            "differences": None, "reason": "verification timed out — process did not complete",
        }
    if result.spawn_error:
        if "FileNotFoundError" in result.spawn_error:
            logger.error("rclone not found")
            return {
                "verified": False, "exit_code": -1, "error": "rclone not found",
                "termination": "abnormal", "completion": False,
                "differences": None, "reason": "rclone binary missing — no verification ran",
            }
        logger.error(f"Cloud verify error: {result.spawn_error}")
        return {
            "verified": False, "exit_code": -1, "error": result.spawn_error,
            "termination": "abnormal", "completion": False,
            "differences": None, "reason": f"verification spawn failed: {result.spawn_error}",
        }

    stderr_text = result.stderr_text()
    stdout_text = result.stdout_text()
    # F-08: the exit code is only interpreted together with the
    # process's own completion evidence (decide_cloud_verify_result).
    # A killed check can inherit exit 0 with a truncated log — that is
    # never VERIFIED, even over clean destination data.
    decision = decide_cloud_verify_result(result.exit_code, stderr_text, stdout_text)
    verified = decision["verified"]
    stderr_output = stderr_text.strip() or "no stderr"

    if verified:
        logger.info("Cloud integrity verified - source matches GCS")
    else:
        # Distinguish mismatch (exit 1) from error (exit 2+)
        label = "mismatch" if result.exit_code == _EXIT_MISMATCH else "error"
        # Log full stderr — truncating hides the actual error in production
        logger.warning(f"Cloud verify {label} (exit {result.exit_code}): {stderr_output}")

    error = _build_error_message(result.exit_code, stderr_output)
    if not verified and decision["reason"]:
        error = f"{error} ({decision['reason']})" if error else decision["reason"]

    return {
        "verified": verified,
        "exit_code": result.exit_code,
        "error": error,
        "termination": decision["termination"],
        "completion": decision["completion"],
        "differences": decision["differences"],
        "reason": decision["reason"],
    }


def _build_error_message(exit_code: int, stderr_detail: str = "") -> str | None:
    """Build a human-readable error message from rclone exit code.

    Exit 0 = no error.
    Exit 1 = mismatch (source and GCS diverged).
    Exit 2+ = rclone error (connection, auth, invalid config, etc.).
    """
    if exit_code == _EXIT_VERIFIED:
        return None
    detail = (stderr_detail or "").strip()
    if detail and detail != "no stderr":
        if len(detail) > 2000:
            detail = detail[:2000] + "..."
        suffix = f": {detail}"
    else:
        suffix = ""
    if exit_code == _EXIT_MISMATCH:
        return f"Integrity mismatch — source and GCS file counts or sizes differ{suffix}"
    return f"Rclone check failed with exit code {exit_code} — check rclone logs for details{suffix}"


def _manifest_from_unchanged(source: str, unchanged: list[str]) -> list[dict]:
    """Build manifest entries [{Path, Size, ModTime}] from local source files."""
    root = Path(source)
    entries: list[dict] = []
    for rel in unchanged:
        try:
            st = os.stat(root / rel)
        except OSError:
            continue
        entries.append({"Path": rel, "Size": st.st_size, "ModTime": st.st_mtime})
    return entries


def verify_cloud_and_diff(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
    *,
    timeout: int,
    checkers: int,
    modify_window_seconds: int,
    retries: int = 3,
    retries_sleep_seconds: int = 10,
    artefacts: RunArtefacts | None = None,
) -> dict:
    """Run one `rclone check --one-way --combined --size-only` for verify, diff, and manifest."""
    dest = f"aam_gcs:{bucket}/{fy_prefix}"
    art = artefacts or RunArtefacts.ephemeral("cloud-verify")
    combined_path = art.new_path("rclone-verify-combined", "txt")
    rclone_exe = resolve_binary("rclone") or "rclone"
    cmd = [
        rclone_exe, "check",
        source, dest,
        "--one-way",
        "--combined", str(combined_path),
        "--fast-list",
        "--size-only",
        "--modify-window", f"{int(modify_window_seconds)}s",
        "--checkers", str(int(checkers)),
        "--retries", str(int(retries)),
        "--retries-sleep", f"{int(retries_sleep_seconds)}s",
        "--config", config_path,
        "--gcs-no-check-bucket",
    ]
    logger.info(f"Cloud verify+diff: checking {source} <-> {bucket}/{fy_prefix}")
    result = run_tool("rclone-verify", cmd, timeout_s=timeout, artefacts=art)

    empty_diff = {"added": [], "removed": [], "modified": [], "unchanged": [], "_partial": True}
    if result.timed_out:
        msg = f"Timeout after {timeout}s"
        logger.error(f"Cloud verify timed out after {timeout}s")
        return {
            "verified": False, "exit_code": -1, "error": msg,
            "termination": "abnormal", "completion": False,
            "differences": None, "reason": "verification timed out — process did not complete",
            "diff": {**empty_diff, "_error": msg}, "manifest": [], "manifest_error": msg,
        }
    if result.spawn_error:
        err = "rclone not found" if "FileNotFoundError" in result.spawn_error else result.spawn_error
        reason = ("rclone binary missing — no verification ran"
                  if "FileNotFoundError" in result.spawn_error
                  else f"verification spawn failed: {result.spawn_error}")
        logger.error(f"Cloud verify error: {err}")
        return {
            "verified": False, "exit_code": -1, "error": err,
            "termination": "abnormal", "completion": False,
            "differences": None, "reason": reason,
            "diff": {**empty_diff, "_error": err}, "manifest": [], "manifest_error": err,
        }

    stderr_text = result.stderr_text()
    stdout_text = result.stdout_text()
    decision = decide_cloud_verify_result(result.exit_code, stderr_text, stdout_text)
    verified = decision["verified"]
    stderr_output = stderr_text.strip() or "no stderr"
    error = _build_error_message(result.exit_code, stderr_output)
    if not verified and decision["reason"]:
        error = f"{error} ({decision['reason']})" if error else decision["reason"]

    diff, found = parse_combined_diff(combined_path)
    manifest_error = None
    if not found:
        diff["_partial"] = True
        diff["_error"] = "combined diff file missing"
        manifest_error = "combined diff file missing"
    elif result.exit_code >= 2:
        diff["_partial"] = True
        diff["_error"] = error or f"rclone check exited {result.exit_code}"
        manifest_error = diff["_error"]

    manifest = _manifest_from_unchanged(source, diff["unchanged"]) if verified and not manifest_error else []
    if verified:
        logger.info("Cloud integrity verified - source matches GCS")
    else:
        label = "mismatch" if result.exit_code == _EXIT_MISMATCH else "error"
        logger.warning(f"Cloud verify {label} (exit {result.exit_code}): {stderr_output}")

    return {
        "verified": verified,
        "exit_code": result.exit_code,
        "error": error,
        "termination": decision["termination"],
        "completion": decision["completion"],
        "differences": decision["differences"],
        "reason": decision["reason"],
        "diff": diff,
        "manifest": manifest,
        "manifest_error": manifest_error,
    }
