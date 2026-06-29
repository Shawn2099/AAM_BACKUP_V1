"""Comprehensive tests for cloud_preflight — full coverage of every public function and path."""

import subprocess
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from core.cloud_preflight import run_cloud_dry_run

# ── Helpers ────────────────────────────────────────────────────────────────

def _mock_result(returncode=0, stdout="[]", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


@contextmanager
def _mock_cfg(*args, **kwargs):
    yield "/tmp/rclone_test.conf"


_CALL_ARGS = ("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")


def _make_accessible_path_mock():
    """Returns a mock Path instance that looks like an accessible, non-empty dir."""
    mock_path_inst = MagicMock()
    mock_path_inst.exists.return_value = True
    mock_path_inst.iterdir.return_value = iter([MagicMock()])
    return mock_path_inst


# ── Probe A: Source drive checks ──────────────────────────────────────────

class TestProbeASourceNotExists:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_source_not_exists_returns_error(self, mock_cfg, mock_run):
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = False
            mock_path_cls.return_value = mock_path_inst
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "not accessible" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_source_not_exists_does_not_call_rclone(self, mock_cfg, mock_run):
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = False
            mock_path_cls.return_value = mock_path_inst
            run_cloud_dry_run(*_CALL_ARGS)
        mock_run.assert_not_called()


class TestProbeASourceOSError:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_iterdir_oserror_returns_error(self, mock_cfg, mock_run):
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = True
            mock_path_inst.iterdir.side_effect = OSError("Permission denied")
            mock_path_cls.return_value = mock_path_inst
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "read error" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_iterdir_oserror_does_not_call_rclone(self, mock_cfg, mock_run):
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = True
            mock_path_inst.iterdir.side_effect = OSError("Permission denied")
            mock_path_cls.return_value = mock_path_inst
            run_cloud_dry_run(*_CALL_ARGS)
        mock_run.assert_not_called()


class TestProbeAEmptySource:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_empty_source_proceeds_to_probe_b(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_inst = MagicMock()
            mock_path_inst.exists.return_value = True
            mock_path_inst.iterdir.return_value = iter([])
            mock_path_cls.return_value = mock_path_inst
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is True
        mock_run.assert_called_once()

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_accessible_source_proceeds_to_probe_b(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is True
        mock_run.assert_called_once()


# ── Probe B: GCS rclone probe ────────────────────────────────────────────

class TestProbeBExitCodes:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_exit_0_returns_ok(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is True
        assert result["exit_code"] == 0
        assert result["error"] is None

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_exit_nonzero_returns_error(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(5, stderr="AccessDenied")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == 5
        assert "AccessDenied" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_exit_3_bucket_not_found(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(3, stderr="bucket not found")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == 3
        assert "bucket not found" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_exit_1_auth_failure(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(1, stderr="unauthorized")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == 1


class TestProbeBStderr:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_stderr_empty_on_failure_shows_no_stderr(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(5, stderr="")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert "no stderr" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_stderr_none_on_failure_shows_no_stderr(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(5, stderr=None)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert "no stderr" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_stderr_present_in_error(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(2, stderr="connection refused")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert "connection refused" in result["error"]


class TestProbeBExceptions:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_timeout_expired(self, mock_cfg, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=30)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "Timeout" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_timeout_message_includes_seconds(self, mock_cfg, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=45)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS, timeout=45)
        assert "45s" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_file_not_found(self, mock_cfg, mock_run):
        mock_run.side_effect = FileNotFoundError("rclone missing")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "rclone not found" in result["error"]

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_os_error(self, mock_cfg, mock_run):
        mock_run.side_effect = OSError("exec format error")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert result["error"] is not None


# ── Command shape assertions ──────────────────────────────────────────────

class TestCommandShape:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_uses_lsjson_not_check(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        assert "lsjson" in cmd
        assert "check" not in cmd

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_max_depth_0(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--max-depth")
        assert cmd[idx + 1] == "0"

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_no_gcs_no_check_bucket(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        assert "--gcs-no-check-bucket" not in cmd

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_retries_2(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--retries")
        assert cmd[idx + 1] == "2"

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_retries_sleep_5s(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        idx = cmd.index("--retries-sleep")
        assert cmd[idx + 1] == "5s"

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_config_flag_present(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        assert "--config" in cmd
        idx = cmd.index("--config")
        assert cmd[idx + 1] == "/tmp/rclone_test.conf"

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_no_one_way_flag(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        cmd = mock_run.call_args[0][0]
        assert "--one-way" not in cmd

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_source_path_not_in_cmd(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run("/src", "bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        cmd = mock_run.call_args[0][0]
        assert "/src" not in cmd

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_dest_includes_bucket_and_prefix(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run("/src", "my-bucket", "FY26-27", "/key.json", "123", "COLDLINE")
        cmd = mock_run.call_args[0][0]
        assert any("my-bucket/FY26-27" in arg for arg in cmd)


# ── Return dict contract ──────────────────────────────────────────────────

class TestReturnContract:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_success_has_required_keys(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert set(result.keys()) == {"ok", "exit_code", "error"}

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_failure_has_required_keys(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(5, stderr="auth error")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert set(result.keys()) == {"ok", "exit_code", "error"}

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_no_matched_key(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            result = run_cloud_dry_run(*_CALL_ARGS)
        assert "matched" not in result


# ── Timeout passthrough ──────────────────────────────────────────────────

class TestTimeoutPassthrough:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_default_timeout_30(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 30

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_custom_timeout_passed(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS, timeout=45)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 45


# ── Config cleanup ────────────────────────────────────────────────────────

class TestConfigCleanup:
    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_config_context_manager_called(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(0)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        mock_cfg.assert_called_once()

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_config_cleaned_up_on_rclone_failure(self, mock_cfg, mock_run):
        mock_run.return_value = _mock_result(5, stderr="fail")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        mock_cfg.assert_called_once()

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_config_cleaned_up_on_timeout(self, mock_cfg, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=30)
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        mock_cfg.assert_called_once()

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_config_cleaned_up_on_file_not_found(self, mock_cfg, mock_run):
        mock_run.side_effect = FileNotFoundError("rclone missing")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        mock_cfg.assert_called_once()

    @patch("core.cloud_preflight.subprocess.run")
    @patch("core.cloud_preflight.temp_rclone_config", side_effect=_mock_cfg)
    def test_config_cleaned_up_on_os_error(self, mock_cfg, mock_run):
        mock_run.side_effect = OSError("io error")
        with patch("core.cloud_preflight.Path") as mock_path_cls:
            mock_path_cls.return_value = _make_accessible_path_mock()
            run_cloud_dry_run(*_CALL_ARGS)
        mock_cfg.assert_called_once()
