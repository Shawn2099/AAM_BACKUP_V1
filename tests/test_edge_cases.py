"""Edge case tests — boundary values, unicode, long paths, special chars."""

import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.hashing import compute_md5, verify_checksum
from core.lan_manifest import diff_snapshots
from core.lan_sync import classify_exit_code, decide_lan_result, failed_file_count
from core.cloud_reporter import get_cloud_diff
from core.cloud_preflight import run_cloud_dry_run


@contextmanager
def _mock_rclone_check(diff_content=None, *, exit_code=0):
    """Run `rclone check --combined` for real, minus rclone.

    get_cloud_diff() makes its own temp file with tempfile.mkstemp and
    OS-closes that fd before rclone writes the combined diff, so the content
    is written at the path named by the command's ``--combined`` argument.
    A mocked mkstemp returning fd 0 would close pytest's own stdin (or raise
    EBADF on the second call) and push the call onto its _partial path.
    """
    with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
         patch("core.cloud_reporter.subprocess.run") as mock_run:
        def _side_effect(cmd, *args, **kwargs):
            diff_path = Path(cmd[cmd.index("--combined") + 1])
            if diff_content is not None:
                diff_path.write_text(
                    diff_content, encoding="utf-8", errors="replace", newline=""
                )
            return MagicMock(returncode=exit_code, stdout="", stderr="")

        mock_run.side_effect = _side_effect
        yield mock_run


class TestUnicodePaths:
    def test_diff_snapshots_unicode_keys(self):
        """diff_snapshots handles unicode filenames."""
        before = {"文档/报告.pdf": (100, 1700000000.0)}
        after = {"文档/报告.pdf": (200, 1700000000.0)}
        result = diff_snapshots(before, after)
        assert "文档/报告.pdf" in result["modified"]

    def test_diff_snapshots_emoji_keys(self):
        """diff_snapshots handles emoji filenames."""
        before = {"📁/📄.txt": (100, 1700000000.0)}
        after = {}
        result = diff_snapshots(before, after)
        assert "📁/📄.txt" in result["removed"]

    def test_diff_snapshots_chinese_keys(self):
        """diff_snapshots handles Chinese filenames."""
        before = {"新建文件夹/测试文件.docx": (100, 1700000000.0)}
        after = {"新建文件夹/测试文件.docx": (100, 1700000000.0)}
        result = diff_snapshots(before, after)
        assert "新建文件夹/测试文件.docx" in result["unchanged"]


class TestLongPaths:
    def test_diff_snapshots_long_path(self):
        """diff_snapshots handles very long paths."""
        long_name = "a" * 500
        before = {long_name: (100, 1700000000.0)}
        after = {long_name: (200, 1700000000.0)}
        result = diff_snapshots(before, after)
        assert long_name in result["modified"]

    def test_diff_snapshots_deep_nesting(self):
        """diff_snapshots handles deeply nested paths."""
        deep = "/".join(["dir"] * 100) + "/file.txt"
        before = {deep: (100, 1700000000.0)}
        after = {}
        result = diff_snapshots(before, after)
        assert deep in result["removed"]


class TestSpecialValues:
    def test_diff_snapshots_zero_size(self):
        """Files with zero size are handled."""
        before = {"empty.txt": (0, 1700000000.0)}
        after = {"empty.txt": (0, 1700000000.0)}
        result = diff_snapshots(before, after)
        assert "empty.txt" in result["unchanged"]

    def test_diff_snapshots_negative_size(self):
        """Negative sizes (shouldn't happen but shouldn't crash)."""
        before = {"file.txt": (-1, 1700000000.0)}
        after = {"file.txt": (-1, 1700000000.0)}
        result = diff_snapshots(before, after)
        assert "file.txt" in result["unchanged"]

    def test_diff_snapshots_very_large_size(self):
        """Very large file sizes."""
        before = {"large.bin": (999999999999, 1700000000.0)}
        after = {"large.bin": (999999999999, 1700000000.0)}
        result = diff_snapshots(before, after)
        assert "large.bin" in result["unchanged"]

    def test_diff_snapshots_mtime_zero(self):
        """Zero mtime is handled."""
        before = {"file.txt": (100, 0.0)}
        after = {"file.txt": (100, 0.0)}
        result = diff_snapshots(before, after)
        assert "file.txt" in result["unchanged"]


