"""Tests for report — email sending and formatting."""

from unittest.mock import MagicMock, patch

import humanize
from core.report import _send_email_with_attachments, send_failure_alert, generate_report_html
from models.config import NotificationConfig


class TestHumanBytes:
    def test_bytes(self):
        assert humanize.naturalsize(500, binary=True) == "500 Bytes"

    def test_kb(self):
        assert humanize.naturalsize(2048, binary=True) == "2.0 KiB"

    def test_mb(self):
        assert humanize.naturalsize(5 * 1024 * 1024, binary=True) == "5.0 MiB"

    def test_gb(self):
        assert humanize.naturalsize(3 * 1024**3, binary=True) == "3.0 GiB"

    def test_tb(self):
        assert humanize.naturalsize(2 * 1024**4, binary=True) == "2.0 TiB"

    def test_zero(self):
        assert humanize.naturalsize(0, binary=True) == "0 Bytes"


class TestSendEmail:
    def test_skips_when_no_smtp_host(self):
        cfg = NotificationConfig(smtp_host="")
        assert _send_email_with_attachments(cfg, "Subject", "<p>body</p>") is False

    def test_skips_when_no_credentials(self):
        cfg = NotificationConfig(
            smtp_host="smtp.example.com",
            smtp_port=587,
            sender="from@example.com",
            recipients=["to@example.com"],
            smtp_username="",
            smtp_password="",
        )
        assert _send_email_with_attachments(cfg, "Subject", "<p>body</p>") is False

    def test_sends_successfully_tls(self):
        cfg = NotificationConfig(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="user",
            smtp_password="pass",
            sender="from@example.com",
            recipients=["to@example.com"],
        )
        mock_server = MagicMock()
        with patch("core.report.smtplib.SMTP", return_value=mock_server):
            assert _send_email_with_attachments(cfg, "Subject", "<p>body</p>") is True
            mock_server.login.assert_called_once_with("user", "pass")
            mock_server.sendmail.assert_called_once()


class TestGenerateReportHtml:
    def test_empty_when_no_runs(self):
        db = MagicMock()
        db.get_runs_since.return_value = []
        assert generate_report_html(db, "TestFirm", 7, "Weekly") == ""

    def test_generates_html_with_runs(self):
        db = MagicMock()
        db.get_runs_since.return_value = [
            {"started_at": "2026-05-27T10:00:00Z", "mode": "cloud",
             "status": "CLOUD_COMPLETE", "files_copied": 42, "bytes_copied": 123456},
            {"started_at": "2026-05-28T10:00:00Z", "mode": "lan",
             "status": "LAN_PARTIAL", "files_copied": 30, "bytes_copied": 50000},
        ]
        html = generate_report_html(db, "TestFirm", 7, "Weekly")
        assert "TestFirm" in html
        assert "Weekly Backup Report" in html
        assert "Completed" in html
        assert "Partial" in html
        assert "42" in html
        assert "50.0%" in html  # 1 success out of 2 = 50%

    def test_sends_successfully_ssl(self):
        cfg = NotificationConfig(
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_username="user",
            smtp_password="pass",
            sender="from@example.com",
            recipients=["to@example.com"],
        )
        mock_server = MagicMock()
        with patch("core.report.smtplib.SMTP_SSL", return_value=mock_server):
            assert _send_email_with_attachments(cfg, "Subject", "<p>body</p>") is True
            mock_server.login.assert_called_once_with("user", "pass")

    def test_quits_on_sendmail_failure(self):
        cfg = NotificationConfig(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="user",
            smtp_password="pass",
            sender="from@example.com",
            recipients=["to@example.com"],
        )
        mock_server = MagicMock()
        mock_server.sendmail.side_effect = ConnectionError("timeout")
        with patch("core.report.smtplib.SMTP", return_value=mock_server):
            assert _send_email_with_attachments(cfg, "Subject", "<p>body</p>") is False
            mock_server.quit.assert_called_once()


class TestSendFailureAlert:
    def test_skips_when_send_on_failure_disabled(self):
        cfg = NotificationConfig(send_on_failure=False)
        assert send_failure_alert(cfg, "Firm", "error", {"mode": "cloud"}) is False

    def test_sends_when_enabled(self):
        cfg = NotificationConfig(
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="user",
            smtp_password="pass",
            sender="from@example.com",
            recipients=["to@example.com"],
            send_on_failure=True,
        )
        mock_server = MagicMock()
        with patch("core.report.smtplib.SMTP", return_value=mock_server):
            result = send_failure_alert(
                cfg, "TestFirm", "disk full", {"mode": "cloud"}, timestamp="2026-05-27T10:00:00Z"
            )
            assert result is True
            mock_server.sendmail.assert_called_once()
