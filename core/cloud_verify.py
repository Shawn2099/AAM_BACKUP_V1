"""Cloud verification — rclone check --one-way post-sync integrity."""

import subprocess

from loguru import logger

from core.process import resolve_binary


def verify_cloud_integrity(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
    timeout: int = 600,
) -> dict:
    """Run rclone check --one-way to verify source matches GCS.

    Exit 0 = everything matches. Source and GCS are byte-identical.
    Exit 1 = differences found (something didn't sync).
    Exit 2+ = error.

    Args:
        source: Source drive path.
        bucket: GCS bucket name.
        fy_prefix: Fiscal year folder prefix.
        config_path: Path to rclone config file.
        timeout: Max seconds for the check (default 600).

    Returns:
        {"verified": bool, "exit_code": int, "error": str | None}
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"

    rclone_exe = resolve_binary("rclone") or "rclone"
    cmd = [
        rclone_exe, "check",
        source, dest,
        "--one-way",
        "--fast-list",
        "--config", config_path,
        "--gcs-no-check-bucket",
    ]

    logger.info(f"Cloud verify: checking {source} ↔ {bucket}/{fy_prefix}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        verified = result.returncode == 0

        if verified:
            logger.info("Cloud integrity verified — source matches GCS")
        else:
            stderr_snippet = result.stderr[:200] if result.stderr else "no stderr"
            logger.warning(f"Cloud integrity mismatch (exit {result.returncode}): {stderr_snippet}")

        return {
            "verified": verified,
            "exit_code": result.returncode,
            "error": None if verified else f"Exit {result.returncode}: mismatch detected",
        }

    except subprocess.TimeoutExpired:
        logger.error("Cloud verify timed out after 600s")
        return {"verified": False, "exit_code": -1, "error": "Timeout"}
    except FileNotFoundError:
        logger.error("rclone not found")
        return {"verified": False, "exit_code": -1, "error": "rclone not found"}
    except OSError as e:
        logger.error(f"Cloud verify error: {e}")
        return {"verified": False, "exit_code": -1, "error": str(e)}
