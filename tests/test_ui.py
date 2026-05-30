"""Tests for dashboard UI — authentication, helpers, rendering, and reports."""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

import ui
from core.manifest import ManifestDB
from core.process import pid_alive
from core.time_utils import cron_to_human
from ui import (
    _check_api_key_header,
    _create_session,
    _get_last_success,
    _last_run_summary,
    _render_dashboard,
    _require_auth,
    _serve_report,
    _validate_session,
)


class TestSessionManagement:
    def test_create_and_validate_session(self):
        token = _create_session()
        assert len(token) == 64
        assert _validate_session(token) is True

    def test_invalid_token_returns_false(self):
        assert _validate_session(None) is False
        assert _validate_session("") is False
        assert _validate_session("nonexistent") is False

    def test_expired_session_returns_false(self):
        token = _create_session()
        assert _validate_session(token) is True
        ui._sessions[token]["created_at"] = time.time() - 100000
        assert _validate_session(token) is False


class TestAuthMiddleware:
    def test_passes_when_auth_disabled(self):
        mock_request = MagicMock()
        with patch("ui._auth_enabled", return_value=False):
            _require_auth(mock_request)

    def test_raises_when_no_session_and_auth_enabled(self):
        mock_request = MagicMock()
        mock_request.cookies.get.return_value = None
        mock_request.headers.get.return_value = ""
        with (
            patch("ui._auth_enabled", return_value=True),
            patch("ui._get_api_key", return_value="secret"),
            pytest.raises(Exception),  # FastAPI HTTPException
        ):
            _require_auth(mock_request)

    def test_passes_with_valid_session(self):
        mock_request = MagicMock()
        token = _create_session()
        mock_request.cookies.get.return_value = token
        with patch("ui._auth_enabled", return_value=True):
            _require_auth(mock_request)

    def test_passes_with_valid_api_key_header(self):
        mock_request = MagicMock()
        mock_request.cookies.get.return_value = None
        mock_request.headers.get.return_value = "secret-key"
        with (
            patch("ui._auth_enabled", return_value=True),
            patch("ui._get_api_key", return_value="secret-key"),
        ):
            _require_auth(mock_request)


class TestApiKeyHeader:
    def test_matching_key_returns_true(self):
        mock_request = MagicMock()
        mock_request.headers.get.return_value = "my-key"
        with patch("ui._get_api_key", return_value="my-key"):
            assert _check_api_key_header(mock_request) is True

    def test_mismatched_key_returns_false(self):
        mock_request = MagicMock()
        mock_request.headers.get.return_value = "wrong-key"
        with patch("ui._get_api_key", return_value="my-key"):
            assert _check_api_key_header(mock_request) is False

    def test_empty_configured_key_returns_true(self):
        mock_request = MagicMock()
        with patch("ui._get_api_key", return_value=""):
            assert _check_api_key_header(mock_request) is True


class TestLastRunSummary:
    def test_none_run_returns_none(self):
        db = MagicMock(spec=ManifestDB)
        db.last_run.return_value = None
        assert _last_run_summary(db, "cloud") is None

    def test_valid_run_returns_summary(self):
        db = MagicMock(spec=ManifestDB)
        db.last_run.return_value = {
            "status": "CLOUD_COMPLETE",
            "started_at": "2026-05-27T10:00:00+00:00",
            "files_copied": 42,
            "bytes_copied": 123456,
            "duration_seconds": 123.4,
            "error_message": None,
        }
        summary = _last_run_summary(db, "cloud")
        assert summary["status"] == "CLOUD_COMPLETE"
        assert summary["files"] == 42
        assert summary["bytes"] == 123456
        assert summary["duration"] == "123s"


class TestPidAlive:
    def test_pid_alive(self):
        with patch("psutil.pid_exists", return_value=True):
            assert pid_alive(12345) is True

    def test_pid_dead(self):
        with patch("psutil.pid_exists", return_value=False):
            assert pid_alive(99999) is False


