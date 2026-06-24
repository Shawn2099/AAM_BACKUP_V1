"""Tests for cloud_reporter — mock subprocess calls."""

import json
from unittest.mock import patch, MagicMock
import subprocess

from core.cloud_reporter import get_cloud_size, get_cloud_manifest, get_cloud_diff


def _mock_result(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestGetCloudSize:
    @patch("core.cloud_reporter.subprocess.run")
    def test_returns_count_and_bytes(self, mock_run):
        mock_run.return_value = _mock_result(0, stdout=json.dumps({"count": 42, "bytes": 12345}))
        result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert result["count"] == 42
        assert result["bytes"] == 12345

    @patch("core.cloud_reporter.subprocess.run")
    def test_timeout_returns_fallback(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=30)
        result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert result["count"] == 0
        assert "_error" in result

    @patch("core.cloud_reporter.subprocess.run")
    def test_invalid_json_returns_fallback(self, mock_run):
        mock_run.return_value = _mock_result(0, stdout="not json")
        result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert result["count"] == 0


class TestGetCloudManifest:
    @patch("core.cloud_reporter.subprocess.run")
    def test_returns_file_list(self, mock_run):
        data = [{"Path": "a.txt", "Size": 100, "IsDir": False}, {"Path": "dir", "IsDir": True}]
        mock_run.return_value = _mock_result(0, stdout=json.dumps(data))
        result = get_cloud_manifest("bucket", "FY26-27", "/cfg")
        assert len(result) == 1
        assert result[0]["Path"] == "a.txt"

    @patch("core.cloud_reporter.subprocess.run")
    def test_timeout_returns_empty(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=300)
        result = get_cloud_manifest("bucket", "FY26-27", "/cfg")
        assert result == []


class TestGetCloudDiff:
    @patch("core.cloud_reporter.subprocess.run")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.os.close")
    def test_parses_diff_file(self, mock_close, mock_mkstemp, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("builtins.open", MagicMock()):
            with patch("core.cloud_reporter.Path") as mock_path:
                mock_path.return_value.unlink = MagicMock()
                # Mock the file reading
                with patch("core.cloud_reporter.open", create=True) as mock_open:
                    mock_open.return_value.__enter__ = lambda s: iter(["+ new.txt\n", "- old.txt\n", "* mod.txt\n", "= same.txt\n"])
                    mock_open.return_value.__exit__ = MagicMock(return_value=False)
                    result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
                    assert "new.txt" in result["added"]
                    assert "old.txt" in result["removed"]

    @patch("core.cloud_reporter.subprocess.run")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.os.close")
    def test_timeout_returns_empty(self, mock_close, mock_mkstemp, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=600)
        result = get_cloud_diff("/src", "bucket", "FY26-27", "/cfg")
        assert result["added"] == []
        assert result["removed"] == []

    @patch("core.cloud_reporter.subprocess.run")
    @patch("core.cloud_reporter.tempfile.mkstemp", return_value=(1, "/tmp/diff.txt"))
    @patch("core.cloud_reporter.os.close")
    def test_uses_passed_timeout(self, mock_close, mock_mkstemp, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_reporter.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: iter([])
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            with patch("core.cloud_reporter.Path") as mock_path:
                mock_path.return_value.unlink = MagicMock()
                get_cloud_diff("/src", "bucket", "FY26-27", "/cfg", timeout=123)
        assert mock_run.call_args.kwargs["timeout"] == 123
