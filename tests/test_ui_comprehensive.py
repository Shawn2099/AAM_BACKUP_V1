"""Comprehensive tests for ui.py — endpoints, auth, rate limiter, sessions, rendering."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Override the conftest prefect_harness to avoid timeout


@pytest.fixture(scope="session", autouse=True)
def prefect_harness():
    yield


import ui
from ui import (
    _check_api_key_header,
    _check_rate_limit,
    _cleanup_expired_sessions,
    _create_session,
    _get_last_success,
    _last_run_summary,
    _require_auth,
    _serve_report,
    _validate_session,
)


# ── Helpers ────────────────────────────────────────────────────────


def _mock_cfg(auth_enabled=False, api_key="test-key", source_drive="/tmp/src",
              db_path="/tmp/test.db", firm_name="TestFirm",
              cloud_cron="0 18 * * *", lan_cron="0 1 * * *", timezone="Asia/Kolkata"):
    cfg = MagicMock()
    cfg.dashboard.auth_enabled = auth_enabled
    cfg.dashboard.api_key = api_key
    cfg.dashboard.bind_address = "127.0.0.1"
    cfg.dashboard.port = 8080
    cfg.paths.source_drive = source_drive
    cfg.paths.database_path = db_path
    cfg.firm_name = firm_name
    cfg.schedule.cloud_cron = cloud_cron
    cfg.schedule.lan_cron = lan_cron
    cfg.schedule.timezone = timezone
    cfg.notifications.smtp_host = ""
    cfg.notifications.smtp_port = 587
    cfg.notifications.recipients = ["test@example.com"]
    return cfg


def _make_request(cookies=None, headers=None, accept="application/json", client_host="127.0.0.1"):
    mock = MagicMock()
    mock.cookies.get = lambda k: (cookies or {}).get(k)
    mock.headers.get = lambda k, default="": headers.get(k, default) if headers else default
    mock.client.host = client_host
    return mock


# ═══════════════════════════════════════════════════════════════════
# Session Management
# ═══════════════════════════════════════════════════════════════════


class TestSessionCreateAndValidate:
    def test_create_session_returns_64char_token(self):
        token = _create_session()
        assert len(token) == 64

    def test_validate_session_with_valid_token(self):
        token = _create_session()
        assert _validate_session(token) is True

    def test_validate_session_none_returns_false(self):
        assert _validate_session(None) is False

    def test_validate_session_empty_string_returns_false(self):
        assert _validate_session("") is False

    def test_validate_session_nonexistent_returns_false(self):
        assert _validate_session("nonexistent-token") is False

    def test_validate_session_expired_returns_false(self):
        token = _create_session()
        ui._sessions[token]["created_at"] = time.time() - 100000
        assert _validate_session(token) is False

    def test_validate_session_removes_expired(self):
        token = _create_session()
        ui._sessions[token]["created_at"] = time.time() - 100000
        _validate_session(token)
        assert token not in ui._sessions


class TestSessionCleanup:
    def test_cleanup_removes_expired_sessions(self):
        t1 = _create_session()
        t2 = _create_session()
        ui._sessions[t1]["created_at"] = time.time() - 100000
        ui._sessions[t2]["created_at"] = time.time()  # still valid
        _cleanup_expired_sessions()
        assert t1 not in ui._sessions
        assert t2 in ui._sessions

    def test_cleanup_preserves_valid_sessions(self):
        tokens = [_create_session() for _ in range(3)]
        _cleanup_expired_sessions()
        for t in tokens:
            assert t in ui._sessions

    def test_cleanup_empty_store(self):
        ui._sessions.clear()
        _cleanup_expired_sessions()  # should not raise


# ═══════════════════════════════════════════════════════════════════
# Auth — _require_auth
# ═══════════════════════════════════════════════════════════════════


class TestRequireAuth:
    def test_passes_when_auth_disabled(self):
        mock_req = _make_request()
        with patch("ui._auth_enabled", return_value=False):
            _require_auth(mock_req)  # should not raise

    def test_passes_with_valid_session(self):
        token = _create_session()
        mock_req = _make_request(cookies={"session": token})
        with patch("ui._auth_enabled", return_value=True):
            _require_auth(mock_req)

    def test_passes_with_valid_api_key_header(self):
        mock_req = _make_request(headers={"X-API-Key": "test-key"})
        with patch("ui._auth_enabled", return_value=True), \
             patch("ui._get_api_key", return_value="test-key"):
            _require_auth(mock_req)

    def test_browser_redirects_to_login(self):
        mock_req = _make_request(
            headers={"Accept": "text/html"},
        )
        with patch("ui._auth_enabled", return_value=True), \
             patch("ui._get_api_key", return_value="secret"):
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                _require_auth(mock_req)
            assert exc_info.value.status_code == 303

    def test_api_request_returns_401(self):
        mock_req = _make_request(headers={"Accept": "application/json"})
        with patch("ui._auth_enabled", return_value=True), \
             patch("ui._get_api_key", return_value="secret"):
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as exc_info:
                _require_auth(mock_req)
            assert exc_info.value.status_code == 401


# ═══════════════════════════════════════════════════════════════════
# Auth — API Key Header
# ═══════════════════════════════════════════════════════════════════


class TestApiKeyHeader:
    def test_matching_key_returns_true(self):
        mock_req = _make_request(headers={"X-API-Key": "my-key"})
        with patch("ui._get_api_key", return_value="my-key"):
            assert _check_api_key_header(mock_req) is True

    def test_mismatched_key_returns_false(self):
        mock_req = _make_request(headers={"X-API-Key": "wrong"})
        with patch("ui._get_api_key", return_value="my-key"):
            assert _check_api_key_header(mock_req) is False

    def test_empty_configured_key_returns_true(self):
        mock_req = _make_request()
        with patch("ui._get_api_key", return_value=""):
            assert _check_api_key_header(mock_req) is True

    def test_missing_header_returns_false(self):
        mock_req = _make_request()
        with patch("ui._get_api_key", return_value="secret"):
            assert _check_api_key_header(mock_req) is False

    def test_uses_hmac_compare_digest(self):
        mock_req = _make_request(headers={"X-API-Key": "key"})
        with patch("ui._get_api_key", return_value="key"), \
             patch("hmac.compare_digest", return_value=True) as mock_cmp:
            _check_api_key_header(mock_req)
            mock_cmp.assert_called_once_with("key", "key")


# ═══════════════════════════════════════════════════════════════════
# Rate Limiter
# ═══════════════════════════════════════════════════════════════════


class TestRateLimiter:
    def setup_method(self):
        ui._RATE_LIMITS.clear()

    def test_allows_within_limit(self):
        for _ in range(4):
            assert _check_rate_limit("ip1", 5) is True

    def test_blocks_when_limit_exceeded(self):
        for _ in range(5):
            _check_rate_limit("ip2", 5)
        assert _check_rate_limit("ip2", 5) is False

    def test_different_ips_independent(self):
        for _ in range(5):
            _check_rate_limit("ipA", 5)
        assert _check_rate_limit("ipB", 5) is True

    def test_expired_entries_cleaned_up(self):
        for _ in range(5):
            _check_rate_limit("ipC", 5)
        assert _check_rate_limit("ipC", 5) is False
        with patch.object(ui, "_RATE_WINDOW", 0):
            assert _check_rate_limit("ipC", 5) is True

    def test_single_request_allowed(self):
        assert _check_rate_limit("new_ip", 1) is True

    def test_second_request_blocked_at_limit_1(self):
        _check_rate_limit("ipD", 1)
        assert _check_rate_limit("ipD", 1) is False


# ═══════════════════════════════════════════════════════════════════
# _is_running uses AsyncMock
# ═══════════════════════════════════════════════════════════════════


class TestIsRunning:
    def test_is_running_delegates_to_prefect(self):
        with patch("ui._prefect_has_active_run", new_callable=AsyncMock, return_value=True):
            from ui import _is_running
            result = asyncio.run(_is_running("cloud"))
            assert result is True

    def test_is_running_false_when_no_active_run(self):
        with patch("ui._prefect_has_active_run", new_callable=AsyncMock, return_value=False):
            from ui import _is_running
            result = asyncio.run(_is_running("lan"))
            assert result is False


# ═══════════════════════════════════════════════════════════════════
# Dashboard Rendering
# ═══════════════════════════════════════════════════════════════════


class TestDashboardRendering:
    def test_render_without_db(self):
        with patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.Path.exists", return_value=False):
            html = asyncio.run(ui._render_dashboard())
            assert "Loading..." in html

    def test_render_with_db_and_runs(self):
        mock_db = MagicMock()
        mock_db.file_count.return_value = 42
        mock_db.get_recent_runs.return_value = []
        mock_db.last_run.return_value = None
        with patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.Path.exists", return_value=True), \
             patch("ui.get_db", return_value=mock_db), \
             patch("ui._is_running", new_callable=AsyncMock, return_value=False), \
             patch("ui._get_health", return_value={"source_free_gb": "500.0", "source_exists": True}):
            html = asyncio.run(ui._render_dashboard())
            assert "Loading..." in html

    def test_render_with_running_pipeline(self):
        mock_db = MagicMock()
        mock_db.file_count.return_value = 0
        mock_db.get_recent_runs.return_value = []
        mock_db.last_run.return_value = None
        with patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.Path.exists", return_value=True), \
             patch("ui.get_db", return_value=mock_db), \
             patch("ui._is_running", new_callable=AsyncMock, return_value=True), \
             patch("ui._get_health", return_value={"source_free_gb": "100.0", "source_exists": True}):
            html = asyncio.run(ui._render_dashboard())
            assert "Loading..." in html


# ═══════════════════════════════════════════════════════════════════
# Last Run Summary
# ═══════════════════════════════════════════════════════════════════


class TestLastRunSummary:
    def test_none_returns_none(self):
        mock_db = MagicMock()
        mock_db.last_run.return_value = None
        assert _last_run_summary(mock_db, "cloud") is None

    def test_valid_run(self):
        mock_db = MagicMock()
        mock_db.last_run.return_value = {
            "status": "CLOUD_COMPLETE",
            "started_at": "2026-05-27T10:00:00+00:00",
            "files_copied": 42,
            "bytes_copied": 123456,
            "duration_seconds": 123.4,
            "error_message": None,
        }
        summary = _last_run_summary(mock_db, "cloud")
        assert summary["status"] == "CLOUD_COMPLETE"
        assert summary["files"] == 42
        assert summary["duration"] == "123s"

    def test_run_without_duration(self):
        mock_db = MagicMock()
        mock_db.last_run.return_value = {
            "status": "LAN_COMPLETE",
            "started_at": "2026-05-27T10:00:00",
            "files_copied": 0,
            "bytes_copied": 0,
            "duration_seconds": 0,
            "error_message": "",
        }
        summary = _last_run_summary(mock_db, "lan")
        assert summary["duration"] == "?"


class TestGetLastSuccess:
    def test_returns_none_when_not_complete(self):
        mock_db = MagicMock()
        mock_db.last_successful_run.return_value = {"status": "RUNNING"}
        assert _get_last_success(mock_db, "cloud") is None

    def test_returns_ended_at_when_complete(self):
        mock_db = MagicMock()
        mock_db.last_successful_run.return_value = {"status": "CLOUD_COMPLETE", "ended_at": "2026-01-01T00:00:00Z"}
        assert _get_last_success(mock_db, "cloud") == "2026-01-01T00:00:00Z"

    def test_returns_none_when_failed(self):
        mock_db = MagicMock()
        mock_db.last_successful_run.return_value = None
        assert _get_last_success(mock_db, "cloud") is None


# ═══════════════════════════════════════════════════════════════════
# FastAPI Endpoints — /health
# ═══════════════════════════════════════════════════════════════════


class TestHealthEndpoint:
    def test_health_returns_200(self):
        with patch("ui._cfg", return_value=_mock_cfg(source_drive="/tmp")), \
             patch("ui.Path.exists", return_value=True):
            client = TestClient(ui.app)
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"

    def test_health_source_missing(self):
        with patch("ui._cfg", return_value=_mock_cfg(source_drive="/nonexistent")), \
             patch("ui.Path.exists", return_value=False):
            client = TestClient(ui.app)
            resp = client.get("/health")
            assert resp.json()["source_accessible"] is False

    def test_health_exception_returns_200(self):
        with patch("ui._cfg", side_effect=Exception("config error")):
            client = TestClient(ui.app)
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"


# ═══════════════════════════════════════════════════════════════════
# FastAPI Endpoints — /login
# ═══════════════════════════════════════════════════════════════════


class TestLoginEndpoint:
    def test_login_page_renders(self):
        client = TestClient(ui.app)
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "AAM Backup" in resp.text

    def test_login_page_with_error(self):
        client = TestClient(ui.app)
        resp = client.get("/login?error=Invalid+key")
        assert resp.status_code == 200
        assert "Invalid key" in resp.text

    def test_login_post_creates_session(self):
        client = TestClient(ui.app, follow_redirects=False)
        with patch("ui._cfg", return_value=_mock_cfg(auth_enabled=True, api_key="secret")), \
             patch("ui._get_api_key", return_value="secret"):
            resp = client.post("/login", data={"api_key": "secret"})
            assert resp.status_code == 303
            assert "session" in resp.cookies

    def test_login_post_wrong_key_redirects(self):
        client = TestClient(ui.app, follow_redirects=False)
        with patch("ui._cfg", return_value=_mock_cfg(auth_enabled=True, api_key="secret")), \
             patch("ui._get_api_key", return_value="secret"):
            resp = client.post("/login", data={"api_key": "wrong"})
            assert resp.status_code == 303
            assert "Invalid" in resp.headers["location"]

    def test_login_post_rate_limited(self):
        client = TestClient(ui.app)
        with patch("ui._check_rate_limit", return_value=False):
            resp = client.post("/login", data={"api_key": "key"})
            assert resp.status_code == 429


class TestLogoutEndpoint:
    def test_logout_redirects_to_login(self):
        client = TestClient(ui.app, follow_redirects=False)
        resp = client.get("/logout")
        assert resp.status_code == 303
        assert "/login" in resp.headers["location"]


# ═══════════════════════════════════════════════════════════════════
# FastAPI Endpoints — /status
# ═══════════════════════════════════════════════════════════════════


class TestStatusEndpoint:
    def test_status_returns_data(self):
        mock_db = MagicMock()
        mock_db.get_recent_runs.return_value = []
        mock_db.file_count.return_value = 0
        mock_db.last_run.return_value = None
        with patch("ui._require_auth"), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.get_db", return_value=mock_db), \
             patch("ui._is_running", new_callable=AsyncMock, return_value=False), \
             patch("ui._last_run_summary", return_value=None), \
             patch("ui._get_last_success", return_value=None), \
             patch("ui._get_health", return_value={"source_free_gb": "100.0", "source_exists": True}):
            client = TestClient(ui.app)
            resp = client.get("/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["firm"] == "TestFirm"
            assert "cloud" in data
            assert "lan" in data

    def test_status_returns_503_when_db_error(self):
        mock_db = MagicMock()
        mock_db.get_recent_runs.side_effect = Exception("no such table")
        with patch("ui._require_auth"), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.get_db", return_value=mock_db):
            client = TestClient(ui.app)
            resp = client.get("/status")
            assert resp.status_code == 503

    def test_status_with_runs(self):
        mock_db = MagicMock()
        mock_db.get_recent_runs.return_value = [
            {"mode": "cloud", "status": "CLOUD_COMPLETE", "started_at": "2026-05-27T10:00:00",
             "files_copied": 10, "duration_seconds": 300, "error_message": ""}
        ]
        mock_db.file_count.return_value = 5
        mock_db.last_run.return_value = {"status": "CLOUD_COMPLETE", "started_at": "2026-05-27T10:00:00",
                                          "files_copied": 10, "bytes_copied": 5000,
                                          "duration_seconds": 300, "error_message": ""}
        with patch("ui._require_auth"), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.get_db", return_value=mock_db), \
             patch("ui._is_running", new_callable=AsyncMock, return_value=False), \
             patch("ui._last_run_summary", return_value={"status": "CLOUD_COMPLETE", "started_at": "2026-05-27T10:00:00",
                                                          "files": 10, "bytes": 5000, "duration": "300s", "error": None, "ended_at": ""}), \
             patch("ui._get_last_success", return_value="2026-05-27T11:00:00"), \
             patch("ui._get_health", return_value={"source_free_gb": "100.0", "source_exists": True}):
            client = TestClient(ui.app)
            resp = client.get("/status")
            assert resp.status_code == 200
            assert len(resp.json()["recent_runs"]) == 1


# ═══════════════════════════════════════════════════════════════════
# FastAPI Endpoints — / (dashboard)
# ═══════════════════════════════════════════════════════════════════


class TestDashboardEndpoint:
    def test_dashboard_renders(self):
        with patch("ui._require_auth"), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.Path.exists", return_value=False):
            client = TestClient(ui.app)
            resp = client.get("/")
            assert resp.status_code == 200

    def test_dashboard_with_auth_redirect(self):
        with patch("ui._require_auth"), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.Path.exists", return_value=False):
            client = TestClient(ui.app)
            resp = client.get("/")
            assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════
# FastAPI Endpoints — /trigger/cloud, /trigger/lan
# ═══════════════════════════════════════════════════════════════════


class TestTriggerEndpoints:
    def test_trigger_cloud_success(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._is_running", new_callable=AsyncMock, return_value=False), \
             patch("ui._run_in_background", new_callable=AsyncMock):
            client = TestClient(ui.app)
            resp = client.post("/trigger/cloud")
            assert resp.status_code == 200
            assert resp.json()["status"] == "triggered"

    def test_trigger_cloud_already_running(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._is_running", new_callable=AsyncMock, return_value=True):
            client = TestClient(ui.app)
            resp = client.post("/trigger/cloud")
            assert resp.status_code == 400
            assert resp.json()["status"] == "already_running"

    def test_trigger_cloud_rate_limited(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=False):
            client = TestClient(ui.app)
            resp = client.post("/trigger/cloud")
            assert resp.status_code == 429

    def test_trigger_lan_success(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._is_running", new_callable=AsyncMock, return_value=False), \
             patch("ui._run_in_background", new_callable=AsyncMock):
            client = TestClient(ui.app)
            resp = client.post("/trigger/lan")
            assert resp.status_code == 200
            assert resp.json()["status"] == "triggered"

    def test_trigger_lan_already_running(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._is_running", new_callable=AsyncMock, return_value=True):
            client = TestClient(ui.app)
            resp = client.post("/trigger/lan")
            assert resp.status_code == 400

    def test_trigger_lan_rate_limited(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=False):
            client = TestClient(ui.app)
            resp = client.post("/trigger/lan")
            assert resp.status_code == 429


# ═══════════════════════════════════════════════════════════════════
# FastAPI Endpoints — /report/weekly, /report/monthly
# ═══════════════════════════════════════════════════════════════════


class TestReportEndpoints:
    def test_report_weekly_downloads(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.Path.exists", return_value=True), \
             patch("ui.get_db") as mock_db_cls, \
             patch("ui.get_db") as mock_get_db, \
             patch("core.report.generate_report_html", return_value="<html>report</html>"):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            mock_get_db.return_value = mock_db
            client = TestClient(ui.app)
            resp = client.get("/report/weekly")
            assert resp.status_code == 200

    def test_report_monthly_downloads(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.Path.exists", return_value=True), \
             patch("ui.get_db") as mock_db_cls, \
             patch("ui.get_db") as mock_get_db, \
             patch("core.report.generate_report_html", return_value="<html>report</html>"):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            mock_get_db.return_value = mock_db
            client = TestClient(ui.app)
            resp = client.get("/report/monthly")
            assert resp.status_code == 200

    def test_report_rate_limited(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=False):
            client = TestClient(ui.app)
            resp = client.get("/report/weekly")
            assert resp.status_code == 429

    def test_report_no_db_returns_503(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.Path.exists", return_value=False):
            client = TestClient(ui.app)
            resp = client.get("/report/weekly")
            assert resp.status_code == 503


# ═══════════════════════════════════════════════════════════════════
# FastAPI Endpoints — email triggers
# ═══════════════════════════════════════════════════════════════════


class TestEmailTriggerEndpoints:
    def test_weekly_email_no_data(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.get_db") as mock_db_cls, \
             patch("ui.get_db") as mock_get_db, \
             patch("core.report.generate_report_html", return_value=""):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            mock_get_db.return_value = mock_db
            client = TestClient(ui.app)
            resp = client.post("/trigger/report/weekly/email")
            assert resp.status_code == 404

    def test_monthly_email_no_data(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.get_db") as mock_db_cls, \
             patch("ui.get_db") as mock_get_db, \
             patch("core.report.generate_report_html", return_value=""):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            mock_get_db.return_value = mock_db
            client = TestClient(ui.app)
            resp = client.post("/trigger/report/monthly/email")
            assert resp.status_code == 404

    def test_weekly_email_send_success(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.get_db") as mock_db_cls, \
             patch("ui.get_db") as mock_get_db, \
             patch("core.report.generate_report_html", return_value="<html>report</html>"), \
             patch("core.report.send_weekly_report", return_value=True):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            mock_get_db.return_value = mock_db
            client = TestClient(ui.app)
            resp = client.post("/trigger/report/weekly/email")
            assert resp.status_code == 200
            assert resp.json()["status"] == "sent"

    def test_weekly_email_send_failure(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.get_db") as mock_db_cls, \
             patch("ui.get_db") as mock_get_db, \
             patch("core.report.generate_report_html", return_value="<html>report</html>"), \
             patch("core.report.send_weekly_report", return_value=False):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            mock_get_db.return_value = mock_db
            client = TestClient(ui.app)
            resp = client.post("/trigger/report/weekly/email")
            assert resp.status_code == 500

    def test_weekly_email_smtp_exception(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.get_db") as mock_db_cls, \
             patch("ui.get_db") as mock_get_db, \
             patch("core.report.generate_report_html", return_value="<html>report</html>"), \
             patch("core.report.send_weekly_report", side_effect=Exception("SMTP error")):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            mock_get_db.return_value = mock_db
            client = TestClient(ui.app)
            resp = client.post("/trigger/report/weekly/email")
            assert resp.status_code == 500
            assert "SMTP" in resp.json()["detail"]

    def test_monthly_email_send_success(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=True), \
             patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.get_db") as mock_db_cls, \
             patch("ui.get_db") as mock_get_db, \
             patch("core.report.generate_report_html", return_value="<html>report</html>"), \
             patch("core.report.send_monthly_report", return_value=True):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            mock_get_db.return_value = mock_db
            client = TestClient(ui.app)
            resp = client.post("/trigger/report/monthly/email")
            assert resp.status_code == 200
            assert resp.json()["status"] == "sent"

    def test_weekly_email_rate_limited(self):
        with patch("ui._require_auth"), \
             patch("ui._check_rate_limit", return_value=False):
            client = TestClient(ui.app)
            resp = client.post("/trigger/report/weekly/email")
            assert resp.status_code == 429


# ═══════════════════════════════════════════════════════════════════
# _serve_report helper
# ═══════════════════════════════════════════════════════════════════


class TestServeReport:
    def test_serves_html_report(self):
        with patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.Path.exists", return_value=True), \
             patch("ui.get_db") as mock_db_cls, \
             patch("ui.get_db") as mock_get_db, \
             patch("core.report.generate_report_html", return_value="<html>weekly</html>"):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            mock_get_db.return_value = mock_db
            resp = _serve_report(7, "Weekly")
            assert resp.status_code == 200
            assert resp.media_type == "text/html"

    def test_returns_503_when_no_db(self):
        with patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.Path.exists", return_value=False):
            resp = _serve_report(7, "Weekly")
            assert resp.status_code == 503

    def test_returns_404_when_no_runs(self):
        with patch("ui._cfg", return_value=_mock_cfg()), \
             patch("ui.Path.exists", return_value=True), \
             patch("ui.get_db") as mock_db_cls, \
             patch("ui.get_db") as mock_get_db, \
             patch("core.report.generate_report_html", return_value=""):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db
            mock_get_db.return_value = mock_db
            resp = _serve_report(7, "Weekly")
            assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════
# _get_health helper
# ═══════════════════════════════════════════════════════════════════


class TestGetHealth:
    def test_health_returns_free_gb(self):
        mock_usage = MagicMock()
        mock_usage.free = 100 * 1024 ** 3  # 100 GB
        with patch("ui._cfg", return_value=_mock_cfg(source_drive="/tmp")), \
             patch("ui.shutil.disk_usage", return_value=mock_usage), \
             patch("ui.Path.exists", return_value=True):
            result = asyncio.run(ui._get_health())
            assert "source_free_gb" in result
            assert result["source_exists"] is True

    def test_health_exception_returns_error(self):
        with patch("ui._cfg", side_effect=Exception("config error")):
            result = asyncio.run(ui._get_health())
            assert result == {"error": "unavailable"}
