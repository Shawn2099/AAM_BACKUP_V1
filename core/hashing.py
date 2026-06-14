"""MD5 checksums — compatible with rclone hashsum md5."""

import hashlib
from pathlib import Path

PENDING_CHECKSUM = "pending"


def compute_md5(file_path: str | Path) -> str:
    """Compute MD5 digest for a file using streaming.

    Returns:
        Hex digest string matching rclone hashsum md5 output.
    """
    with open(file_path, "rb") as f:
        if hasattr(hashlib, "file_digest"):
            return hashlib.file_digest(f, "md5").hexdigest()
        # Fallback for Python < 3.11
        h = hashlib.md5()
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
        return h.hexdigest()


def verify_checksum(file_path: str | Path, expected: str) -> bool:
    """Verify file checksum matches expected value.

    Returns True if checksum matches. Returns False for PENDING_CHECKSUM
    (no false positives — callers must handle uncatalogued files explicitly).
    """
    if expected == PENDING_CHECKSUM:
        return False
    return compute_md5(file_path) == expected
