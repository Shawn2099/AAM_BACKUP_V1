"""MD5 checksums — compatible with rclone hashsum md5."""

import hashlib
from pathlib import Path

PENDING_CHECKSUM = "pending"


def compute_md5(file_path: str | Path) -> str:
    """Compute MD5 digest for a file using chunked reading (Python 3.10 compatible).

    Returns:
        Hex digest string matching rclone hashsum md5 output.
    """
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            md5.update(chunk)
    return md5.hexdigest()


def verify_checksum(file_path: str | Path, expected: str) -> bool:
    """Verify file checksum matches expected value.

    Returns True if checksum matches. Returns False for PENDING_CHECKSUM
    (no false positives — callers must handle uncatalogued files explicitly).
    """
    if expected == PENDING_CHECKSUM:
        return False
    return compute_md5(file_path) == expected
