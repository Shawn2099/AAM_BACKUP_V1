"""Edge case tests — boundary values, unicode, long paths, special chars."""

import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.hashing import compute_md5, verify_checksum
from core.lan_sync import decide_lan_rclone_result
from core.cloud_reporter import get_cloud_diff
from core.cloud_preflight import run_cloud_dry_run
from test_support.fake_tools import SubprocessStyleRunTool


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
         patch("core.cloud_reporter.run_tool", new_callable=SubprocessStyleRunTool) as mock_run:
        def _side_effect(cmd, *args, **kwargs):
            diff_path = Path(cmd[cmd.index("--combined") + 1])
            if diff_content is not None:
                diff_path.write_text(
                    diff_content, encoding="utf-8", errors="replace", newline=""
                )
            return MagicMock(returncode=exit_code, stdout="", stderr="")

        mock_run.side_effect = _side_effect
        yield mock_run








class TestExitCodeEdgeCases:
    def test_exit_code_zero_without_stats_is_suspect(self):
        assert decide_lan_rclone_result(0, "")["status"] == "LAN_SUSPECT"

    def test_exit_code_255(self):
        assert decide_lan_rclone_result(255, "")["status"] == "LAN_FAILED"

    def test_exit_code_negative(self):
        assert decide_lan_rclone_result(-1, "")["status"] == "LAN_FAILED"

    def test_exit_code_very_large(self):
        """An implausibly large exit code is not in the table: LAN_FAILED."""
        assert decide_lan_rclone_result(9999, "")["status"] == "LAN_FAILED"

    def test_decide_lan_result_empty_log(self):
        assert decide_lan_rclone_result(0, "")["status"] == "LAN_SUSPECT"

    def test_decide_lan_result_none_log(self):
        assert decide_lan_rclone_result(0, None)["status"] == "LAN_SUSPECT"


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
            # rclone tunables at the cloud.* config defaults
            timeout=18000, checkers=4, modify_window_seconds=2,
            retries=3, retries_sleep_seconds=10,
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


