"""Cloud verification — rclone check --one-way post-sync integrity.

Runs after cloud_sync to confirm source matches GCS. Uses size-only
comparison (not MD5 hash) to avoid 2-hour HDD re-hashing of 500GB.

Exit codes (rclone check):
    0 = verified — source and GCS file counts and sizes agree
    1 = mismatch — something didn't sync or sizes diverged
    2+ = error — connection failure, invalid config, etc.
"""

import subprocess

from loguru import logger

from core.lan_sync import MIRROR_EXCLUDED_DIRS
from core.process import resolve_binary

# rclone check exit codes
_EXIT_VERIFIED = 0
_EXIT_MISMATCH = 1


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
         "error_class": str | None}
        `error_class` is the NV-02 classification of a failed check
        (None on success and on local exceptions — timeout/missing binary —
        which are not GCS outcomes).
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
        # R4: same exclusions as the sync (see MIRROR_EXCLUDED_DIRS) — the
        # check must compare the same file set the sync transferred, or
        # excluded directories would report as "missing from cloud".
        *[opt for d in MIRROR_EXCLUDED_DIRS for opt in ("--exclude", d)],
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
            timeout=timeout,
        )

        verified = result.returncode == _EXIT_VERIFIED
        error_class = _classify_check_failure(result.returncode, result.stderr or "")

        if verified:
            logger.info("Cloud integrity verified — source matches GCS")
        else:
            # NV-02: distinguish a true integrity mismatch from a GCS access
            # failure — rclone check exits 1 for BOTH, so the exit code alone
            # mislabels an auth/network outage as "data mismatch" (live
            # verified: bad credentials → exit 1, "N errors while checking").
            label = error_class
            # Log full stderr — truncating hides the actual error in production
            stderr_output = result.stderr.strip() if result.stderr else "no stderr"
            logger.warning(f"Cloud verify {label} (exit {result.returncode}): {stderr_output}")

        return {
            "verified": verified,
            "exit_code": result.returncode,
            "error": _build_error_message(result.returncode, error_class),
            "error_class": error_class,
        }

    except subprocess.TimeoutExpired:
        logger.error(f"Cloud verify timed out after {timeout}s")
        return {"verified": False, "exit_code": -1,
                "error": f"Timeout after {timeout}s", "error_class": None}
    except FileNotFoundError:
        logger.error("rclone not found")
        return {"verified": False, "exit_code": -1,
                "error": "rclone not found", "error_class": None}
    except OSError as e:
        logger.error(f"Cloud verify error: {e}")
        return {"verified": False, "exit_code": -1, "error": str(e),
                "error_class": None}


def _classify_check_failure(exit_code: int, stderr_text: str) -> str | None:
    """Classify a failed `rclone check` (NV-02).

    rclone check exits 1 for BOTH a true integrity mismatch AND a
    destination access failure (bad credentials, network drop, missing
    bucket) — live verified on GCS. The NOTICE summary line distinguishes
    them: an access failure reports "N errors while checking" where the
    "differences" are the unreadable files; a true mismatch reports
    differences with zero check errors.

    Returns:
        None for exit 0;
        "access_error" — destination read errors dominate (NOT a data mismatch);
        "mismatch"     — true size/count divergence;
        "mixed"        — both signals present;
        "error"        — exit 2+ (config/usage-level failure).
    """
    import re

    if exit_code == _EXIT_VERIFIED:
        return None
    if exit_code != _EXIT_MISMATCH:
        return "error"

    err_m = re.search(r"(\d+) errors? while checking", stderr_text)
    diff_m = re.search(r"(\d+) differences? found", stderr_text)
    errors = int(err_m.group(1)) if err_m else 0
    diffs = int(diff_m.group(1)) if diff_m else 0

    if errors and errors >= diffs:
        return "access_error"
    if errors and diffs:
        return "mixed"
    return "mismatch"


def _build_error_message(exit_code: int, error_class: str | None = None) -> str | None:
    """Build a human-readable error message from rclone exit code + class.

    Exit 0 = no error.
    Exit 1 = mismatch (source and GCS diverged) — or a GCS access failure,
        which NV-02's classifier re-labels so an auth/network outage is not
        alerted as "data may be missing".
    Exit 2+ = rclone error (connection, auth, invalid config, etc.).
    """
    if exit_code == _EXIT_VERIFIED:
        return None
    if error_class == "access_error":
        return (
            "Cloud verify could not read GCS (access/auth/network error) — "
            "this is NOT a confirmed integrity mismatch; GCS reachability "
            "must be checked before drawing conclusions about the data"
        )
    if error_class == "mixed":
        return (
            "Cloud verify found differences AND destination read errors — "
            "treat as unverified until GCS is reachable; the mismatch count "
            "is unreliable while reads fail"
        )
    if exit_code == _EXIT_MISMATCH:
        return "Integrity mismatch — source and GCS file counts or sizes differ"
    return f"Rclone check failed with exit code {exit_code} — check rclone logs for details"
