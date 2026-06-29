"""Comprehensive tests for core/health.py — pre-backup health checks."""

from __future__ import annotations

from datetime import UTC
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


@pytest.fixture(autouse=True, scope="session")
def prefect_harness():
    """Override session-scoped fixture from conftest to avoid Prefect server startup."""
    yield


# ── check_source_drive ───────────────────────────────────────────────────────


class TestCheckSourceDrive:
    def test_passes_when_source_exists_and_has_files(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        ok, reason = check_source_drive(str(tmp_path))
        assert ok is True
        assert reason == ""

    def test_fails_when_source_not_accessible(self):
        ok, reason = check_source_drive("/nonexistent/path/abc123")
        assert ok is False
        assert "not accessible" in reason

    def test_fails_when_source_empty(self, tmp_path):
        ok, reason = check_source_drive(str(tmp_path))
        assert ok is False
        assert "appears empty" in reason

    def test_fails_when_permission_denied(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        with patch("core.health.Path") as MockPath:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.iterdir.side_effect = PermissionError("denied")
            MockPath.return_value = mock_path
            ok, reason = check_source_drive(str(tmp_path))
        assert ok is False
        assert "permission denied" in reason.lower()

    def test_fails_when_os_error(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        with patch("core.health.Path") as MockPath:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.iterdir.side_effect = OSError("disk error")
            MockPath.return_value = mock_path
            ok, reason = check_source_drive(str(tmp_path))
        assert ok is False
        assert "disk error" in reason

    def test_fails_when_low_disk_space(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        mock_usage = MagicMock()
        mock_usage.free = 500 * 1024**3  # 500 GB
        with patch("core.health.shutil.disk_usage", return_value=mock_usage):
            ok, reason = check_source_drive(str(tmp_path), min_free_gb=1000)
        assert ok is False
        assert "critically low" in reason

    def test_passes_when_disk_space_ok(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        mock_usage = MagicMock()
        mock_usage.free = 2000 * 1024**3  # 2000 GB
        with patch("core.health.shutil.disk_usage", return_value=mock_usage):
            ok, reason = check_source_drive(str(tmp_path), min_free_gb=1)
        assert ok is True

    def test_passes_when_disk_usage_oserror(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        with patch("core.health.shutil.disk_usage", side_effect=OSError("no stat")):
            ok, reason = check_source_drive(str(tmp_path))
        assert ok is True


# ── check_binary_exists ──────────────────────────────────────────────────────


class TestCheckBinaryExists:
    def test_returns_true_when_binary_found(self):
        with patch("core.health.resolve_binary", return_value="/usr/bin/rclone"):
            assert check_binary_exists("rclone") is True

    def test_returns_false_when_binary_not_found(self):
        with patch("core.health.resolve_binary", return_value=None):
            assert check_binary_exists("nonexistent_binary_xyz") is False


# ── check_gcs_key ────────────────────────────────────────────────────────────


class TestCheckGcsKey:
    def test_passes_with_valid_key(self, tmp_path):
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')
        ok, reason = check_gcs_key(str(key))
        assert ok is True
        assert reason == ""

    def test_fails_when_key_not_found(self):
        ok, reason = check_gcs_key("/nonexistent/key.json")
        assert ok is False
        assert "not found" in reason

    def test_fails_when_key_empty(self, tmp_path):
        key = tmp_path / "empty.json"
        key.write_text("")
        ok, reason = check_gcs_key(str(key))
        assert ok is False
        assert "empty" in reason


# ── check_clock_skew ─────────────────────────────────────────────────────────


class TestCheckClockSkew:
    def _make_mock_response(self, date_str: str):
        mock_resp = MagicMock()
        mock_resp.getheader.return_value = date_str
        return mock_resp

    def test_passes_when_skew_small(self):
        from datetime import datetime

        now = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
        google_time_str = "Tue, 24 Jun 2026 11:59:30 GMT"
        with patch("core.health.pendulum") as mock_pendulum:
            mock_pendulum.now.return_value = now
            mock_conn = MagicMock()
            mock_conn.getresponse.return_value = self._make_mock_response(google_time_str)
            with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn):
                ok, reason = check_clock_skew()
        assert ok is True
        assert reason == ""

    def test_fails_when_skew_large(self):
        from datetime import datetime

        # Local time is 20 minutes ahead of Google
        local = datetime(2026, 6, 24, 12, 20, 0, tzinfo=UTC)
        google_time_str = "Tue, 24 Jun 2026 12:00:00 GMT"
        with patch("core.health.pendulum") as mock_pendulum:
            mock_pendulum.now.return_value = local
            mock_conn = MagicMock()
            mock_conn.getresponse.return_value = self._make_mock_response(google_time_str)
            with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn):
                ok, reason = check_clock_skew(max_skew_seconds=600)
        assert ok is False
        assert "clock skew" in reason.lower()

    def test_passes_on_network_failure(self):
        with patch("core.health.http.client.HTTPSConnection", side_effect=OSError("timeout")):
            ok, reason = check_clock_skew()
        assert ok is True
        assert reason == ""

    def test_passes_on_date_header_missing(self):
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.getheader.return_value = None
        mock_conn.getresponse.return_value = mock_resp
        with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn):
            ok, reason = check_clock_skew()
        assert ok is False
        assert "Date header" in reason

    def test_passes_on_date_parse_error(self):
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.getheader.return_value = "not-a-date"
        mock_conn.getresponse.return_value = mock_resp
        with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn):
            ok, reason = check_clock_skew()
        assert ok is True
        assert reason == ""

    def test_uses_custom_timeout(self):
        from datetime import datetime

        now = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)
        with patch("core.health.pendulum") as mock_pendulum:
            mock_pendulum.now.return_value = now
            mock_conn = MagicMock()
            mock_conn.getresponse.return_value = self._make_mock_response(
                "Tue, 24 Jun 2026 12:00:00 GMT"
            )
            with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn) as mock_cls:
                check_clock_skew(connection_timeout=5)
            mock_cls.assert_called_once_with("www.googleapis.com", timeout=5)