class TestReportEndpoints:
    @patch("ui.Path.exists", return_value=True)
    @patch("ui.ManifestDB")
    @patch("ui._cfg")
    def test_report_downloads_html(self, mock_cfg, mock_db_cls, mock_exists):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        mock_cfg.return_value = MagicMock(
            paths=MagicMock(database_path="/tmp/test.db"),
            firm_name="TestFirm",
        )
        with patch("core.report.generate_report_html",
                   return_value="<html>Weekly Report</html>"):
            response = _serve_report(7, "Weekly")
            assert response.status_code == 200
            assert response.media_type == "text/html"
            assert "attachment" in response.headers["content-disposition"]

    @patch("ui.Path.exists", return_value=False)
    @patch("ui._cfg")
    def test_report_returns_503_when_no_db(self, mock_cfg, mock_exists):
        mock_cfg.return_value = MagicMock(
            paths=MagicMock(database_path="/tmp/missing.db"),
        )
        response = _serve_report(7, "Weekly")
        assert response.status_code == 503

    @patch("ui.Path.exists", return_value=True)
    @patch("ui.ManifestDB")
    @patch("ui._cfg")
    def test_report_returns_404_when_no_runs(self, mock_cfg, mock_db_cls, mock_exists):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        mock_cfg.return_value = MagicMock(
            paths=MagicMock(database_path="/tmp/test.db"),
            firm_name="TestFirm",
        )
        with patch("core.report.generate_report_html", return_value=""):
            response = _serve_report(7, "Weekly")
            assert response.status_code == 404


class TestCronToHuman:
    def test_daily(self):
        assert cron_to_human("0 18 * * *", "Asia/Kolkata") == "Daily at 18:00 Kolkata"

    def test_weekly(self):
        assert cron_to_human("0 8 * * MON", "Asia/Kolkata") == "Every Monday at 08:00 Kolkata"

    def test_monthly(self):
        assert cron_to_human("0 8 1 * *", "Asia/Kolkata") == "1st of month at 08:00 Kolkata"

    def test_short_cron_returns_raw(self):
        assert cron_to_human("invalid", "UTC") == "invalid"

    def test_tuesday(self):
        assert "Tuesday" in cron_to_human("0 9 * * TUE", "UTC")


class TestGetLastSuccess:
    def test_returns_none_when_no_status(self):
        db = MagicMock()
        db.last_run.return_value = {"status": "RUNNING"}
        assert _get_last_success(db, "cloud") is None

    def test_returns_ended_at_when_complete(self):
        db = MagicMock()
        db.last_run.return_value = {"status": "CLOUD_COMPLETE", "ended_at": "2026-01-01T00:00:00Z"}
        assert _get_last_success(db, "cloud") == "2026-01-01T00:00:00Z"

    def test_returns_none_when_failed(self):
        db = MagicMock()
        db.last_run.return_value = {"status": "CLOUD_FAILED", "ended_at": "2026-01-01T00:00:00Z"}
        assert _get_last_success(db, "cloud") is None


# ── FastAPI endpoint integration tests ──────────────────────

from fastapi.testclient import TestClient


class TestEndpointHealth:
    def test_health_returns_200(self):
        with patch("ui.Path.exists", return_value=True):
            with patch("ui._cfg") as mock_cfg:
                mock_cfg.return_value = MagicMock(
                    paths=MagicMock(source_drive="/tmp"),
                )
                client = TestClient(ui.app)
                response = client.get("/health")
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "healthy"

    def test_health_source_accessible_false(self):
        with patch("ui.Path.exists", return_value=False):
            with patch("ui._cfg") as mock_cfg:
                mock_cfg.return_value = MagicMock(
                    paths=MagicMock(source_drive="/nonexistent"),
                )
                client = TestClient(ui.app)
                response = client.get("/health")
                assert response.status_code == 200
                data = response.json()
                assert data["source_accessible"] is False


class TestEndpointLogin:
    def test_login_page_renders(self):
        client = TestClient(ui.app)
        response = client.get("/login")
        assert response.status_code == 200
        assert "AAM Backup" in response.text


