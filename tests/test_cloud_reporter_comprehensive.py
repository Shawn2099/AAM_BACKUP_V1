"""Comprehensive tests for cloud_reporter — full coverage of every public function and path."""

import json
import subprocess
from unittest.mock import patch, MagicMock, mock_open

import pytest

from core.cloud_reporter import get_cloud_diff, get_cloud_size, get_cloud_manifest, _base_args


# ── Helpers ────────────────────────────────────────────────────────────────

def _mock_result(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# ── _base_args — shared flags ─────────────────────────────────────────────

class TestBaseArgs:
    def test_config_flag(self):
        args = _base_args("/tmp/rclone.conf")
        assert "--config" in args
        idx = args.index("--config")
        assert args[idx + 1] == "/tmp/rclone.conf"

    def test_gcs_no_check_bucket(self):
        args = _base_args("/tmp/c.conf")
        assert "--gcs-no-check-bucket" in args

    def test_fast_list(self):
        args = _base_args("/tmp/c.conf")
        assert "--fast-list" in args


# ── get_cloud_size — every path ──────────────────────────────────────────

class TestGetCloudSize:
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_exit_0_returns_parsed_json(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(
            0, stdout=json.dumps({"count": 42, "bytes": 12345, "sizeless": "12 KB"})
        )
        result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert result["count"] == 42
        assert result["bytes"] == 12345

    @patch("core.cloud_reporter.resolve_binary", return_value=None)
    @patch("core.cloud_reporter.subprocess.run")
    def test_fallback_to_rclone(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(
            0, stdout=json.dumps({"count": 10, "bytes": 500})
        )
        result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert result["count"] == 10
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "rclone"

    @patch("core.cloud_reporter.resolve_binary", return_value="/custom/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_resolved_binary_used(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(
            0, stdout=json.dumps({"count": 1, "bytes": 100})
        )
        get_cloud_size("bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/custom/rclone"

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_nonzero_exit_logs_warning(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(
            2, stdout=json.dumps({"count": 0, "bytes": 0}), stderr="auth error"
        )
        result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert result["count"] == 0

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_timeout_returns_fallback(self, mock_run, mock_resolve):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=30)
        result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert result["count"] == 0
        assert result["bytes"] == 0
        assert "_error" in result

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_invalid_json_returns_fallback(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(0, stdout="not json")
        result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert result["count"] == 0
        assert "_error" in result

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_size_flag_in_command(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(
            0, stdout=json.dumps({"count": 0, "bytes": 0})
        )
        get_cloud_size("bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert "size" in cmd
        assert "--json" in cmd

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_timeout_passthrough(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(
            0, stdout=json.dumps({"count": 0, "bytes": 0})
        )
        get_cloud_size("bucket", "FY26-27", "/cfg", timeout=60)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 60

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_empty_json_returns_empty_dict(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(0, stdout="{}")
        result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert result == {}


# ── get_cloud_manifest — every path ──────────────────────────────────────

class TestGetCloudManifest:
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_exit_0_returns_files_only(self, mock_run, mock_resolve):
        data = [
            {"Path": "a.txt", "Size": 100, "IsDir": False},
            {"Path": "dir", "IsDir": True},
            {"Path": "b.txt", "Size": 200, "IsDir": False},
        ]
        mock_run.return_value = _mock_result(0, stdout=json.dumps(data))
        result = get_cloud_manifest("bucket", "FY26-27", "/cfg")
        assert len(result) == 2
        assert result[0]["Path"] == "a.txt"
        assert result[1]["Path"] == "b.txt"

    @patch("core.cloud_reporter.resolve_binary", return_value=None)
    @patch("core.cloud_reporter.subprocess.run")
    def test_fallback_to_rclone(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(0, stdout="[]")
        result = get_cloud_manifest("bucket", "FY26-27", "/cfg")
        assert result == []
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "rclone"

    @patch("core.cloud_reporter.resolve_binary", return_value="/custom/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_resolved_binary_used(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(0, stdout="[]")
        get_cloud_manifest("bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/custom/rclone"

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_nonzero_exit_logs_warning_still_parses(self, mock_run, mock_resolve):
        data = [{"Path": "a.txt", "Size": 10, "IsDir": False}]
        mock_run.return_value = _mock_result(1, stdout=json.dumps(data), stderr="partial listing")
        result = get_cloud_manifest("bucket", "FY26-27", "/cfg")
        assert len(result) == 1

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_timeout_returns_empty(self, mock_run, mock_resolve):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        result = get_cloud_manifest("bucket", "FY26-27", "/cfg")
        assert result == []

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_invalid_json_returns_empty(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(0, stdout="not json")
        result = get_cloud_manifest("bucket", "FY26-27", "/cfg")
        assert result == []

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_lsjson_flag(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(0, stdout="[]")
        get_cloud_manifest("bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert "lsjson" in cmd
        assert "-R" in cmd

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_timeout_passthrough(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(0, stdout="[]")
        get_cloud_manifest("bucket", "FY26-27", "/cfg", timeout=600)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 600

    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_empty_json_returns_empty_list(self, mock_run, mock_resolve):
        mock_run.return_value = _mock_result(0, stdout="[]")
        result = get_cloud_manifest("bucket", "FY26-27", "/cfg")
        assert result == []


# ── get_cloud_diff — every path ──────────────────────────────────────────

class TestGetCloudDiffParsing:
    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_parses_added_removed_modified_unchanged(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(0)
        diff_content = "+ new.txt\n- old.txt\n* mod.txt\n= same.txt\n"
        m = mock_open(read_data=diff_content)
        with patch("builtins.open", m):
            result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert "new.txt" in result["added"]
        assert "old.txt" in result["removed"]
        assert "mod.txt" in result["modified"]
        assert "same.txt" in result["unchanged"]

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_empty_diff_file(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(0)
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert result["added"] == []
        assert result["removed"] == []
        assert result["modified"] == []
        assert result["unchanged"] == []


class TestGetCloudDiffExitCodes:
    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_exit_0_no_partial(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(0)
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert "_partial" not in result

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_exit_1_no_partial(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        """Exit 1 (mismatch) still produces valid diff — not partial."""
        mock_run.return_value = _mock_result(1)
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert "_partial" not in result

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_exit_2_sets_partial(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(2, stderr="rclone error")
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert result["_partial"] is True

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_exit_5_sets_partial(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(5, stderr="network error")
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert result["_partial"] is True


class TestGetCloudDiffMissingFile:
    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_file_not_found_returns_empty(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(0)
        m = mock_open()
        m.side_effect = FileNotFoundError("diff file missing")
        with patch("builtins.open", m):
            result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert result["added"] == []
        assert result["removed"] == []


class TestGetCloudDiffCommandFlags:
    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_size_only_flag(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(0)
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert "--size-only" in cmd

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_modify_window_2s(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(0)
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--modify-window")
        assert cmd[idx + 1] == "2s"

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_combined_flag(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(0)
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        assert "--combined" in cmd
        idx = cmd.index("--combined")
        assert cmd[idx + 1] == "/tmp/diff.txt"

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_checkers_4(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(0)
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--checkers")
        assert cmd[idx + 1] == "4"


class TestGetCloudDiffTimeout:
    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_default_timeout_600(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(0)
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 600

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_custom_timeout_passed(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(0)
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            get_cloud_diff("/src", "bucket", "FY26-27", "/cfg", timeout=123)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 123

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_timeout_returns_empty(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=600)
        result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert result["added"] == []
        assert result["removed"] == []
        assert result["modified"] == []
        assert result["unchanged"] == []


class TestGetCloudDiffCleanup:
    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_diff_file_cleaned_up_on_success(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(0)
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        mock_path.return_value.unlink.assert_called_once()

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_diff_file_cleaned_up_on_timeout(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=600)
        get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        mock_path.return_value.unlink.assert_called_once()

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_diff_file_cleaned_up_on_os_error(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.side_effect = OSError("disk full")
        get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        mock_path.return_value.unlink.assert_called_once()

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_unlink_failure_swallowed(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        """If unlink fails in finally, it must not raise."""
        mock_run.return_value = _mock_result(0)
        mock_path.return_value.unlink.side_effect = OSError("still in use")
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert result["added"] == []


class TestGetCloudDiffReturnStructure:
    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_return_keys(self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path):
        mock_run.return_value = _mock_result(0)
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert set(result.keys()) == {"added", "removed", "modified", "unchanged"}

    @patch("core.cloud_reporter.Path")
    @patch("core.cloud_reporter.os.close")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.resolve_binary", return_value="/usr/bin/rclone")
    @patch("core.cloud_reporter.subprocess.run")
    def test_return_keys_on_partial(
        self, mock_run, mock_resolve, mock_mkstemp, mock_close, mock_path
    ):
        mock_run.return_value = _mock_result(2, stderr="error")
        m = mock_open(read_data="")
        with patch("builtins.open", m):
            result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert "_partial" in result
        assert result["_partial"] is True
