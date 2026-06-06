"""Cloud preflight — rclone check --one-way dry-run before sync.

Fast metadata-only comparison. Catches auth failures, missing buckets,
and config errors before the multi-hour sync attempt.
"""

import subprocess

from loguru import logger

from core.rclone_config import temp_rclone_config
from core.process import resolve_binary


def run_cloud_dry_run(
    source: str,
    bucket: str,
    fy_prefix: str,
    gcs_key_path: str,
    project_number: str,
    storage_class: str,
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
    with temp_rclone_config(gcs_key_path, location, project_number, storage_class) as config_path:
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

        logger.info(f"Cloud dry-run: checking {source} ↔ {bucket}/{fy_prefix}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            logger.error("Cloud dry-run timed out after 300s")
            return {"ok": False, "matched": False, "exit_code": -1, "error": "Timeout"}
        except FileNotFoundError:
            logger.error("rclone not found")
            return {"ok": False, "matched": False, "exit_code": -1, "error": "rclone not found"}
        except OSError as e:
            logger.error(f"Cloud dry-run error: {e}")
            return {"ok": False, "matched": False, "exit_code": -1, "error": str(e)}

        code = result.returncode
        ok = code < 2
        matched = code == 0

        if not ok:
            stderr_snippet = result.stderr[:300] if result.stderr else "no stderr"
            logger.error(f"Cloud dry-run failed (exit {code}): {stderr_snippet}")
            return {"ok": False, "matched": False, "exit_code": code, "error": f"Exit {code}: {stderr_snippet}"}

        logger.info(f"Cloud dry-run OK (matched={matched}, exit {code})")
        return {"ok": True, "matched": matched, "exit_code": code, "error": None}
