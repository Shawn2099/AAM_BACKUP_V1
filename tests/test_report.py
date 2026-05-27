"""Tests for report — email sending and formatting."""

from unittest.mock import MagicMock, patch

from core.report import _human_bytes, _send_email, send_failure_alert
from models.config import NotificationConfig


class TestHumanBytes:
    def test_bytes(self):
        assert _human_bytes(500) == "500.0 B"

    def test_kb(self):
        assert _human_bytes(2048) == "2.0 KB"

    def test_mb(self):
        assert _human_bytes(5 * 1024 * 1024) == "5.0 MB"

    def test_gb(self):
        assert _human_bytes(3 * 1024**3) == "3.0 GB"

    def test_tb(self):
        assert _human_bytes(2 * 1024**4) == "2.0 TB"

    def test_zero(self):
        assert _human_bytes(0) == "0.0 B"


class TestSendEmail:
    def test_skips_when_no_smtp_host(self):
        cfg = NotificationConfig(smtp_host="")
        assert _send_email(cfg, "Subject", "<p>body</p>") is False

    def test_skips_when_no_credentials(self):
        cfg = NotificationConfig(
            smtp_host="smtp.example.com",
            smtp_port=587,
            sender="from@example.com",
            recipients=["to@example.com"],
            smtp_username="",
            smtp_password="",
        )
        assert _send_email(cfg, "Subject", "<p>body</p>") is False

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
            assert _send_email(cfg, "Subject", "<p>body</p>") is True
            mock_server.login.assert_called_once_with("user", "pass")
            mock_server.sendmail.assert_called_once()
            mock_server.quit.assert_called_once()

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
            assert _send_email(cfg, "Subject", "<p>body</p>") is True
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
            assert _send_email(cfg, "Subject", "<p>body</p>") is False
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
                cfg, "TestFirm", "disk full", {"mode": "cloud", "run_id": "r-001"}
            )
            assert result is True
            mock_server.sendmail.assert_called_once()
