"""MD5 checksums — compatible with rclone hashsum md5."""

import hashlib
from pathlib import Path

PENDING_CHECKSUM = "pending"


def compute_md5(file_path: str | Path) -> str:
    """Compute MD5 digest for a file using 64KB streaming reads.

    Uses usedforsecurity=False to satisfy FIPS-compliant OpenSSL policies on
    Windows Server 2016 — MD5 is strictly used as an integrity fingerprint.

    Returns:
        Hex digest string matching rclone hashsum md5 output.
    """
    md5_hash = hashlib.md5(usedforsecurity=False)
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()


def verify_checksum(file_path: str | Path, expected: str) -> bool:
    """Verify file checksum matches expected value.

    Returns True if checksum matches. Returns False for PENDING_CHECKSUM
    (no false positives — callers must handle uncatalogued files explicitly).
    """
    if expected == PENDING_CHECKSUM:
        return False
    return compute_md5(file_path) == expected
