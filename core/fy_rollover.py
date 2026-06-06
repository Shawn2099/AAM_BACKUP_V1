"""Fiscal year rollover — detects FY boundary, runs final backup of closing FY,
creates new FY folders, and atomically updates config.yaml.

Called by launch.py at startup before normal backup processing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from core.cloud_sync import run_cloud_sync
from core.lan_sync import run_lan_sync
from core.time_utils import get_fy_prefix

FY_PATTERN = re.compile(r"^FY\d{2}-\d{2}$", re.IGNORECASE)


class RolloverError(Exception):
    """FY rollover could not complete — config unchanged, retry next run."""


def _fy_name(path_str: str) -> str | None:
    """Extract FY suffix from path. E.g. 'E:\\SOURCE\\FY26-27' → 'FY26-27'.

    Works cross-platform with both forward and backslash separators."""
    parts = path_str.replace("\\", "/").rstrip("/").split("/")
    name = parts[-1]
    return name.upper() if FY_PATTERN.match(name) else None


def _parent_path(path_str: str) -> str:
    """Get parent directory preserving separator style.

    'E:\\SOURCE\\FY26-27' → 'E:\\SOURCE'
    '\\\\server\\share\\FY26-27' → '\\\\server\\share'
    '/mnt/source/FY26-27' → '/mnt/source'
    """
    parts = path_str.replace("\\", "/").rstrip("/").split("/")
    parent_parts = parts[:-1] if len(parts) > 1 else parts

    if path_str.startswith("\\\\"):
        return "\\\\" + "\\".join(parent_parts[2:]) if len(parent_parts) > 3 else path_str
    elif "\\" in path_str:
        return "\\".join(parent_parts)
    else:
        return "/".join(parent_parts)


def _child_path(root: str, fy: str) -> str:
    """Append FY folder to root using same separator style."""
    sep = "\\" if "\\" in root else "/"
    return f"{root.rstrip(sep)}{sep}{fy}"


def detect_rollover(source_drive: str, lan_destination: str) -> bool:
    """Return True if the configured FY suffix doesn't match the computed FY prefix."""
    current_fy = _fy_name(source_drive) or _fy_name(lan_destination)
    if current_fy is None:
        # Warn operators so they know rollover is permanently skipped for this config.
        logger.warning(
            "FY rollover: no FY suffix found in source_drive or lan_destination — "
            "rollover detection is permanently disabled. "
            f"source_drive={source_drive!r}, lan_destination={lan_destination!r}"
        )
        return False
    computed = get_fy_prefix()
    return current_fy != computed


def run_final_backup(source_drive: str, lan_destination: str,
                     lan_config, cloud_config, paths_config,
                     config, old_fy: str) -> tuple[bool, bool]:
    """Run one final backup of the closing FY to both destinations.

    Returns (cloud_ok, lan_ok). Only the cloud pipeline uses old_fy
    GCS prefix; LAN uses lan_destination directly."""
    cloud_ok = lan_ok = False

    if cloud_config.enabled:
        try:
            logger.info(f"FY rollover: running final cloud backup to GCS FY {old_fy}")
            result = run_cloud_sync(
                source=source_drive,
                bucket=cloud_config.bucket,
                fy_prefix=old_fy,
                gcs_key_path=paths_config.gcs_key_path,
                project_number=cloud_config.project_number,
                storage_class=cloud_config.storage_class,
                location=cloud_config.location,
                bwlimit=cloud_config.bandwidth_limit,
                retries=cloud_config.retry_count,
                transfers=cloud_config.transfers,
                checkers=cloud_config.checkers,
                timeout=cloud_config.subprocess_timeout_seconds,
            )
            exit_code = result.get("exit_code", -1)
            if exit_code in (0, 9):
                cloud_ok = True
                logger.info(f"FY rollover: final cloud backup OK (exit {exit_code})")
            else:
                logger.error(f"FY rollover: final cloud backup failed (exit {exit_code})")
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            # Narrow to runtime/IO errors only.  Config typos (AttributeError,
            # TypeError) should propagate loudly so operators fix them.
            logger.error(f"FY rollover: final cloud backup error: {e}")

    if lan_config.enabled:
        try:
            from core.wol import ensure_server_online

            if config.wol.enabled:
                logger.info("FY rollover: waking LAN server before final backup")
                ensure_server_online(config)

            logger.info(f"FY rollover: running final LAN backup to {lan_destination}")
            result = run_lan_sync(
                source=source_drive,
                dest=lan_destination,
                lan_config=lan_config,
            )
            exit_code = result.get("exit_code", -1)
            if exit_code in range(0, 8):
                lan_ok = True
                logger.info(f"FY rollover: final LAN backup OK (exit {exit_code})")
            else:
                logger.error(f"FY rollover: final LAN backup failed (exit {exit_code})")

            if config.wol.enabled and config.lan.shutdown_after_backup:
                try:
                    from core.shutdown import shutdown_server

                    logger.info(f"FY rollover: shutting down backup server {config.wol.server_ip}")
                    shutdown_server(config.wol.server_ip)
                except (OSError, RuntimeError) as e:
                    logger.warning(f"FY rollover: server shutdown failed (non-critical): {e}")
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            # Narrow to runtime/IO errors only.  Config typos (AttributeError,
            # TypeError) should propagate loudly so operators fix them.
            logger.error(f"FY rollover: final LAN backup error: {e}")

    return cloud_ok, lan_ok


