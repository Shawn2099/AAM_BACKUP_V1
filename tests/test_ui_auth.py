"""Tests for ui.py - sign-in, sessions and the always-on authentication (plan E).

Sign-in is always on: one dashboard password (hash in config.yaml), a second Advanced password,
no API key, no 'auth disabled' mode. Until both hashes exist nothing opens (fail closed).
"""

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import ui
from test_support import ui_login
from ui import (
    _RATE_LIMITS,
    _check_rate_limit,
    _cleanup_expired_sessions,
    _create_session,
    _run_state_fields,
    _sessions,
    _validate_session,
)

pytestmark = pytest.mark.anonymous          # these tests build their own clients


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    return TestClient(ui.app, raise_server_exceptions=False, follow_redirects=False)


class TestSessionManagement:
    def test_create_session(self):
        token = _create_session()
        assert token in _sessions
        assert "created_at" in _sessions[token] and _sessions[token]["fp"]

    def test_validate_session_valid(self):
        assert _validate_session(_create_session()) is True

    def test_validate_session_invalid(self):
        assert _validate_session("nonexistent") is False

    def test_validate_session_none(self):
        assert _validate_session(None) is False

    def test_cleanup_expired_sessions(self):
        _sessions["expired"] = {"created_at": time.time() - 90000, "fp": "x"}      # > 24h
        _sessions["valid"] = {"created_at": time.time(), "fp": "x"}
        _cleanup_expired_sessions()
        assert "expired" not in _sessions
        assert "valid" in _sessions

    def test_session_ttl(self):
        token = _create_session()
        _sessions[token]["created_at"] = time.time() - 90000
        assert _validate_session(token) is False

    def test_session_ends_when_the_dashboard_password_changes(self):
        """Plan E2: the session remembers the fingerprint of the hash it was opened with."""
        token = _create_session()
        other = SimpleNamespace(dashboard=SimpleNamespace(
            login_password_hash="pbkdf2_sha256$1$a$b", admin_password_hash=ui_login.ADMIN_HASH))
        with patch("ui._cfg", return_value=other):
            assert _validate_session(token) is False
        assert token not in _sessions

    def test_session_is_never_valid_while_the_passwords_are_not_set_up(self):
        token = _create_session()
        empty = SimpleNamespace(dashboard=SimpleNamespace(login_password_hash="",
                                                          admin_password_hash=""))
        with patch("ui._cfg", return_value=empty):
            assert _validate_session(token) is False


class TestSignIn:
    def test_login_page_asks_for_a_password_not_an_api_key(self, client):
        page = client.get("/login")
        assert page.status_code == 200 and 'name="password"' in page.text
        assert "API" not in page.text

    def test_right_password_gives_a_session_cookie_that_opens_the_dashboard(self, client):
        r = client.post("/login", data={"password": ui_login.LOGIN_PASSWORD})
        assert r.status_code == 303 and r.headers["location"] == "/"
        cookie = r.cookies.get("session")
        assert cookie and _validate_session(cookie)
        ok = client.get("/status", cookies={"session": cookie}, headers={"Accept": "application/json"})
        assert ok.status_code != 401

    def test_wrong_password_and_the_advanced_password_do_not_sign_in(self, client):
        for pw in ("wrong", "", ui_login.ADMIN_PASSWORD):
            r = client.post("/login", data={"password": pw})
            assert r.status_code == 303 and r.headers["location"].startswith("/login?error=")
            assert "session" not in r.cookies

    def test_nothing_opens_without_a_session(self, client):
        assert client.get("/status", headers={"Accept": "application/json"}).status_code == 401
        page = client.get("/", headers={"Accept": "text/html"})
        assert page.status_code == 303 and page.headers["location"] == "/login"

    def test_the_x_api_key_header_is_gone(self, client):
        for key in ("anything", ui_login.LOGIN_PASSWORD, ui_login.ADMIN_PASSWORD):
            r = client.get("/status", headers={"X-API-Key": key, "Accept": "application/json"})
            assert r.status_code == 401

    def test_logout_ends_the_session(self, client):
        token = _create_session()
        client.get("/logout", cookies={"session": token})
        assert not _validate_session(token)

    def test_health_stays_open_for_monitoring(self, client):
        assert client.get("/health").status_code in (200, 503)      # answered, never 401/303


class TestNotSetUp:
    @pytest.fixture
    def empty(self, monkeypatch):
        cfg = SimpleNamespace(dashboard=SimpleNamespace(
            login_password_hash="", admin_password_hash=ui_login.ADMIN_HASH,
            session_hours=24, rate_limit_logins=10))
        monkeypatch.setattr(ui, "_cfg", lambda: cfg)
        return cfg

    def test_login_page_says_not_set_up_and_has_no_form(self, client, empty):
        page = client.get("/login")
        assert page.status_code == 503 and "not set up" in page.text and "<form" not in page.text

    def test_login_never_signs_anyone_in(self, client, empty):
        for pw in ("", ui_login.LOGIN_PASSWORD, ui_login.ADMIN_PASSWORD):
            r = client.post("/login", data={"password": pw})
            assert "session" not in r.cookies and r.headers["location"] == "/login"

    def test_api_callers_get_503_and_browsers_the_login_page(self, client, empty):
        assert client.get("/status", headers={"Accept": "application/json"}).status_code == 503
        assert client.get("/", headers={"Accept": "text/html"}).headers["location"] == "/login"

    def test_only_one_hash_set_is_still_not_set_up(self, client, monkeypatch):
        cfg = SimpleNamespace(dashboard=SimpleNamespace(
            login_password_hash=ui_login.LOGIN_HASH, admin_password_hash=""))
        monkeypatch.setattr(ui, "_cfg", lambda: cfg)
        assert client.get("/status", headers={"Accept": "application/json"}).status_code == 503


_LIMIT = 5   # dashboard.rate_limit_triggers default (audit 2026-10-18)


class TestRateLimiter:
    def test_rate_limit_allows_under_limit(self):
        client_ip = "127.0.0.1"
        _RATE_LIMITS.pop(client_ip, None)
        assert _check_rate_limit(client_ip, _LIMIT) is True

    def test_rate_limit_blocks_over_limit(self):
        client_ip = "127.0.0.2"
        _RATE_LIMITS.pop(client_ip, None)
        for _ in range(_LIMIT):
            _check_rate_limit(client_ip, _LIMIT)
        assert _check_rate_limit(client_ip, _LIMIT) is False

    def test_rate_limit_cleans_expired(self):
        client_ip = "127.0.0.3"
        now = time.time()
        _RATE_LIMITS[client_ip] = [now - 400]  # Older than 300s window
        _check_rate_limit(client_ip, _LIMIT)
        assert len(_RATE_LIMITS[client_ip]) == 1


class TestRunStateFields:
    def test_running(self):
        result = _run_state_fields(True)
        assert result["running"] is True
        assert result["run_state"] == "running"

    def test_idle(self):
        result = _run_state_fields(False)
        assert result["running"] is False
        assert result["run_state"] == "idle"

    def test_unknown(self):
        result = _run_state_fields(None)
        assert result["running"] is False
        assert result["run_state"] == "unknown"
