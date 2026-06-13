"""Tests for pre-backup health checks."""

from collections import namedtuple
from unittest.mock import MagicMock, patch

import pytest

from core.health import (
    HealthError,
    check_binary_exists,
    check_clock_skew,
    check_gcs_key,
    check_source_drive,
    pre_backup_health,
)

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


class TestCheckSourceDrive:
    def test_missing_source_returns_false(self, temp_dir):
        ok, reason = check_source_drive(str(temp_dir / "nonexistent"))
        assert ok is False
        assert "not accessible" in reason.lower()

    def test_empty_directory_returns_false(self, temp_dir):
        empty = temp_dir / "empty"
        empty.mkdir()
        ok, reason = check_source_drive(str(empty))
        assert ok is False
        assert "empty" in reason.lower()

    def test_directory_with_files_returns_true(self, temp_dir):
        (temp_dir / "file.txt").write_text("content")
        ok, reason = check_source_drive(str(temp_dir))
        assert ok is True
        assert reason == ""

    def test_low_disk_space_returns_false(self, temp_dir):
        (temp_dir / "file.txt").write_text("content")
        with patch("shutil.disk_usage") as mock_du:
            mock_du.return_value = DiskUsage(100 * 1024**3, 99.5 * 1024**3, 0.5 * 1024**3)
            ok, reason = check_source_drive(str(temp_dir), min_free_gb=1)
            assert ok is False
            assert "low on space" in reason.lower()

    def test_adequate_space_passes(self, temp_dir):
        (temp_dir / "file.txt").write_text("content")
        with patch("shutil.disk_usage") as mock_du:
            mock_du.return_value = DiskUsage(100 * 1024**3, 50 * 1024**3, 10 * 1024**3)
            ok, _ = check_source_drive(str(temp_dir), min_free_gb=1)
            assert ok is True


class TestCheckBinaryExists:
    def test_python_exists(self):
        assert check_binary_exists("python3") or check_binary_exists("python") is True

    def test_nonexistent_binary(self):
        assert check_binary_exists("this_definitely_does_not_exist_xyzzy") is False


class TestCheckGcsKey:
    def test_existing_non_empty_file(self, temp_dir):
        key = temp_dir / "key.json"
        key.write_text('{"type": "service_account"}')
        ok, reason = check_gcs_key(str(key))
        assert ok is True

    def test_missing_file(self, temp_dir):
        ok, reason = check_gcs_key(str(temp_dir / "missing.json"))
        assert ok is False
        assert "not found" in reason.lower()

    def test_empty_file(self, temp_dir):
        key = temp_dir / "empty.json"
        key.write_text("")
        ok, reason = check_gcs_key(str(key))
        assert ok is False
        assert "empty" in reason.lower()


class TestCheckClockSkew:
    def test_passes_when_google_reachable(self):
        import email.utils
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.getheader.return_value = email.utils.formatdate(usegmt=True)
        mock_conn.getresponse.return_value = mock_resp

        with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn):
            ok, reason = check_clock_skew(max_skew_seconds=999999)
            assert ok is True
            assert reason == ""

    def test_passes_on_network_error(self):
        with patch("core.health.http.client.HTTPSConnection", side_effect=OSError("no network")):
            ok, reason = check_clock_skew()
            assert ok is True
            assert reason == ""

    def test_fails_on_large_skew(self):
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.getheader.return_value = "Mon, 26 May 2026 12:00:00 GMT"
        mock_conn.getresponse.return_value = mock_resp

        with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn):
            ok, reason = check_clock_skew(max_skew_seconds=600)
            assert ok is False
            assert "skew" in reason.lower()

    def test_fails_when_no_date_header(self):
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.getheader.return_value = None
        mock_conn.getresponse.return_value = mock_resp

        with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn):
            ok, reason = check_clock_skew()
            assert ok is False
            assert "Date header" in reason


class TestPreBackupHealth:
    def test_all_checks_pass_cloud_mode(self, temp_dir):
        (temp_dir / "file.txt").write_text("data")
        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", return_value=True),
        ):
            pre_backup_health(str(temp_dir), "cloud")

    def test_source_drive_fails_raises(self):
        with (
            patch("core.health.check_source_drive", return_value=(False, "drive gone")),
            pytest.raises(HealthError, match="drive gone"),
        ):
            pre_backup_health("X:\\", "cloud")

    def test_rclone_missing_in_cloud_mode(self):
        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", return_value=False),
            pytest.raises(HealthError, match="rclone not found"),
        ):
            pre_backup_health("C:\\", "cloud")

    def test_robocopy_missing_in_lan_mode(self):
        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", return_value=False),
            pytest.raises(HealthError, match="robocopy not found"),
        ):
            pre_backup_health("C:\\", "lan")
