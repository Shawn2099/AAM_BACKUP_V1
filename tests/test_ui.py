"""Tests for dashboard UI — authentication, helpers, and rendering."""

import time
from unittest.mock import MagicMock, patch

import pytest

import ui
from core.manifest import ManifestDB
from core.process import pid_alive
from ui import (
    _check_api_key_header,
    _create_session,
    _last_run_summary,
    _require_auth,
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
    def test_pid_alive_via_kill(self):
        with patch("os.kill", return_value=None):
            assert pid_alive(12345) is True

    def test_pid_dead_no_tasklist(self):
        with (
            patch("os.kill", side_effect=OSError),
            patch("core.process.sys.platform", "linux"),
        ):
            assert pid_alive(99999) is False

    def test_pid_alive_via_tasklist_win32(self):
        with (
            patch("os.kill", side_effect=OSError),
            patch("core.process.sys.platform", "win32"),
            patch("core.process.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(stdout="99999 some-process.exe", returncode=0)
            assert pid_alive(99999) is True

    def test_pid_dead_via_tasklist_win32(self):
        with (
            patch("os.kill", side_effect=OSError),
            patch("core.process.sys.platform", "win32"),
            patch("core.process.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(stdout="INFO: No tasks are running", returncode=0)
            assert pid_alive(99999) is False
