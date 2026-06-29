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
        {"verified": bool, "exit_code": int, "error": str | None}
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
            timeout=timeout,
        )

        verified = result.returncode == _EXIT_VERIFIED

        if verified:
            logger.info("Cloud integrity verified — source matches GCS")
        else:
            # Distinguish mismatch (exit 1) from error (exit 2+)
            if result.returncode == _EXIT_MISMATCH:
                label = "mismatch"
            else:
                label = "error"
            # Log full stderr — truncating hides the actual error in production
            stderr_output = result.stderr.strip() if result.stderr else "no stderr"
            logger.warning(f"Cloud verify {label} (exit {result.returncode}): {stderr_output}")

        return {
            "verified": verified,
            "exit_code": result.returncode,
            "error": _build_error_message(result.returncode),
        }

    except subprocess.TimeoutExpired:
        logger.error(f"Cloud verify timed out after {timeout}s")
        return {"verified": False, "exit_code": -1, "error": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        logger.error("rclone not found")
        return {"verified": False, "exit_code": -1, "error": "rclone not found"}
    except OSError as e:
        logger.error(f"Cloud verify error: {e}")
        return {"verified": False, "exit_code": -1, "error": str(e)}


def _build_error_message(exit_code: int) -> str | None:
    """Build a human-readable error message from rclone exit code.

    Exit 0 = no error.
    Exit 1 = mismatch (source and GCS diverged).
    Exit 2+ = rclone error (connection, auth, invalid config, etc.).
    """
    if exit_code == _EXIT_VERIFIED:
        return None
    if exit_code == _EXIT_MISMATCH:
        return "Integrity mismatch — source and GCS file counts or sizes differ"
    return f"Rclone check failed with exit code {exit_code} — check rclone logs for details"
