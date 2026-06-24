"""Comprehensive tests for core/report.py — email reports and HTML generation."""

from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from core.report import (
    _send_email,
    generate_report_html,
    send_failure_alert,
    send_monthly_report,
    send_summary_report,
    send_weekly_report,
)
from models.config import NotificationConfig


@pytest.fixture(autouse=True, scope="session")
def prefect_harness():
    """Override session-scoped fixture from conftest to avoid Prefect server startup."""
    yield


def _make_config(**overrides) -> NotificationConfig:
    defaults = dict(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="user@example.com",
        smtp_password="secret",
        sender="backup@example.com",
        recipients=["admin@example.com"],
        send_on_failure=True,
    )
    defaults.update(overrides)
    return NotificationConfig(**defaults)


# ── _send_email ──────────────────────────────────────────────────────────────


class TestSendEmail:
    def test_returns_false_when_no_host(self):
        config = _make_config(smtp_host="")
        assert _send_email(config, "subj", "<p>hi</p>") is False

    def test_returns_false_when_no_sender(self):
        config = _make_config(sender="")
        assert _send_email(config, "subj", "<p>hi</p>") is False

    def test_returns_false_when_no_recipients(self):
        config = _make_config(recipients=[])
        assert _send_email(config, "subj", "<p>hi</p>") is False

    def test_returns_false_when_no_username(self):
        config = _make_config(smtp_username="")
        assert _send_email(config, "subj", "<p>hi</p>") is False

    def test_returns_false_when_no_password(self):
        config = _make_config(smtp_password="")
        assert _send_email(config, "subj", "<p>hi</p>") is False

    def test_ssl_port_465(self):
        config = _make_config(smtp_port=465)
        mock_server = MagicMock()
        with patch("core.report.smtplib.SMTP_SSL", return_value=mock_server) as mock_ssl:
            result = _send_email(config, "subj", "<p>hi</p>")
        assert result is True
        mock_ssl.assert_called_once_with("smtp.example.com", 465, timeout=30)
        mock_server.login.assert_called_once()
        mock_server.sendmail.assert_called_once()
        mock_server.quit.assert_called()

    def test_starttls_port_587(self):
        config = _make_config(smtp_port=587)
        mock_server = MagicMock()
        with patch("core.report.smtplib.SMTP", return_value=mock_server) as mock_smtp:
            result = _send_email(config, "subj", "<p>hi</p>")
        assert result is True
        mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=30)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once()
        mock_server.sendmail.assert_called_once()
        mock_server.quit.assert_called()

    def test_plain_port_25(self):
        config = _make_config(smtp_port=25)
        mock_server = MagicMock()
        with patch("core.report.smtplib.SMTP", return_value=mock_server):
            result = _send_email(config, "subj", "<p>hi</p>")
        assert result is True
        mock_server.starttls.assert_called_once()

    def test_returns_false_on_smtp_connection_error(self):
        config = _make_config()
        with patch("core.report.smtplib.SMTP_SSL", side_effect=ConnectionRefusedError("refused")):
            result = _send_email(config, "subj", "<p>hi</p>")
        assert result is False

    def test_returns_false_on_sendmail_failure(self):
        config = _make_config(smtp_port=465)
        mock_server = MagicMock()
        mock_server.sendmail.side_effect = smtplib.SMTPException("send failed")
        with patch("core.report.smtplib.SMTP_SSL", return_value=mock_server):
            with patch("core.report.smtplib.SMTPException", SMTPException=type(mock_server.sendmail.side_effect)):
                result = _send_email(config, "subj", "<p>hi</p>")
        assert result is False

    def test_quit_called_even_on_error(self):
        config = _make_config(smtp_port=465)
        mock_server = MagicMock()
        mock_server.login.side_effect = Exception("login failed")
        with patch("core.report.smtplib.SMTP_SSL", return_value=mock_server):
            result = _send_email(config, "subj", "<p>hi</p>")
        assert result is False
        mock_server.quit.assert_called()


# ── generate_report_html ─────────────────────────────────────────────────────


