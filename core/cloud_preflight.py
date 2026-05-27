"""Cloud preflight — rclone check --one-way dry-run before sync.

Fast metadata-only comparison. Catches auth failures, missing buckets,
and config errors before the multi-hour sync attempt.
"""

import subprocess
from pathlib import Path

from loguru import logger

from core.rclone_config import write_temp_config as _write_temp_config


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
