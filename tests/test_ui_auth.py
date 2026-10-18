"""Tests for ui.py — authentication and session system.

Previously the entire auth/session system had zero test coverage.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from ui import (
    _check_api_key_header,
    _check_rate_limit,
    _cleanup_expired_sessions,
    _create_session,
    _get_api_key,
    _is_running,
    _RATE_LIMITS,
    _run_state_fields,
    _sessions,
    _validate_session,
    _auth_enabled,
)


class TestSessionManagement:
    def test_create_session(self):
        """Session token is created and stored."""
        token = _create_session()
        assert token in _sessions
        assert "created_at" in _sessions[token]

    def test_validate_session_valid(self):
        """Valid session token → True."""
        token = _create_session()
        assert _validate_session(token) is True

    def test_validate_session_invalid(self):
        """Invalid token → False."""
        assert _validate_session("nonexistent") is False

    def test_validate_session_none(self):
        """None token → False."""
        assert _validate_session(None) is False

    def test_cleanup_expired_sessions(self):
        """Expired sessions are removed."""
        _sessions["expired"] = {"created_at": time.time() - 90000}  # > 24h
        _sessions["valid"] = {"created_at": time.time()}
        _cleanup_expired_sessions()
        assert "expired" not in _sessions
        assert "valid" in _sessions

    def test_session_ttl(self):
        """Session expires after TTL."""
        token = _create_session()
        # Manually set created_at to past
        _sessions[token]["created_at"] = time.time() - 90000
        assert _validate_session(token) is False


_LIMIT = 5   # dashboard.rate_limit_triggers default (audit 2026-10-18)


class TestRateLimiter:
    def test_rate_limit_allows_under_limit(self):
        """Requests under limit are allowed."""
        client_ip = "127.0.0.1"
        _RATE_LIMITS.pop(client_ip, None)
        assert _check_rate_limit(client_ip, _LIMIT) is True

    def test_rate_limit_blocks_over_limit(self):
        """Requests over limit are blocked."""
        client_ip = "127.0.0.2"
        _RATE_LIMITS.pop(client_ip, None)
        # Fill up to limit
        for _ in range(_LIMIT):
            _check_rate_limit(client_ip, _LIMIT)
        # Next one should be blocked
        assert _check_rate_limit(client_ip, _LIMIT) is False

    def test_rate_limit_cleans_expired(self):
        """Expired entries are cleaned up."""
        client_ip = "127.0.0.3"
        now = time.time()
        _RATE_LIMITS[client_ip] = [now - 400]  # Older than 300s window
        _check_rate_limit(client_ip, _LIMIT)
        # Should have been cleaned and new entry added
        assert len(_RATE_LIMITS[client_ip]) == 1


class TestApiKeyHeader:
    def test_matching_key(self):
        """Matching API key → True."""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = "secret-key"
        with patch("ui._get_api_key", return_value="secret-key"):
            assert _check_api_key_header(mock_request) is True

    def test_mismatched_key(self):
        """Mismatched API key → False."""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = "wrong-key"
        with patch("ui._get_api_key", return_value="secret-key"):
            assert _check_api_key_header(mock_request) is False

    def test_no_configured_key(self):
        """No configured key → True (auth disabled)."""
        mock_request = MagicMock()
        mock_request.headers.get.return_value = ""
        with patch("ui._get_api_key", return_value=""):
            assert _check_api_key_header(mock_request) is True


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


class TestAuthEnabled:
    def test_auth_enabled(self):
        with patch("ui._cfg") as mock_cfg:
            mock_cfg.return_value.dashboard.auth_enabled = True
            assert _auth_enabled() is True

    def test_auth_disabled(self):
        with patch("ui._cfg") as mock_cfg:
            mock_cfg.return_value.dashboard.auth_enabled = False
            assert _auth_enabled() is False
