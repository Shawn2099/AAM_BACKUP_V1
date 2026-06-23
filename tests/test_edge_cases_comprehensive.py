"""Comprehensive edge case tests — lock files, PID validation, exit code classification."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(scope="session", autouse=True)
def prefect_harness():
    yield


from core.process import read_lock_alive, write_lock, pid_alive
from core.cloud_sync import classify_rclone_exit
from core.lan_sync import classify_exit_code


# ═══════════════════════════════════════════════════════════════════
# Lock File — PID:create_time Format (New)
# ═══════════════════════════════════════════════════════════════════


class TestLockPidCreateTime:
    def test_pid_alive_same_create_time_is_live(self, tmp_path):
        lock = tmp_path / "backup.lock"
        lock.write_text("12345:1700000.123456")
        with patch("core.process._get_create_time", return_value=1700000.123456):
            alive, pid = read_lock_alive(lock)
            assert alive is True
            assert pid == 12345

    def test_pid_alive_different_create_time_is_stale(self, tmp_path):
        """PID was reused by a different process."""
        lock = tmp_path / "backup.lock"
        lock.write_text("12345:1700000.123456")
        with patch("core.process._get_create_time", return_value=2000000.0):
            alive, pid = read_lock_alive(lock)
            assert alive is False
            assert pid == 12345

    def test_pid_dead_is_stale(self, tmp_path):
        lock = tmp_path / "backup.lock"
        lock.write_text("12345:1700000.123456")
        with patch("core.process._get_create_time", return_value=None):
            alive, pid = read_lock_alive(lock)
            assert alive is False
            assert pid == 12345

    def test_create_time_within_tolerance(self, tmp_path):
        """0.1 second tolerance for floating-point differences."""
        lock = tmp_path / "backup.lock"
        lock.write_text("12345:1700000.123456")
        with patch("core.process._get_create_time", return_value=1700000.200000):
            alive, pid = read_lock_alive(lock)
            assert alive is True


# ═══════════════════════════════════════════════════════════════════
# Lock File — Bare PID Format (Legacy)
# ═══════════════════════════════════════════════════════════════════


class TestLockBarePid:
    def test_pid_alive_is_live(self, tmp_path):
        lock = tmp_path / "backup.lock"
        lock.write_text("99999")
        with patch("psutil.pid_exists", return_value=True):
            alive, pid = read_lock_alive(lock)
            assert alive is True
            assert pid == 99999

    def test_pid_dead_is_stale(self, tmp_path):
        lock = tmp_path / "backup.lock"
        lock.write_text("99999")
        with patch("psutil.pid_exists", return_value=False):
            alive, pid = read_lock_alive(lock)
            assert alive is False
            assert pid == 99999


# ═══════════════════════════════════════════════════════════════════
# Lock File — Unreadable / Malformed
# ═══════════════════════════════════════════════════════════════════


class TestLockUnreadable:
    def test_no_lock_file(self, tmp_path):
        lock = tmp_path / "backup.lock"
        alive, pid = read_lock_alive(lock)
        assert alive is False
        assert pid is None

    def test_empty_file(self, tmp_path):
        lock = tmp_path / "backup.lock"
        lock.write_text("")
        alive, pid = read_lock_alive(lock)
        assert alive is False
        assert pid is None

    def test_malformed_content(self, tmp_path):
        lock = tmp_path / "backup.lock"
        lock.write_text("not-a-number")
        alive, pid = read_lock_alive(lock)
        assert alive is False
        assert pid is None

    def test_malformed_colon_format(self, tmp_path):
        lock = tmp_path / "backup.lock"
        lock.write_text("abc:xyz")
        alive, pid = read_lock_alive(lock)
        assert alive is False
        assert pid is None

    def test_permission_error_fails_safe(self, tmp_path):
        """Antivirus locks the file → PermissionError → fail safe (alive=True)."""
        lock = tmp_path / "backup.lock"
        lock.write_text("99999:1000.0")
        with patch.object(Path, "read_text", side_effect=PermissionError("locked")):
            alive, pid = read_lock_alive(lock)
            assert alive is True
            assert pid == -1


# ═══════════════════════════════════════════════════════════════════
# Lock File — Atomic Write
# ═══════════════════════════════════════════════════════════════════


class TestWriteLock:
    def test_atomic_write_creates_lock(self, tmp_path):
        lock = tmp_path / "backup.lock"
        write_lock(lock)
        assert lock.exists()
        content = lock.read_text()
        assert ":" in content
        pid_str, ct_str = content.split(":", 1)
        assert int(pid_str) == os.getpid()

    def test_atomic_write_cleans_up_on_failure(self, tmp_path):
        lock = tmp_path / "backup.lock"
        with patch("tempfile.mkstemp", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                write_lock(lock)
        # No temp files left behind
        assert not any(tmp_path.glob(".backup.lock.*"))

    def test_lock_format_is_pid_colon_ct(self, tmp_path):
        lock = tmp_path / "backup.lock"
        write_lock(lock)
        content = lock.read_text()
        parts = content.split(":")
        assert len(parts) == 2
        assert parts[0].isdigit()
        float(parts[1])  # should not raise


# ═══════════════════════════════════════════════════════════════════
# classify_rclone_exit — All Exit Codes 0–10
# ═══════════════════════════════════════════════════════════════════


class TestClassifyRcloneExit:
    def test_exit_0_complete(self):
        assert classify_rclone_exit(0) == "CLOUD_COMPLETE"

    def test_exit_1_failed(self):
        assert classify_rclone_exit(1) == "CLOUD_FAILED"

    def test_exit_2_failed(self):
        assert classify_rclone_exit(2) == "CLOUD_FAILED"

    def test_exit_3_failed(self):
        assert classify_rclone_exit(3) == "CLOUD_FAILED"

    def test_exit_4_partial(self):
        assert classify_rclone_exit(4) == "CLOUD_PARTIAL"

    def test_exit_5_partial(self):
        assert classify_rclone_exit(5) == "CLOUD_PARTIAL"

    def test_exit_6_failed(self):
        assert classify_rclone_exit(6) == "CLOUD_FAILED"

    def test_exit_7_failed(self):
        assert classify_rclone_exit(7) == "CLOUD_FAILED"

    def test_exit_8_failed(self):
        assert classify_rclone_exit(8) == "CLOUD_FAILED"

    def test_exit_9_no_changes(self):
        assert classify_rclone_exit(9) == "CLOUD_NO_CHANGES_COMPLETE"

    def test_exit_10_partial(self):
        assert classify_rclone_exit(10) == "CLOUD_PARTIAL"

    def test_unknown_code_defaults_to_failed(self):
        assert classify_rclone_exit(99) == "CLOUD_FAILED"

    def test_negative_code_defaults_to_failed(self):
        assert classify_rclone_exit(-1) == "CLOUD_FAILED"


# ═══════════════════════════════════════════════════════════════════
# classify_exit_code — LAN Exit Codes 0–8
# ═══════════════════════════════════════════════════════════════════


class TestClassifyLanExit:
    def test_exit_0_complete(self):
        assert classify_exit_code(0) == "LAN_COMPLETE"

    def test_exit_1_complete(self):
        assert classify_exit_code(1) == "LAN_COMPLETE"

    def test_exit_2_complete(self):
        assert classify_exit_code(2) == "LAN_COMPLETE"

    def test_exit_3_complete(self):
        assert classify_exit_code(3) == "LAN_COMPLETE"

    def test_exit_4_partial(self):
        assert classify_exit_code(4) == "LAN_PARTIAL"

    def test_exit_5_partial(self):
        assert classify_exit_code(5) == "LAN_PARTIAL"

    def test_exit_6_partial(self):
        assert classify_exit_code(6) == "LAN_PARTIAL"

    def test_exit_7_partial(self):
        assert classify_exit_code(7) == "LAN_PARTIAL"

    def test_exit_8_partial(self):
        assert classify_exit_code(8) == "LAN_PARTIAL"

    def test_exit_15_partial(self):
        assert classify_exit_code(15) == "LAN_PARTIAL"

    def test_exit_16_failed(self):
        assert classify_exit_code(16) == "LAN_FAILED"

    def test_exit_32_failed(self):
        assert classify_exit_code(32) == "LAN_FAILED"


# ═══════════════════════════════════════════════════════════════════
# pid_alive helper
# ═══════════════════════════════════════════════════════════════════


class TestPidAlive:
    def test_alive(self):
        with patch("psutil.pid_exists", return_value=True):
            assert pid_alive(123) is True

    def test_dead(self):
        with patch("psutil.pid_exists", return_value=False):
            assert pid_alive(99999) is False
