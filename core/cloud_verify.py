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

import re
import subprocess

from loguru import logger

from core.process import resolve_binary

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
    timeout: int = 14400,
) -> dict:
    """Run rclone check --one-way --size-only to verify source matches GCS.

    Args:
        source: Source drive path.
        bucket: GCS bucket name.
        fy_prefix: Fiscal year folder prefix.
        config_path: Path to rclone config file.
        timeout: Max seconds for the check (default 14400 — 4 hours for large HDD datasets).

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
        "--modify-window", "2s",   # NTFS mtime has 2s granularity; default 1ns causes false positives
        # NOTE: --check-first and --transfers are intentionally omitted here.
        # rclone check does no file transfers, so both flags are no-ops on this command.
        "--checkers", "4",         # Concurrent metadata checkers — safe for GCS API rate limits
        "--config", config_path,
        "--gcs-no-check-bucket",   # Bucket already verified by preflight; skip redundant check
    ]

    logger.info(f"Cloud verify: checking {source} <-> {bucket}/{fy_prefix}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

        # F-08: the exit code is only interpreted together with the
        # process's own completion evidence (decide_cloud_verify_result).
        # A killed check can inherit exit 0 with a truncated log — that is
        # never VERIFIED, even over clean destination data.
        decision = decide_cloud_verify_result(result.returncode, result.stderr, result.stdout)
        verified = decision["verified"]
        stderr_output = (result.stderr or "").strip() if result.stderr else "no stderr"

        if verified:
            logger.info("Cloud integrity verified - source matches GCS")
        else:
            # Distinguish mismatch (exit 1) from error (exit 2+)
            if result.returncode == _EXIT_MISMATCH:
                label = "mismatch"
            else:
                label = "error"
            # Log full stderr — truncating hides the actual error in production
            logger.warning(f"Cloud verify {label} (exit {result.returncode}): {stderr_output}")

        error = _build_error_message(result.returncode, stderr_output)
        if not verified and decision["reason"]:
            error = f"{error} ({decision['reason']})" if error else decision["reason"]

        return {
            "verified": verified,
            "exit_code": result.returncode,
            "error": error,
            "termination": decision["termination"],
            "completion": decision["completion"],
            "differences": decision["differences"],
            "reason": decision["reason"],
        }

    except subprocess.TimeoutExpired:
        logger.error(f"Cloud verify timed out after {timeout}s")
        return {
            "verified": False, "exit_code": -1, "error": f"Timeout after {timeout}s",
            "termination": "abnormal", "completion": False,
            "differences": None, "reason": "verification timed out — process did not complete",
        }
    except FileNotFoundError:
        logger.error("rclone not found")
        return {
            "verified": False, "exit_code": -1, "error": "rclone not found",
            "termination": "abnormal", "completion": False,
            "differences": None, "reason": "rclone binary missing — no verification ran",
        }
    except OSError as e:
        logger.error(f"Cloud verify error: {e}")
        return {
            "verified": False, "exit_code": -1, "error": str(e),
            "termination": "abnormal", "completion": False,
            "differences": None, "reason": f"verification spawn failed: {e}",
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
