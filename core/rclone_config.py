"""Shared rclone temporary config writer — single source of truth.

Used by cloud_preflight, cloud_sync, and cloud_verify/reporter callers.
"""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


def write_temp_config(
    gcs_key_path: str,
    location: str,
    project_number: str,
    storage_class: str,
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
storage_class = {storage_class}
"""
    fd, cfg_path = tempfile.mkstemp(suffix=".conf", prefix="rclone_")
    os.close(fd)
    Path(cfg_path).write_text(content, encoding="utf-8")
    return cfg_path


@contextmanager
def temp_rclone_config(*args, **kwargs):
    """Context manager: write temp config, yield path, auto-cleanup."""
    path = write_temp_config(*args, **kwargs)
    try:
        yield path
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass
