"""Tests for core/hashing.py remaining functions and core/health.py gaps.

Covers: check_gcs_key, check_binary_exists.
"""

import http.client
from unittest.mock import MagicMock, patch

import pytest

from core.health import check_binary_exists, check_gcs_key


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