# ── pre_backup_health ────────────────────────────────────────────────────────


class TestPreBackupHealth:
    def test_cloud_mode_all_pass(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", return_value=True),
            patch("core.health.check_gcs_key", return_value=(True, "")),
            patch("core.health.check_clock_skew", return_value=(True, "")),
        ):
            pre_backup_health(
                source_path=str(tmp_path),
                mode="cloud",
                gcs_key_path="/some/key.json",
            )

    def test_lan_mode_all_pass(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", return_value=True),
        ):
            pre_backup_health(
                source_path=str(tmp_path),
                mode="lan",
            )

    def test_all_mode_checks_both(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", return_value=True),
            patch("core.health.check_gcs_key", return_value=(True, "")),
            patch("core.health.check_clock_skew", return_value=(True, "")),
        ):
            pre_backup_health(
                source_path=str(tmp_path),
                mode="all",
                gcs_key_path="/some/key.json",
            )

    def test_invalid_mode_raises(self):
        with pytest.raises(HealthError, match="Invalid mode"):
            pre_backup_health(source_path="/tmp", mode="invalid")

    def test_cloud_fails_when_rclone_missing(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", return_value=False),
        ):
            with pytest.raises(HealthError, match="rclone not found"):
                pre_backup_health(source_path=str(tmp_path), mode="cloud")

    def test_lan_fails_when_robocopy_missing(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", return_value=False),
        ):
            with pytest.raises(HealthError, match="robocopy not found"):
                pre_backup_health(source_path=str(tmp_path), mode="lan")

    def test_cloud_fails_when_gcs_key_invalid(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", return_value=True),
            patch("core.health.check_gcs_key", return_value=(False, "GCS key file not found")),
        ):
            with pytest.raises(HealthError, match="GCS key file not found"):
                pre_backup_health(
                    source_path=str(tmp_path),
                    mode="cloud",
                    gcs_key_path="/bad/key.json",
                )

    def test_cloud_fails_when_clock_skewed(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", return_value=True),
            patch("core.health.check_clock_skew", return_value=(False, "1200s skew")),
        ):
            with pytest.raises(HealthError, match="Clock skew"):
                pre_backup_health(source_path=str(tmp_path), mode="cloud")

    def test_source_drive_fails_raises(self, tmp_path):
        with (
            patch("core.health.check_source_drive", return_value=(False, "Source drive missing")),
        ):
            with pytest.raises(HealthError, match="Source drive missing"):
                pre_backup_health(source_path=str(tmp_path), mode="cloud")

    def test_cloud_mode_skips_gcs_key_check_when_none(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")
        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", return_value=True),
            patch("core.health.check_clock_skew", return_value=(True, "")),
        ):
            # Should NOT raise, gcs_key_path=None means skip key check
            pre_backup_health(source_path=str(tmp_path), mode="cloud", gcs_key_path=None)

    def test_all_mode_fails_when_robocopy_missing(self, tmp_path):
        (tmp_path / "file.txt").write_text("data")

        def mock_binary(name):
            return name == "rclone"

        with (
            patch("core.health.check_source_drive", return_value=(True, "")),
            patch("core.health.check_binary_exists", side_effect=mock_binary),
            patch("core.health.check_gcs_key", return_value=(True, "")),
            patch("core.health.check_clock_skew", return_value=(True, "")),
        ):
            with pytest.raises(HealthError, match="robocopy not found"):
                pre_backup_health(
                    source_path=str(tmp_path),
                    mode="all",
                    gcs_key_path="/some/key.json",
                )
