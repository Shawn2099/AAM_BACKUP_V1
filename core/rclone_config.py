"""Shared rclone temporary config writer — single source of truth.

Used by cloud_preflight, cloud_sync, and cloud_verify/reporter callers.
"""

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from core import gcs_key


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
    # Sanitize inputs — strip whitespace and validate
    location = location.strip()
    storage_class = storage_class.strip()
    project_number = project_number.strip()
    
    # Validate storage class (GCS valid values)
    valid_storage_classes = {"", "STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"}
    if storage_class and storage_class.upper() not in valid_storage_classes:
        raise ValueError(f"Invalid storage_class: {storage_class!r}. Must be one of {valid_storage_classes}")
    
    if gcs_key.is_protected(gcs_key_path):
        # the key reaches rclone through its environment (temp_rclone_config), never as a file
        key_line = ""
    else:
        key_line = "service_account_file = " + str(Path(gcs_key_path).resolve()).replace("\\", "/") + "\n"
    content = f"""[aam_gcs]
type = google cloud storage
{key_line}project_number = {project_number}
object_acl =
bucket_acl =
bucket_policy_only = true
location = {location}
storage_class = {storage_class}
"""
    fd, cfg_path = tempfile.mkstemp(suffix=".conf", prefix="rclone_")
    os.close(fd)
    Path(cfg_path).write_text(content, encoding="utf-8")
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass
    return cfg_path


@contextmanager
def temp_rclone_config(*args, **kwargs):
    """Context manager: write temp config, yield path, auto-cleanup."""
    key_path = args[0] if args else kwargs.get("gcs_key_path", "")
    credentials = gcs_key.read_json(key_path) if gcs_key.is_protected(key_path) else None
    path = write_temp_config(*args, **kwargs)
    previous = os.environ.get(gcs_key.RCLONE_ENV)
    if credentials is not None:
        os.environ[gcs_key.RCLONE_ENV] = credentials      # in memory only; removed below
    try:
        yield path
    finally:
        if credentials is not None:
            if previous is None:
                os.environ.pop(gcs_key.RCLONE_ENV, None)
            else:
                os.environ[gcs_key.RCLONE_ENV] = previous
        try:
            Path(path).unlink()
        except OSError:
            pass
