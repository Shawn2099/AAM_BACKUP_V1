"""Cloud preflight — two-probe auth & source check before sync.

Replaces the old rclone check --one-way (full HDD scan) with two fast probes
that together validate every failure mode the old preflight covered, but in
~2 seconds with zero HDD traversal.

Probe A (Python, ~0ms):
    os.stat + iterdir on the source drive root — confirms the drive is
    mounted and the filesystem is readable without touching any file contents.

Probe B (rclone, ~1-3s):
    rclone lsjson --max-depth 0 on the GCS destination — one API call that
    validates:
        - Service Account JSON key is valid and not expired
        - Network path to Google Cloud Storage is reachable
        - Bucket exists (--gcs-no-check-bucket intentionally ABSENT to force
          a real storage.buckets.get IAM check)
        - FY prefix path is queryable (exit 0 on empty prefix = first run OK)

Step 2 (sync) and Step 3 (verify) are unchanged.
"""

from pathlib import Path

from loguru import logger

from core.process import resolve_binary
from core.rclone_config import temp_rclone_config
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool


def run_cloud_dry_run(
    source: str,
    bucket: str,
    fy_prefix: str,
    gcs_key_path: str,
    project_number: str,
    storage_class: str,
    *,
    location: str,
    timeout: int,
    retries: int,
    retries_sleep_seconds: int,
    artefacts: RunArtefacts | None = None,
) -> dict:
    """Two-probe preflight: source drive alive + GCS auth/bucket probe.

    Does NOT scan the source HDD. Does NOT compare files. Purely confirms
    both ends of the backup pipeline are reachable and accessible before
    committing to a multi-hour sync run.

    Args:
        source:         Source drive path (e.g. "E:\\\\").
        bucket:         GCS bucket name.
        fy_prefix:      Fiscal year folder prefix (e.g. "FY26-27").
        gcs_key_path:   Path to GCS service account key JSON file.
        project_number: GCP project number.
        storage_class:  GCS storage class (e.g. "COLDLINE").
        location:       GCS region (cloud.location).
        timeout:        Seconds to wait for the rclone network probe
                        (cloud.preflight_timeout_seconds).
        retries / retries_sleep_seconds: rclone --retries / --retries-sleep
                        for the probe (cloud.probe_retries / probe_retries_sleep_seconds).
        artefacts:      Run folder for the probe's stdout/stderr (run_tool).

    Returns:
        {"ok": bool, "exit_code": int, "error": str | None}
    """
    # ── Probe A: Source drive alive (Python, zero HDD IO) ──────────────────
    source_path = Path(source)

    if not source_path.exists():
        msg = f"Source drive not accessible: {source}"
        logger.error(f"Cloud preflight [A] FAILED - {msg}")
        return {"ok": False, "exit_code": -1, "error": msg}

    try:
        # One iterdir() call: forces kernel to open the directory and prove
        # the filesystem is readable. Not a traversal — stops after one entry.
        next(source_path.iterdir())
    except StopIteration:
        # M8: this preflight probe is diagnostic-only. It does NOT mean the
        # pipeline will run: guard G1 (flow.cloud_guards_task) blocks a
        # source without real files because rclone sync would mirror the
        # emptiness onto the destination. An empty source reaching here
        # almost always means the drive is unmounted.
        logger.warning(
            f"Cloud preflight [A]: source drive is EMPTY ({source}) - "
            "guard G1 will block the sync and ask for a human decision."
        )
    except OSError as exc:
        msg = f"Source drive read error ({source}): {exc}"
        logger.error(f"Cloud preflight [A] FAILED - {msg}")
        return {"ok": False, "exit_code": -1, "error": msg}

    logger.info(f"Cloud preflight [A] OK - source drive accessible: {source}")

    # ── Probe B: GCS auth + bucket probe (rclone lsjson, zero HDD IO) ──────
    with temp_rclone_config(gcs_key_path, location, project_number, storage_class) as config_path:
        dest = f"aam_gcs:{bucket}/{fy_prefix}"
        rclone_exe = resolve_binary("rclone") or "rclone"

        # --gcs-no-check-bucket intentionally ABSENT: forces storage.buckets.get
        # IAM check, catching deleted/renamed buckets before the sync starts.
        #
        # --max-depth 0: single GCS API call (one page, no pagination).
        # Returns exit 0 with [] on empty prefix (correct on first run).
        #
        # --retries: retry transient network errors (DNS flake, TCP reset).
        cmd = [
            rclone_exe, "lsjson",
            dest,
            "--max-depth", "0",
            "--retries", str(int(retries)),
            "--retries-sleep", f"{int(retries_sleep_seconds)}s",
            "--config", config_path,
        ]

        logger.info(f"Cloud preflight [B]: probing GCS {bucket}/{fy_prefix}")

        result = run_tool("rclone-preflight-probe", cmd, timeout_s=timeout,
                          artefacts=artefacts or RunArtefacts.ephemeral("cloud-preflight"))
        if result.timed_out:
            logger.error(f"Cloud preflight [B] FAILED - GCS probe timed out after {timeout}s")
            return {"ok": False, "exit_code": -1, "error": f"Timeout after {timeout}s"}
        if result.spawn_error:
            if "FileNotFoundError" in result.spawn_error:
                logger.error("Cloud preflight [B] FAILED - rclone not found in PATH "
                             "or configured binary path")
                return {"ok": False, "exit_code": -1, "error": "rclone not found"}
            msg = f"OS error launching rclone: {result.spawn_error}"
            logger.error(f"Cloud preflight [B] FAILED - {msg}")
            return {"ok": False, "exit_code": -1, "error": msg}

        code = result.exit_code

        if code != 0:
            # Log full stderr — truncating hides the actual error in production
            stderr_output = result.stderr_text().strip() or "no stderr"
            msg = f"Exit {code}: {stderr_output}"
            logger.error(f"Cloud preflight [B] FAILED - {msg}")
            return {"ok": False, "exit_code": code, "error": msg}

        logger.info("Cloud preflight [B] OK - GCS reachable, credentials valid, bucket accessible")
        return {"ok": True, "exit_code": 0, "error": None}