class TestExitCodeEdgeCases:
    def test_exit_code_zero(self):
        assert classify_exit_code(0) == "LAN_COMPLETE"

    def test_exit_code_255(self):
        """All bits set."""
        assert classify_exit_code(255) == "LAN_FAILED"

    def test_exit_code_negative(self):
        assert classify_exit_code(-1) == "LAN_FAILED"

    def test_exit_code_very_large(self):
        """An implausibly large exit code is classified by its BITMASK, not
        its magnitude: 9999 = 0b10011100001111 → bit 3 (copy errors) set,
        bit 4 (fatal) clear → LAN_PARTIAL. Only bit 4 makes a large code
        LAN_FAILED (e.g. 255 = all bits set)."""
        assert 9999 & 16 == 0 and 9999 & 8 == 8   # bit 3 set, bit 4 clear
        assert classify_exit_code(9999) == "LAN_PARTIAL"

    def test_decide_lan_result_empty_log(self):
        result = decide_lan_result(0, "")
        assert result["status"] == "LAN_SUSPECT"

    def test_decide_lan_result_none_log(self):
        result = decide_lan_result(0, None)
        assert result["status"] == "LAN_SUSPECT"


class TestHashingEdgeCases:
    def test_compute_md5_empty_file(self, tmp_path):
        f = tmp_path / "empty"
        f.write_bytes(b"")
        result = compute_md5(f)
        assert len(result) == 32
        assert result == "d41d8cd98f00b204e9800998ecf8427e"

    def test_compute_md5_single_byte(self, tmp_path):
        f = tmp_path / "single"
        f.write_bytes(b"x")
        result = compute_md5(f)
        assert len(result) == 32

    def test_verify_checksum_case_sensitive(self, tmp_path):
        """MD5 comparison is case-sensitive (hex is lowercase)."""
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello")
        md5 = compute_md5(f)
        # Uppercase should still match (hex comparison)
        assert verify_checksum(f, md5.upper()) is False  # case sensitive

    def test_verify_checksum_partial_match(self, tmp_path):
        """Partial MD5 match → False."""
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello")
        assert verify_checksum(f, "5d41402abc4b") is False


class TestGetCloudDiffEdgeCases:
    def _call_diff(self, **kwargs):
        params = dict(
            source="E:\\",
            bucket="bucket",
            fy_prefix="FY26-27",
            config_path="/tmp/config",
        )
        params.update(kwargs)
        return get_cloud_diff(**params)

    def test_diff_file_with_crlf(self):
        """Diff file with Windows line endings — CR must not leak into paths."""
        content = "+ file1.txt\r\n- file2.txt\r\n"
        with _mock_rclone_check(content):
            result = self._call_diff()
            assert result["added"] == ["file1.txt"]
            assert result["removed"] == ["file2.txt"]

    def test_diff_file_with_unicode_paths(self):
        """Diff file with unicode filenames."""
        content = "+ 文档/报告.pdf\n- 旧文件.docx\n= 🗂/unchanged.txt\n"
        with _mock_rclone_check(content):
            result = self._call_diff()
            assert result["added"] == ["文档/报告.pdf"]
            assert result["removed"] == ["旧文件.docx"]
            assert result["unchanged"] == ["🗂/unchanged.txt"]

    def test_rclone_exit_1_is_not_partial(self):
        """rclone exit 1 (differences found) → NOT partial."""
        with _mock_rclone_check("+ file1.txt\n", exit_code=1):
            result = self._call_diff()
            assert "_partial" not in result
            assert "file1.txt" in result["added"]

    def test_rclone_exit_2_is_partial(self):
        """rclone exit >= 2 (hard failure) → _partial, parsed lines still kept."""
        with _mock_rclone_check("+ file1.txt\n", exit_code=2):
            result = self._call_diff()
            assert result["_partial"] is True
            assert "exited 2" in result["_error"]
            assert "file1.txt" in result["added"]


class TestFailedFileCountEdgeCases:
    def test_summary_with_3digit_thousands(self):
        """Summary with comma-separated thousands."""
        log = "Files : 1,000 1,000 0 0 0 0\nEnded : Mon Jan 01 00:00:00 2026\n"
        result = failed_file_count(log, exit_code=8)
        assert result == 1  # bit3 floor

    def test_summary_extra_columns(self):
        """Summary with extra columns."""
        log = "Files : 10 10 0 0 0 0 99 99\nEnded : Mon Jan 01 00:00:00 2026\n"
        result = failed_file_count(log, exit_code=8)
        assert result == 1

    def test_no_summary_with_bit3(self):
        """No summary but bit 3 set → 1."""
        result = failed_file_count("no summary", exit_code=8)
        assert result == 1

    def test_no_summary_no_bit3(self):
        """No summary, no bit 3 → 0."""
        result = failed_file_count("no summary", exit_code=1)
        assert result == 0