class TestGenerateReportHtml:
    def test_returns_empty_when_no_runs(self):
        mock_db = MagicMock()
        mock_db.get_runs_since.return_value = []
        result = generate_report_html(mock_db, "TestFirm", 7, "Weekly")
        assert result == ""

    def test_returns_html_with_runs(self):
        mock_db = MagicMock()
        mock_db.get_runs_since.return_value = [
            {
                "started_at": "2026-06-24T10:00:00+05:30",
                "mode": "cloud",
                "status": "CLOUD_COMPLETE",
                "files_copied": 100,
                "bytes_copied": 1024,
            },
            {
                "started_at": "2026-06-23T10:00:00+05:30",
                "mode": "lan",
                "status": "LAN_COMPLETE",
                "files_copied": 50,
                "bytes_copied": 2048,
            },
        ]
        with patch("core.report.now_formatted", return_value="2026-06-24 10:00 IST"):
            result = generate_report_html(mock_db, "TestFirm", 7, "Weekly")
        assert "<html>" in result
        assert "TestFirm" in result
        assert "Weekly" in result
        assert "2" in result  # total runs

    def test_counts_partials_correctly(self):
        mock_db = MagicMock()
        mock_db.get_runs_since.return_value = [
            {"started_at": "2026-06-24T10:00:00+05:30", "mode": "cloud", "status": "CLOUD_COMPLETE", "files_copied": 10, "bytes_copied": 100},
            {"started_at": "2026-06-23T10:00:00+05:30", "mode": "cloud", "status": "CLOUD_PARTIAL", "files_copied": 5, "bytes_copied": 50},
            {"started_at": "2026-06-22T10:00:00+05:30", "mode": "lan", "status": "FAILED", "files_copied": 0, "bytes_copied": 0},
        ]
        with patch("core.report.now_formatted", return_value="2026-06-24 10:00 IST"):
            result = generate_report_html(mock_db, "F", 7, "W")
        assert "1" in result  # 1 success
        assert "Partial" in result

    def test_handles_none_files_copied(self):
        mock_db = MagicMock()
        mock_db.get_runs_since.return_value = [
            {"started_at": "2026-06-24T10:00:00+05:30", "mode": "cloud", "status": "CLOUD_COMPLETE", "files_copied": None, "bytes_copied": None},
        ]
        with patch("core.report.now_formatted", return_value="2026-06-24 10:00 IST"):
            result = generate_report_html(mock_db, "F", 7, "W")
        assert "<html>" in result

    def test_shows_all_runs_in_period(self):
        mock_db = MagicMock()
        mock_db.get_runs_since.return_value = [
            {"started_at": f"2026-06-{24-i:02d}T10:00:00+05:30", "mode": "cloud", "status": "CLOUD_COMPLETE", "files_copied": 1, "bytes_copied": 1}
            for i in range(15)
        ]
        with patch("core.report.now_formatted", return_value="2026-06-24 10:00 IST"):
            result = generate_report_html(mock_db, "F", 30, "Monthly")
        # All 15 runs should be shown — no hard cap.
        runs_section = result.split("Recent Backups")[1]
        data_rows = runs_section.count("<td>") // 6  # 6 <td> per row
        assert data_rows == 15
        assert "All 15 backups shown" in result


# ── send_failure_alert ───────────────────────────────────────────────────────


class TestSendFailureAlert:
    def test_skips_when_disabled(self):
        config = _make_config(send_on_failure=False)
        result = send_failure_alert(config, "firm", "err", {"mode": "cloud"})
        assert result is False

    def test_sends_when_enabled(self):
        config = _make_config(send_on_failure=True)
        with patch("core.report._send_email", return_value=True) as mock_send:
            result = send_failure_alert(config, "MyFirm", "boom", {"mode": "cloud"}, timestamp="2026-06-24T10:00:00Z")
        assert result is True
        call_args = mock_send.call_args
        assert "Failure" in call_args[0][1]
        assert "MyFirm" in call_args[0][1]
        assert "boom" in call_args[1] or "boom" in str(call_args)


# ── send_weekly_report ───────────────────────────────────────────────────────


class TestSendWeeklyReport:
    def test_delegates_to_summary_report(self):
        mock_db = MagicMock()
        config = _make_config()
        with patch("core.report.send_summary_report", return_value=True) as mock_send:
            result = send_weekly_report(mock_db, config, "Firm")
        assert result is True
        mock_send.assert_called_once_with(mock_db, config, "Firm", 7, "Weekly", body_html=None)

    def test_passes_body_html(self):
        mock_db = MagicMock()
        config = _make_config()
        with patch("core.report.send_summary_report", return_value=True) as mock_send:
            send_weekly_report(mock_db, config, "Firm", body_html="<p>custom</p>")
        call_kwargs = mock_send.call_args
        assert call_kwargs[1]["body_html"] == "<p>custom</p>"


# ── send_monthly_report ──────────────────────────────────────────────────────


class TestSendMonthlyReport:
    def test_delegates_to_summary_report(self):
        mock_db = MagicMock()
        config = _make_config()
        with patch("core.report.send_summary_report", return_value=True) as mock_send:
            result = send_monthly_report(mock_db, config, "Firm")
        assert result is True
        mock_send.assert_called_once_with(mock_db, config, "Firm", 30, "Monthly", body_html=None)


# ── send_summary_report ──────────────────────────────────────────────────────


class TestSendSummaryReport:
    def test_returns_false_when_no_runs(self):
        mock_db = MagicMock()
        mock_db.get_runs_since.return_value = []
        config = _make_config()
        with patch("core.report.generate_report_html", return_value=""):
            result = send_summary_report(mock_db, config, "Firm", 7, "Weekly")
        assert result is False

    def test_sends_email_with_body(self):
        mock_db = MagicMock()
        config = _make_config()
        with (
            patch("core.report.generate_report_html", return_value="<p>report</p>"),
            patch("core.report._send_email", return_value=True) as mock_send,
        ):
            result = send_summary_report(mock_db, config, "Firm", 7, "Weekly")
        assert result is True
        assert "Weekly" in mock_send.call_args[0][1]

    def test_uses_provided_body_html(self):
        mock_db = MagicMock()
        config = _make_config()
        with patch("core.report._send_email", return_value=True) as mock_send:
            result = send_summary_report(mock_db, config, "Firm", 7, "Weekly", body_html="<p>override</p>")
        assert result is True
        assert "<p>override</p>" in str(mock_send.call_args)