def create_new_fy_folders(source_root: str, lan_root: str, new_fy: str) -> dict[str, Path]:
    """Create new FY folders on source and LAN. Returns dict of created Paths.

    Source folder creation is mandatory — raises on failure (local disk problem).
    LAN folder creation is best-effort — logs a clear error if NAS is offline
    at rollover time, but does NOT block the rollover. The operator must create
    the LAN folder manually before the next LAN backup runs.
    """
    created = {}
    new_source = Path(_child_path(source_root, new_fy))
    new_lan = Path(_child_path(lan_root, new_fy))

    # Source folder: local disk — must succeed
    new_source.mkdir(parents=True, exist_ok=True)
    created["source"] = new_source
    logger.info(f"FY rollover: created source folder {new_source}")

    # LAN folder: network path — may fail if NAS is offline at rollover time
    try:
        new_lan.mkdir(parents=True, exist_ok=True)
        created["lan"] = new_lan
        logger.info(f"FY rollover: created LAN folder {new_lan}")
    except OSError as e:
        logger.error(
            f"FY rollover: FAILED to create LAN folder {new_lan}: {e}. "
            f"The NAS may have been offline at rollover time. "
            f"ACTION REQUIRED: Manually create '{new_lan}' on the NAS "
            f"before the next LAN backup or it will fail with 'destination not found'."
        )
        # Non-blocking — config.yaml still gets updated to new FY.
        # The LAN backup will fail gracefully at the preflight stage until the
        # folder is created manually.

    return created


def update_config_yaml(config_path: str, source_root: str, lan_root: str,
                       new_fy: str) -> None:
    """Atomically update source_drive and lan_destination in config.yaml.

    Uses ruamel.yaml round-trip mode to preserve comments and formatting.
    """
    from ruamel.yaml import YAML

    path = Path(config_path)

    yaml = YAML()
    yaml.preserve_quotes = True

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.load(f)

    new_source = _child_path(source_root, new_fy)
    new_lan = _child_path(lan_root, new_fy)

    old_source = cfg["paths"]["source_drive"]
    old_lan = cfg["paths"]["lan_destination"]

    cfg["paths"]["source_drive"] = new_source
    cfg["paths"]["lan_destination"] = new_lan

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".yaml", prefix=".config_rollover_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f)
        os.replace(tmp_path, str(path))
        logger.info(
            f"FY rollover: config.yaml updated\n"
            f"  source:  {old_source} → {new_source}\n"
            f"  LAN:     {old_lan} → {new_lan}"
        )
    except Exception:
        os.unlink(tmp_path)
        raise