class TestEndpointStatus:
    def test_status_returns_503_when_no_db(self):
        client = TestClient(ui.app)
        with patch("ui._require_auth"), \
             patch("ui._cfg") as mock_cfg, \
             patch("ui.Path.exists", return_value=False):
            mock_cfg.return_value = MagicMock(
                paths=MagicMock(database_path="/tmp/missing.db"),
            )
            response = client.get("/status")
            assert response.status_code == 503
            data = response.json()
            assert "ManifestDB not found" in data["error"]

    def test_status_returns_data_when_db_exists(self):
        client = TestClient(ui.app)
        with patch("ui._require_auth"), \
             patch("ui._cfg") as mock_cfg, \
             patch("ui.Path.exists", return_value=True), \
             patch("ui.ManifestDB") as mock_db_cls, \
             patch("ui._is_running", return_value=False), \
             patch("ui._last_run_summary", return_value=None), \
             patch("ui._get_last_success", return_value=None), \
             patch("ui._get_health", return_value={"source_free_gb": "100.0", "source_exists": True}):
            mock_cfg.return_value = MagicMock(
                firm_name="Test Firm",
                paths=MagicMock(database_path="/tmp/test.db"),
                schedule=MagicMock(cloud_cron="0 18 * * *", lan_cron="0 1 * * *", timezone="Asia/Kolkata"),
            )
            mock_db = MagicMock()
            mock_db.get_recent_runs.return_value = []
            mock_db.file_count.return_value = 0
            mock_db_cls.return_value = mock_db

            response = client.get("/status")
            assert response.status_code == 200
            data = response.json()
            assert data["firm"] == "Test Firm"
            assert "fy_prefix" in data
            assert data["cloud"]["running"] is False
            assert data["lan"]["running"] is False
            assert data["recent_runs"] == []


class TestRateLimiter:
    def setup_method(self):
        ui._RATE_LIMITS.clear()

    def test_allows_within_limit(self):
        for _ in range(5):
            assert ui._check_rate_limit("192.168.1.1", 5) is True

    def test_blocks_when_limit_exceeded(self):
        for _ in range(5):
            ui._check_rate_limit("192.168.1.2", 5)
        assert ui._check_rate_limit("192.168.1.2", 5) is False

    def test_different_ips_independent(self):
        for _ in range(5):
            ui._check_rate_limit("10.0.0.1", 5)
        assert ui._check_rate_limit("10.0.0.2", 5) is True

    def test_expired_entries_removed(self):
        for _ in range(5):
            ui._check_rate_limit("172.16.0.1", 5)
        assert ui._check_rate_limit("172.16.0.1", 5) is False
        with patch.object(ui, "_RATE_WINDOW", 0):
            assert ui._check_rate_limit("172.16.0.1", 5) is True


class TestTriggerEndpoints:
    def test_trigger_cloud_not_running(self):
        client = TestClient(ui.app)
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._is_running", return_value=False), \
             patch("ui._run_in_background") as mock_run:
            response = client.post("/trigger/cloud")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "triggered"

    def test_trigger_cloud_already_running(self):
        client = TestClient(ui.app)
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._is_running", return_value=True):
            response = client.post("/trigger/cloud")
            assert response.status_code == 400
            data = response.json()
            assert data["status"] == "already_running"

    def test_trigger_cloud_rate_limited(self):
        client = TestClient(ui.app)
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=False):
            response = client.post("/trigger/cloud")
            assert response.status_code == 429

    def test_trigger_lan_not_running(self):
        client = TestClient(ui.app)
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._is_running", return_value=False), \
             patch("ui._run_in_background") as mock_run:
            response = client.post("/trigger/lan")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "triggered"


class TestDashboardRendering:
    def test_render_without_db_returns_no_data(self):
        with patch("ui._cfg") as mock_cfg, \
             patch("ui.Path.exists", return_value=False):
            mock_cfg.return_value = MagicMock(
                paths=MagicMock(database_path="/tmp/missing.db"),
            )
            html = asyncio.run(_render_dashboard())
            assert "Unknown" in html
            assert "Unavailable" in html

    def test_render_with_db_and_runs(self):
        with patch("ui._cfg") as mock_cfg, \
             patch("ui.Path.exists", return_value=True), \
             patch("ui.ManifestDB") as mock_db_cls, \
             patch("ui._is_running", return_value=False), \
             patch("ui._get_health", return_value={"source_free_gb": "500.0", "source_exists": True}):
            mock_cfg.return_value = MagicMock(
                paths=MagicMock(database_path="/tmp/test.db"),
                schedule=MagicMock(cloud_cron="0 18 * * *", lan_cron="0 1 * * *", timezone="Asia/Kolkata"),
                firm_name="Test Firm",
            )
            mock_db = MagicMock()
            mock_db.file_count.return_value = 42
            mock_db.get_recent_runs.return_value = []
            mock_db.last_run.return_value = None
            mock_db_cls.return_value = mock_db

            html = asyncio.run(_render_dashboard())
            assert "Test Firm" in html or "42" in html
            mock_db.close.assert_called_once()
