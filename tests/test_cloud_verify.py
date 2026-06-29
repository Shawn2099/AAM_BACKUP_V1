"""Tests for cloud_verify — mock subprocess calls."""

import subprocess
from unittest.mock import MagicMock, patch

from core.cloud_verify import verify_cloud_integrity


def _mock_result(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestVerifyCloudIntegrity:
    @patch("core.cloud_verify.subprocess.run")
    def test_exit_0_verified(self, mock_run):
        mock_run.return_value = _mock_result(0)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is True
        assert result["exit_code"] == 0
        assert result["error"] is None

    @patch("core.cloud_verify.subprocess.run")
    def test_exit_1_not_verified(self, mock_run):
        mock_run.return_value = _mock_result(1, stderr="mismatch")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == 1
        assert "mismatch" in result["error"]

    @patch("core.cloud_verify.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=600)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert "Timeout" in result["error"]

    @patch("core.cloud_verify.subprocess.run")
    def test_rclone_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert "rclone not found" in result["error"]

    @patch("core.cloud_verify.subprocess.run")
    def test_exit_2_rclone_error(self, mock_run):
        """Exit 2+ is an rclone error (auth failure, bad config, etc.) — distinct from exit 1 mismatch."""
        mock_run.return_value = _mock_result(2, stderr="FAILED to auth: credentials not found")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == 2
        assert "exit code 2" in result["error"]

    @patch("core.cloud_verify.subprocess.run")
    def test_os_error_returns_error_dict(self, mock_run):
        """OSError (disk/permission error) should be caught and returned cleanly, not raised."""
        mock_run.side_effect = OSError("Permission denied: /mnt/backup")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == -1
        assert "Permission denied" in result["error"]
