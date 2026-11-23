"""Tests for core/hashing.py — compute_md5() and verify_checksum()."""

import hashlib
from pathlib import Path

import pytest

from core.hashing import PENDING_CHECKSUM, compute_md5, verify_checksum


class TestComputeMd5:
    def test_known_content(self, tmp_path):
        """MD5 of known content matches hashlib."""
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello world")
        expected = hashlib.md5(b"hello world", usedforsecurity=False).hexdigest()
        assert compute_md5(f) == expected

    def test_empty_file(self, tmp_path):
        """MD5 of empty file."""
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        expected = hashlib.md5(b"", usedforsecurity=False).hexdigest()
        assert compute_md5(f) == expected

    def test_large_file_streaming(self, tmp_path):
        """File larger than 64KB chunk — streaming read works."""
        f = tmp_path / "large.bin"
        data = b"x" * 200_000  # > 64KB
        f.write_bytes(data)
        expected = hashlib.md5(data, usedforsecurity=False).hexdigest()
        assert compute_md5(f) == expected

    def test_string_path(self, tmp_path):
        """Accepts str path, not just Path."""
        f = tmp_path / "test.txt"
        f.write_bytes(b"test")
        assert compute_md5(str(f)) == compute_md5(f)

    def test_binary_content(self, tmp_path):
        """Binary content with null bytes."""
        f = tmp_path / "binary.bin"
        data = bytes(range(256))
        f.write_bytes(data)
        expected = hashlib.md5(data, usedforsecurity=False).hexdigest()
        assert compute_md5(f) == expected


class TestVerifyChecksum:
    def test_matching_checksum(self, tmp_path):
        """Correct checksum → True."""
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello")
        md5 = compute_md5(f)
        assert verify_checksum(f, md5) is True

    def test_mismatched_checksum(self, tmp_path):
        """Wrong checksum → False."""
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello")
        assert verify_checksum(f, "0" * 32) is False

    def test_pending_checksum_returns_false(self, tmp_path):
        """PENDING_CHECKSUM sentinel → False (no false positives)."""
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello")
        assert verify_checksum(f, PENDING_CHECKSUM) is False

    def test_pending_constant_value(self):
        """PENDING_CHECKSUM is 'pending'."""
        assert PENDING_CHECKSUM == "pending"
