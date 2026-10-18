"""Comprehensive tests for core/report.py — all functions."""

import os
from unittest.mock import MagicMock, patch

import pendulum
import pytest

from core.notify import FAILED, SENT, DeliveryResult
from core.report import (
    generate_report_html,
    send_monthly_report,
    send_weekly_report,
)

IST = "Asia/Kolkata"


def _recent_iso():
    return pendulum.now(IST).isoformat()


class TestGenerateReportHtml:
    def test_empty_db_returns_empty_string(self, tmp_db):
        """Empty database → empty string (not None)."""
        result = generate_report_html(tmp_db, "Test Firm", 7, "Weekly")
        assert result == ""

    def test_with_runs(self, populated_db):
        """DB with runs → HTML string."""
        populated_db.insert_run({
            "run_id": "r1",
            "mode": "cloud",
            "status": "CLOUD_COMPLETE",
            "started_at": _recent_iso(),
            "ended_at": _recent_iso(),
            "files_copied": 100,
            "bytes_copied": 10000,
        })
        result = generate_report_html(populated_db, "Test Firm", 7, "Weekly")
        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0

    def test_monthly_report(self, populated_db):
        """Monthly report generation."""
        populated_db.insert_run({
            "run_id": "r2",
            "mode": "lan",
            "status": "LAN_COMPLETE",
            "started_at": _recent_iso(),
            "ended_at": _recent_iso(),
        })
        result = generate_report_html(populated_db, "Test Firm", 30, "Monthly")
        assert result is not None


class TestSendReports:
    def _make_mock_db(self, run_mode="cloud", run_status=None):
        mock_db = MagicMock()
        if run_status is None:
            run_status = f"{run_mode.upper()}_COMPLETE"
        mock_db.get_runs_since.return_value = [
            {
                "mode": run_mode,
                "status": run_status,
                # previous calendar month: inside the monthly period (audit 2026-10-18)
                "started_at": pendulum.now(IST).start_of("month").subtract(days=3).isoformat(),
                "files_copied": 100,
                "files_failed": 0,
                "duration_seconds": 3600,
                "error_message": "",
            }
        ]
        mock_db.latest_audit.return_value = None
        return mock_db

    def _make_mock_config(self):
        mock_config = MagicMock()
        mock_config.smtp_host = "localhost"
        mock_config.smtp_port = 25
        mock_config.smtp_username = "user"
        mock_config.smtp_password = "pass"
        mock_config.sender = "test@example.com"
        mock_config.recipients = ["admin@example.com"]
        return mock_config

    def test_send_weekly_report_success(self):
        """Successful email send → True."""
        mock_db = self._make_mock_db()
        mock_config = self._make_mock_config()

        with patch("core.report.send_email", return_value=DeliveryResult(SENT)):
            result = send_weekly_report(mock_db, mock_config, "Test Firm")
            assert result is True

    def test_send_weekly_report_failure(self):
        """Failed email send → False."""
        mock_db = self._make_mock_db()
        mock_config = self._make_mock_config()

        with patch("core.report.send_email",
                   return_value=DeliveryResult(FAILED, "Connection refused", 3)):
            result = send_weekly_report(mock_db, mock_config, "Test Firm")
            assert result is False

    def test_send_monthly_report_success(self):
        """Successful monthly email → True."""
        mock_db = self._make_mock_db(run_mode="lan")
        mock_config = self._make_mock_config()

        with patch("core.report.send_email", return_value=DeliveryResult(SENT)):
            result = send_monthly_report(mock_db, mock_config, "Test Firm")
            assert result is True

    def test_smtp_retries_come_from_config(self):
        """WP8: the single transport (core.notify) retries per config."""
        from models.config import NotificationConfig
        assert NotificationConfig().retry_attempts == 3
