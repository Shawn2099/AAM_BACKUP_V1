"""Cloud preflight — rclone check --one-way dry-run before sync.

Fast metadata-only comparison. Catches auth failures, missing buckets,
and config errors before the multi-hour sync attempt.
"""

import os
import subprocess
import tempfile
from pathlib import Path

from loguru import logger


def _write_temp_config(
    gcs_key_path: str,
    location: str,
    project_number: str = "920173882190",
) -> str:
    """Write temporary rclone config file for GCS access.

    Uses mkstemp + close to avoid Windows file handle lock.
    Returns path to temp file. Caller must clean up.
    """
    key_abs = str(Path(gcs_key_path).resolve()).replace("\\", "/")
    content = f"""[aam_gcs]
type = google cloud storage
service_account_file = {key_abs}
project_number = {project_number}
object_acl =
bucket_acl =
bucket_policy_only = true
location = {location}
storage_class = COLDLINE
"""
    fd, cfg_path = tempfile.mkstemp(suffix=".conf", prefix="rclone_")
    os.close(fd)
    Path(cfg_path).write_text(content, encoding="utf-8")
    return cfg_path


def run_cloud_dry_run(
    source: str,
    bucket: str,
    fy_prefix: str,
    gcs_key_path: str,
    location: str = "asia-south1",
) -> dict:
    """Run rclone check --one-way as dry-run validation.

    Exit 0 = everything matches (source and GCS are in sync).
    Exit 1 = differences found (normal — new files since last run).
    Exit 2+ = error (config, auth, network).

    Args:
        source: Source drive path.
        bucket: GCS bucket name.
        fy_prefix: Fiscal year folder prefix (e.g. "FY26-27").
        gcs_key_path: Path to GCS service account key file.
        location: GCS region.

    Returns:
        {"ok": bool, "matched": bool, "exit_code": int, "error": str | None}
    """
    config_path = None
    try:
        config_path = _write_temp_config(gcs_key_path, location)
        dest = f"aam_gcs:{bucket}/{fy_prefix}"

        cmd = [
            "rclone", "check",
            source, dest,
            "--one-way",
            "--fast-list",
            "--config", config_path,
            "--gcs-no-check-bucket",
        ]

        logger.info(f"Cloud dry-run: checking {source} ↔ {bucket}/{fy_prefix}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 min — metadata only
        )

        code = result.returncode
        ok = code < 2  # Only 0 and 1 are "valid" for rclone check
        matched = code == 0

        if not ok:
            stderr_snippet = result.stderr[:300] if result.stderr else "no stderr"
            logger.error(f"Cloud dry-run failed (exit {code}): {stderr_snippet}")
            return {"ok": False, "matched": False, "exit_code": code, "error": f"Exit {code}: {stderr_snippet}"}

        logger.info(f"Cloud dry-run OK (matched={matched}, exit {code})")
        return {"ok": True, "matched": matched, "exit_code": code, "error": None}

    except subprocess.TimeoutExpired:
        logger.error("Cloud dry-run timed out after 300s")
        return {"ok": False, "matched": False, "exit_code": -1, "error": "Timeout"}
    except FileNotFoundError:
        logger.error("rclone not found")
        return {"ok": False, "matched": False, "exit_code": -1, "error": "rclone not found"}
    except OSError as e:
        logger.error(f"Cloud dry-run error: {e}")
        return {"ok": False, "matched": False, "exit_code": -1, "error": str(e)}
    finally:
        if config_path:
            try:
                Path(config_path).unlink()
            except OSError:
                pass
