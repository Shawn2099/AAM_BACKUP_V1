"""Tests for cloud_verify — mock subprocess calls."""

import subprocess
from unittest.mock import MagicMock, patch

from core.cloud_verify import verify_cloud_integrity as _verify_cloud_integrity
import functools

from models.config import CloudConfig as _CloudConfig
from test_support.fake_tools import SubprocessStyleRunTool

# rclone runs through run_tool (plan 5.1). Required tunables at the production
# defaults (cloud.* in config); a keyword passed by a test still wins.
verify_cloud_integrity = functools.partial(
    _verify_cloud_integrity,
    timeout=_CloudConfig.model_fields["verify_timeout_seconds"].default,
    checkers=_CloudConfig.model_fields["checkers"].default,
    modify_window_seconds=_CloudConfig.model_fields["modify_window_seconds"].default,
)

# Realistic rclone v1.74.2 stderr for a completed clean check
# (--one-way --size-only): the completion summary VERIFIED requires.
CLEAN_STDERR = (
    "2026/09/06 18:18:48 NOTICE: Local file system at //?/C:/dst: 0 differences found\n"
    "2026/09/06 18:18:48 NOTICE: Local file system at //?/C:/dst: 1 matching files\n"
)


def _mock_result(returncode=0, stdout="", stderr=CLEAN_STDERR):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestVerifyCloudIntegrity:
    @patch("core.cloud_verify.run_tool", new_callable=SubprocessStyleRunTool)
    def test_exit_0_verified(self, mock_run):
        mock_run.return_value = _mock_result(0)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is True
        assert result["exit_code"] == 0
        assert result["error"] is None

    @patch("core.cloud_verify.run_tool", new_callable=SubprocessStyleRunTool)
    def test_exit_1_not_verified(self, mock_run):
        mock_run.return_value = _mock_result(1, stderr="mismatch\n1 differences found")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == 1
        assert "mismatch" in result["error"]

    @patch("core.cloud_verify.run_tool", new_callable=SubprocessStyleRunTool)
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=600)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert "Timeout" in result["error"]

    @patch("core.cloud_verify.run_tool", new_callable=SubprocessStyleRunTool)
    def test_rclone_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert "rclone not found" in result["error"]

    @patch("core.cloud_verify.run_tool", new_callable=SubprocessStyleRunTool)
    def test_exit_2_rclone_error(self, mock_run):
        """Exit 2+ is an rclone error (auth failure, bad config, etc.) — distinct from exit 1 mismatch."""
        mock_run.return_value = _mock_result(2, stderr="FAILED to auth: credentials not found")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == 2
        assert "exit code 2" in result["error"]

    @patch("core.cloud_verify.run_tool", new_callable=SubprocessStyleRunTool)
    def test_os_error_returns_error_dict(self, mock_run):
        """OSError (disk/permission error) should be caught and returned cleanly, not raised."""
        mock_run.side_effect = OSError("Permission denied: /mnt/backup")
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["exit_code"] == -1
        assert "Permission denied" in result["error"]


class TestVerifyOnToolRunner:
    """plan 5.1: the check runs through run_tool; its flags come from config."""

    def test_flags_come_from_the_parameters(self):
        from test_support.fake_tools import fake_run_tool
        with fake_run_tool("core.cloud_verify", exit_code=0, stderr=CLEAN_STDERR) as calls:
            result = _verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg",
                                             timeout=123, checkers=8, modify_window_seconds=5)
        assert result["verified"] is True
        cmd = calls[0]["argv"]
        assert cmd[cmd.index("--checkers") + 1] == "8"
        assert cmd[cmd.index("--modify-window") + 1] == "5s"
        assert calls[0]["timeout_s"] == 123

    def test_check_output_is_kept_in_the_run_folder(self, tmp_path):
        from pathlib import Path

        from core.run_artefacts import RunArtefacts
        from test_support.fake_tools import fake_run_tool
        art = RunArtefacts(str(tmp_path / "run_logs"), "run-v")
        with fake_run_tool("core.cloud_verify", exit_code=1,
                           stderr="ERROR : a.txt: sizes differ\n1 differences found\n"):
            result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg", artefacts=art)
        assert result["verified"] is False and result["differences"] == 1
        kept = list(Path(art.dir).glob("*rclone-verify.stderr.txt"))
        assert kept and "sizes differ" in kept[0].read_text(encoding="utf-8")

    def test_tunables_have_no_hidden_defaults(self):
        import pytest
        with pytest.raises(TypeError):
            _verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