def run_archive_transition(bucket: str, old_fy: str, gcs_key_path: str) -> bool:
    """Transition all GCS objects under old_fy/ to ARCHIVE storage class.

    Executes the official ``gcloud storage objects update --recursive`` command
    using stateless service-account authentication injected via the
    GOOGLE_APPLICATION_CREDENTIALS environment variable.  No data is
    downloaded or re-uploaded; only object metadata is rewritten server-side.

    This function is intentionally **non-blocking**: any failure is logged as
    a WARNING so the FY rollover can complete regardless.  The IT admin can
    retry the transition manually from the Google Cloud Console if needed.

    Args:
        bucket:       GCS bucket name (e.g. ``"aam-backup-bucket"``).
        old_fy:       Closing FY prefix (e.g. ``"FY25-26"``).
        gcs_key_path: Absolute path to the service account JSON key file.

    Returns:
        ``True`` on success, ``False`` on any failure.
    """
    logger.info(
        f"FY rollover: transitioning GCS objects under gs://{bucket}/{old_fy}/ "
        "to ARCHIVE storage class"
    )

    # Stateless auth: inject the service-account key path into the subprocess
    # environment.  gcloud honours this variable on every invocation, so no
    # persistent ``gcloud auth`` login is required on the server.
    env = os.environ.copy()
    env["GOOGLE_APPLICATION_CREDENTIALS"] = str(gcs_key_path)

    try:
        # Resolve gcloud dynamically (essential for Windows to pick up gcloud.cmd)
        gcloud_exe = shutil.which("gcloud")
        if not gcloud_exe:
            raise FileNotFoundError("gcloud")

        # Explicitly authenticate the service account if a key file is provided.
        # If no key file exists (e.g. using Windows Credential Manager or
        # persistent ambient authentication), skip this and rely on active session.
        if Path(gcs_key_path).is_file():
            auth_cmd = [
                gcloud_exe, "auth", "activate-service-account",
                f"--key-file={gcs_key_path}"
            ]
            auth_result = subprocess.run(
                auth_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            if auth_result.returncode != 0:
                logger.warning(
                    f"FY rollover: gcloud authentication failed (exit {auth_result.returncode}). "
                    f"Stderr: {(auth_result.stderr or '').strip()[:1000]}"
                )
                return False
        else:
            logger.info("FY rollover: No GCS key file found, assuming ambient/Windows authentication for gcloud.")

        # Use --recursive on the bare prefix — no ** glob — to avoid shell
        # wildcard expansion on Windows PowerShell / cmd.exe environments.
        cmd: list[str] = [
            gcloud_exe, "storage", "objects", "update",
            f"gs://{bucket}/{old_fy}/",
            "--storage-class=ARCHIVE",
            "--recursive",
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,   # 10 min — metadata-only rewrite; no data transfer
        )
        if result.returncode == 0:
            logger.info(
                f"FY rollover: archive transition succeeded for gs://{bucket}/{old_fy}/"
            )
            return True

        # Truncate stderr to avoid multi-megabyte log entries on large buckets.
        stderr_snippet = (result.stderr or "").strip()[:2000]
        logger.warning(
            f"FY rollover: archive transition failed "
            f"(exit {result.returncode}) for gs://{bucket}/{old_fy}/. "
            f"Stderr: {stderr_snippet}"
        )
        return False

    except FileNotFoundError:
        logger.warning(
            "FY rollover: 'gcloud' CLI not found — archive transition skipped. "
            "Install the Google Cloud SDK and ensure 'gcloud' is on the system PATH."
        )
        return False
    except subprocess.TimeoutExpired:
        logger.warning(
            f"FY rollover: archive transition timed out after 600 s "
            f"for gs://{bucket}/{old_fy}/"
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"FY rollover: unexpected error during archive transition: {exc}"
        )
        return False


def rollover(config_path: str = "config.yaml") -> bool:
    """Detect FY boundary and execute rollover if needed.

    Returns True if rollover was performed, False if no rollover needed.
    Raises RolloverError if final backup fails (config unchanged, retry next run).
    """
    from models.config import load_config

    config = load_config(config_path)
    source_drive = config.paths.source_drive
    lan_destination = config.paths.lan_destination

    if not detect_rollover(source_drive, lan_destination):
        return False

    src_fy = _fy_name(source_drive)
    lan_fy = _fy_name(lan_destination)

    old_fy = src_fy or lan_fy
    if old_fy is None:
        return False

    new_fy = get_fy_prefix()
    src_root = _parent_path(source_drive)
    lan_root = _parent_path(lan_destination)

    logger.info(f"FY rollover: {old_fy} → {new_fy}")
    logger.info(f"  Source root: {src_root}")
    logger.info(f"  LAN root:    {lan_root}")

    cloud_ok, lan_ok = run_final_backup(
        source_drive=source_drive,
        lan_destination=lan_destination,
        lan_config=config.lan,
        cloud_config=config.cloud,
        paths_config=config.paths,
        config=config,
        old_fy=old_fy,
    )

    required = []
    if config.cloud.enabled and not cloud_ok:
        required.append("cloud")
    if config.lan.enabled and not lan_ok:
        required.append("LAN")

    if required:
        msg = f"FY rollover blocked: final backup failed for {', '.join(required)}"
        logger.error(msg)
        raise RolloverError(msg)

    # Attempt to move the entire closing-year GCS folder to Archive tier.
    # Non-blocking: rollover proceeds even if this step fails.
    archive_ok = False
    if config.cloud.enabled:
        archive_ok = run_archive_transition(
            bucket=config.cloud.bucket,
            old_fy=old_fy,
            gcs_key_path=config.paths.gcs_key_path,
        )

    create_new_fy_folders(src_root, lan_root, new_fy)

    update_config_yaml(config_path, src_root, lan_root, new_fy)

    logger.info(
        f"FY rollover complete: {old_fy} → {new_fy} | archive_ok={archive_ok}"
    )
    return True
