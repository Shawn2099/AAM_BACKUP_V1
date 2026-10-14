"""Tests for core/hashing.py remaining functions and core/health.py gaps.

Covers: check_clock_skew, check_gcs_key, check_binary_exists.
"""

import http.client
from unittest.mock import MagicMock, patch

import pytest

from core.health import check_binary_exists, check_clock_skew, check_gcs_key


class TestCheckGcsKey:
    def test_key_exists(self, tmp_path):
        """Key file exists and non-empty → True."""
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}')
        ok, reason = check_gcs_key(str(key))
        assert ok is True
        assert reason == ""

    def test_key_not_found(self, tmp_path):
        """Key file doesn't exist → False."""
        ok, reason = check_gcs_key(str(tmp_path / "nonexistent.json"))
        assert ok is False
        assert "not found" in reason

    def test_key_empty(self, tmp_path):
        """Key file is empty → False."""
        key = tmp_path / "key.json"
        key.write_text("")
        ok, reason = check_gcs_key(str(key))
        assert ok is False
        assert "empty" in reason


class TestCheckBinaryExists:
    def test_binary_exists(self):
        """Binary found → True."""
        with patch("core.health.resolve_binary", return_value="/usr/bin/rclone"):
            assert check_binary_exists("rclone") is True

    def test_binary_not_found(self):
        """Binary not found → False."""
        with patch("core.health.resolve_binary", return_value=None):
            assert check_binary_exists("rclone") is False


class TestCheckClockSkew:
    def test_clock_ok(self):
        """Small skew → True."""
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.getheader.return_value = "Mon, 01 Jan 2026 00:00:00 GMT"
        mock_conn.getresponse.return_value = mock_resp

        with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn), \
             patch("core.health.pendulum.now", return_value=MagicMock(
                 __sub__=MagicMock(return_value=MagicMock(total_seconds=MagicMock(return_value=5.0)))
             )):
            ok, reason = check_clock_skew(max_skew_seconds=600)
            assert ok is True

    def test_clock_skew_exceeds_limit(self):
        """Large skew → False with message."""
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.getheader.return_value = "Mon, 01 Jan 2026 00:00:00 GMT"
        mock_conn.getresponse.return_value = mock_resp

        with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn), \
             patch("core.health.pendulum.now") as mock_now:
            # Simulate 2000s difference
            mock_now.return_value.__sub__ = MagicMock(
                return_value=MagicMock(total_seconds=MagicMock(return_value=2000.0))
            )
            ok, reason = check_clock_skew(max_skew_seconds=600)
            assert ok is False
            assert "skew" in reason.lower()

    def test_network_failure_fails_open(self):
        """OSError → currently returns True (fail-open). This test documents the behavior."""
        with patch("core.health.http.client.HTTPSConnection", side_effect=OSError("unreachable")):
            ok, reason = check_clock_skew()
            # BUG: This currently returns True (fail-open). Should be False (fail-closed).
            assert ok is True

    def test_missing_date_header(self):
        """No Date header → False."""
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.getheader.return_value = None
        mock_conn.getresponse.return_value = mock_resp

        with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn):
            ok, reason = check_clock_skew()
            assert ok is False
            assert "Date header" in reason

    def test_unparseable_date_header(self):
        """Unparseable Date → currently returns True (fail-open). Documents behavior."""
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.getheader.return_value = "not-a-date"
        mock_conn.getresponse.return_value = mock_resp

        with patch("core.health.http.client.HTTPSConnection", return_value=mock_conn):
            ok, reason = check_clock_skew()
            # BUG: This currently returns True (fail-open). Should be False (fail-closed).
            assert ok is True
