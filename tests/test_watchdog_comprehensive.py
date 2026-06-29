"""Comprehensive tests for watchdog.py — PID checks, backup detection, service management, main loop."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope="session", autouse=True)
def prefect_harness():
    yield


import watchdog

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def temp_lock(tmp_path):
    return tmp_path / "backup.lock"


@pytest.fixture
def mock_sleep():
    with patch("watchdog.time.sleep") as m:
        yield m


@pytest.fixture
def mock_psutil():
    with patch("psutil.process_iter") as m:
        yield m


# ═══════════════════════════════════════════════════════════════════
# _pid_is_alive
# ═══════════════════════════════════════════════════════════════════


class TestPidIsAlive:
    def test_alive(self):
        with patch("psutil.pid_exists", return_value=True):
            assert watchdog._pid_is_alive(123) is True

    def test_dead(self):
        with patch("psutil.pid_exists", return_value=False):
            assert watchdog._pid_is_alive(99999) is False


# ═══════════════════════════════════════════════════════════════════
# _transfer_process_running
# ═══════════════════════════════════════════════════════════════════


class TestTransferProcessRunning:
    def test_rclone_found(self):
        mock_proc = MagicMock()
        mock_proc.info = {"name": "rclone.exe"}
        with patch("psutil.process_iter", return_value=[mock_proc]):
            assert watchdog._transfer_process_running() is True

    def test_robocopy_found(self):
        mock_proc = MagicMock()
        mock_proc.info = {"name": "robocopy.exe"}
        with patch("psutil.process_iter", return_value=[mock_proc]):
            assert watchdog._transfer_process_running() is True

    def test_none_found(self):
        mock_proc = MagicMock()
        mock_proc.info = {"name": "explorer.exe"}
        with patch("psutil.process_iter", return_value=[mock_proc]):
            assert watchdog._transfer_process_running() is False

    def test_psutil_raises_exception(self):
        with patch("psutil.process_iter", side_effect=Exception("access denied")):
            assert watchdog._transfer_process_running() is False

    def test_process_name_is_none(self):
        mock_proc = MagicMock()
        mock_proc.info = {"name": None}
        with patch("psutil.process_iter", return_value=[mock_proc]):
            assert watchdog._transfer_process_running() is False

    def test_mixed_case_names(self):
        mock_proc = MagicMock()
        mock_proc.info = {"name": "Rclone.EXE"}
        with patch("psutil.process_iter", return_value=[mock_proc]):
            assert watchdog._transfer_process_running() is True

    def test_empty_process_list(self):
        with patch("psutil.process_iter", return_value=[]):
            assert watchdog._transfer_process_running() is False


# ═══════════════════════════════════════════════════════════════════
# _is_backup_running
# ═══════════════════════════════════════════════════════════════════


class TestIsBackupRunning:
    def test_no_lock_returns_false(self, temp_lock):
        with patch("watchdog.BACKUP_LOCK_PATH", temp_lock):
            assert watchdog._is_backup_running() is False

    def test_lock_alive_pid_returns_true(self, temp_lock):
        temp_lock.write_text("999999:1000000.0")
        with patch("watchdog.BACKUP_LOCK_PATH", temp_lock), \
             patch("core.process._get_create_time", return_value=1000000.0):
            assert watchdog._is_backup_running() is True

    def test_lock_dead_pid_removes_lock(self, temp_lock):
        temp_lock.write_text("999999:1000000.0")
        with patch("watchdog.BACKUP_LOCK_PATH", temp_lock), \
             patch("core.process._get_create_time", return_value=None):
            assert watchdog._is_backup_running() is False
            assert not temp_lock.exists()

    def test_lock_pid_reused_removes_lock(self, temp_lock):
        temp_lock.write_text("999999:1000000.0")
        # PID exists but with different create_time — reused
        with patch("watchdog.BACKUP_LOCK_PATH", temp_lock), \
             patch("core.process._get_create_time", return_value=2000000.0):
            assert watchdog._is_backup_running() is False
            assert not temp_lock.exists()

    def test_lock_bare_pid_alive(self, temp_lock):
        """Legacy format: bare PID (no create_time)."""
        temp_lock.write_text("999999")
        with patch("watchdog.BACKUP_LOCK_PATH", temp_lock), \
             patch("core.process.read_lock_alive", return_value=(True, 999999)):
            assert watchdog._is_backup_running() is True

    def test_lock_bare_pid_dead(self, temp_lock):
        """Legacy format: bare PID — dead PID."""
        temp_lock.write_text("999999")
        with patch("watchdog.BACKUP_LOCK_PATH", temp_lock), \
             patch("core.process.read_lock_alive", return_value=(False, 999999)):
            assert watchdog._is_backup_running() is False

    def test_lock_oserror_returns_false(self, temp_lock):
        temp_lock.write_text("999999:1000000.0")
        with patch("watchdog.BACKUP_LOCK_PATH", temp_lock), \
             patch("core.process.read_lock_alive", side_effect=OSError("permission denied")):
            assert watchdog._is_backup_running() is False


# ═══════════════════════════════════════════════════════════════════
# _service_is_running
# ═══════════════════════════════════════════════════════════════════


class TestServiceIsRunning:
    def test_running_returns_true(self):
        mock_result = MagicMock()
        mock_result.stdout = "STATE : 4  RUNNING"
        with patch("watchdog.subprocess.run", return_value=mock_result):
            assert watchdog._service_is_running("AamPrefectServer") is True

    def test_start_pending_returns_false(self):
        mock_result = MagicMock()
        mock_result.stdout = "STATE : 2  START_PENDING"
        with patch("watchdog.subprocess.run", return_value=mock_result):
            assert watchdog._service_is_running("AamPrefectServer") is False

    def test_exception_returns_false(self):
        with patch("watchdog.subprocess.run", side_effect=Exception("error")):
            assert watchdog._service_is_running("AamPrefectServer") is False

    def test_timeout_returns_false(self):
        with patch("watchdog.subprocess.run", side_effect=subprocess.TimeoutExpired("sc", 5)):
            assert watchdog._service_is_running("AamPrefectServer") is False


# ═══════════════════════════════════════════════════════════════════
# _stop_service
# ═══════════════════════════════════════════════════════════════════


class TestStopService:
    def test_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("watchdog.subprocess.run", return_value=mock_result):
            watchdog._stop_service("AamPrefectServer")  # should not raise

    def test_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error"
        with patch("watchdog.subprocess.run", return_value=mock_result):
            watchdog._stop_service("AamPrefectServer")  # should not raise

    def test_timeout(self):
        with patch("watchdog.subprocess.run", side_effect=subprocess.TimeoutExpired("sc", 30)):
            watchdog._stop_service("AamPrefectServer")  # should not raise

    def test_exception(self):
        with patch("watchdog.subprocess.run", side_effect=Exception("unexpected")):
            watchdog._stop_service("AamPrefectServer")  # should not raise


# ═══════════════════════════════════════════════════════════════════
# _check_health
# ═══════════════════════════════════════════════════════════════════


class TestCheckHealth:
    def test_http_200_returns_true(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.get", return_value=mock_resp):
            assert watchdog._check_health() is True

    def test_http_503_returns_false(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch("httpx.get", return_value=mock_resp):
            assert watchdog._check_health() is False

    def test_exception_returns_false(self):
        with patch("httpx.get", side_effect=Exception("connection error")):
            assert watchdog._check_health() is False

    def test_connect_error_returns_false(self):
        with patch("httpx.get", side_effect=Exception("connect error")):
            assert watchdog._check_health() is False


# ═══════════════════════════════════════════════════════════════════
# Main Loop Integration
# ═══════════════════════════════════════════════════════════════════


class TestMainLoop:
    def _run_loop(self, iterations=1):
        """Helper: run watchdog main loop for N iterations then break."""
        with patch("watchdog.time.sleep") as mock_sleep:
            call_count = 0
            def sleep_side_effect(*args):
                nonlocal call_count
                call_count += 1
                if call_count >= iterations:
                    raise KeyboardInterrupt("end")
            mock_sleep.side_effect = sleep_side_effect
            with patch("watchdog._resolve_paths"):
                try:
                    watchdog.main()
                except KeyboardInterrupt:
                    pass
            return mock_sleep

    def test_healthy_resets_counters(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.get", return_value=mock_resp):
            mock_sleep = self._run_loop(1)
            assert mock_sleep.call_args_list[0][0][0] == watchdog.CHECK_INTERVAL_SECONDS

    def test_unhealthy_increments_failure(self):
        with patch("httpx.get", side_effect=Exception("dead")), \
             patch("watchdog._is_backup_running", return_value=False), \
             patch("watchdog._transfer_process_running", return_value=False), \
             patch("watchdog._service_is_running", return_value=True), \
             patch("watchdog._stop_service"):
            mock_sleep = self._run_loop(watchdog.FAILURE_THRESHOLD)
            # After threshold, a stop should be issued
            assert mock_sleep.call_count >= watchdog.FAILURE_THRESHOLD

    def test_threshold_transferring_defers(self):
        with patch("httpx.get", side_effect=Exception("dead")), \
             patch("watchdog._is_backup_running", return_value=False), \
             patch("watchdog._transfer_process_running", return_value=True):
            mock_sleep = self._run_loop(watchdog.FAILURE_THRESHOLD)
            # Last sleep should be BACKUP_WAIT_INTERVAL
            assert mock_sleep.call_args_list[-1][0][0] == watchdog.BACKUP_WAIT_INTERVAL

    def test_threshold_lock_held_defers(self):
        with patch("httpx.get", side_effect=Exception("dead")), \
             patch("watchdog._is_backup_running", return_value=True), \
             patch("watchdog._transfer_process_running", return_value=False):
            mock_sleep = self._run_loop(watchdog.FAILURE_THRESHOLD)
            assert mock_sleep.call_args_list[-1][0][0] == watchdog.BACKUP_WAIT_INTERVAL

    def test_no_backup_restarts(self):
        with patch("httpx.get", side_effect=Exception("dead")), \
             patch("watchdog._is_backup_running", return_value=False), \
             patch("watchdog._transfer_process_running", return_value=False), \
             patch("watchdog._service_is_running", return_value=True), \
             patch("watchdog._stop_service") as mock_stop:
            self._run_loop(watchdog.FAILURE_THRESHOLD)
            mock_stop.assert_called_once()

    def test_transfer_deferral_cap_forces_restart(self):
        """After MAX_TRANSFER_DEFERRALS, force restart."""
        mock_lock = MagicMock()
        with patch("httpx.get", side_effect=Exception("dead")), \
             patch("watchdog._is_backup_running", return_value=False), \
             patch("watchdog._transfer_process_running", return_value=True), \
             patch("watchdog._service_is_running", return_value=True), \
             patch("watchdog._stop_service"), \
             patch("watchdog.BACKUP_LOCK_PATH", mock_lock):
            total_iters = watchdog.FAILURE_THRESHOLD + watchdog.MAX_TRANSFER_DEFERRALS
            mock_sleep = self._run_loop(total_iters)
            mock_lock.unlink.assert_called()

    def test_lock_deferral_cap_forces_restart(self):
        """After MAX_DEFERRALS with lock held, force restart."""
        mock_lock = MagicMock()
        with patch("httpx.get", side_effect=Exception("dead")), \
             patch("watchdog._is_backup_running", return_value=True), \
             patch("watchdog._transfer_process_running", return_value=False), \
             patch("watchdog._service_is_running", return_value=True), \
             patch("watchdog._stop_service"), \
             patch("watchdog.BACKUP_LOCK_PATH", mock_lock):
            total_iters = watchdog.FAILURE_THRESHOLD + watchdog.MAX_DEFERRALS
            mock_sleep = self._run_loop(total_iters)
            mock_lock.unlink.assert_called()

    def test_service_not_running_skips_restart(self):
        with patch("httpx.get", side_effect=Exception("dead")), \
             patch("watchdog._is_backup_running", return_value=False), \
             patch("watchdog._transfer_process_running", return_value=False), \
             patch("watchdog._service_is_running", return_value=False), \
             patch("watchdog._stop_service") as mock_stop:
            mock_sleep = self._run_loop(watchdog.FAILURE_THRESHOLD)
            mock_stop.assert_not_called()
