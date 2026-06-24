"""Tests for hashing — MD5 checksum computation and verification."""


import pytest

from core.hashing import PENDING_CHECKSUM, compute_md5, verify_checksum


class TestComputeMd5:
    def test_known_content(self, temp_dir):
        f = temp_dir / "test.txt"
        f.write_bytes(b"hello world")
        digest = compute_md5(str(f))
        assert digest == "5eb63bbbe01eeed093cb22bb8f5acdc3"

    def test_empty_file(self, temp_dir):
        f = temp_dir / "empty.txt"
        f.write_bytes(b"")
        digest = compute_md5(str(f))
        assert digest == "d41d8cd98f00b204e9800998ecf8427e"

    def test_binary_content(self, temp_dir):
        f = temp_dir / "binary.bin"
        f.write_bytes(bytes(range(256)))
        digest = compute_md5(str(f))
        assert len(digest) == 32

    def test_accepts_path_object(self, temp_dir):
        f = temp_dir / "pathobj.txt"
        f.write_text("test")
        digest = compute_md5(f)
        assert len(digest) == 32

    def test_file_not_found(self, temp_dir):
        with pytest.raises(FileNotFoundError):
            compute_md5(str(temp_dir / "nonexistent.txt"))


class TestVerifyChecksum:
    def test_matching_checksum(self, temp_dir):
        f = temp_dir / "match.txt"
        f.write_bytes(b"hello world")
        expected = "5eb63bbbe01eeed093cb22bb8f5acdc3"
        assert verify_checksum(str(f), expected) is True

    def test_mismatched_checksum(self, temp_dir):
        f = temp_dir / "mismatch.txt"
        f.write_bytes(b"hello world")
        assert verify_checksum(str(f), "deadbeef" * 4) is False

    def test_pending_checksum_returns_false(self, temp_dir):
        f = temp_dir / "pending.txt"
        f.write_bytes(b"some data")
        assert verify_checksum(str(f), PENDING_CHECKSUM) is False

    def test_pending_is_string_pending(self):
        assert PENDING_CHECKSUM == "pending"

    def test_case_sensitive_checksum(self, temp_dir):
        f = temp_dir / "case.txt"
        f.write_bytes(b"hello world")
        expected = "5eb63bbbe01eeed093cb22bb8f5acdc3"
        assert verify_checksum(str(f), expected.upper()) is False
