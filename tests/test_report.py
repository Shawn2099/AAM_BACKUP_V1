"""Tests for report — email sending and formatting."""

import datetime
import ipaddress
import socket
import ssl
import threading
import time
from unittest.mock import MagicMock, patch

import humanize
import pytest

from core.report import _send_email_with_attachments, generate_report_html, send_failure_alert
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
        # smtplib contract: sendmail returns {refused_recipient: (code, msg)}
        # — an empty dict when every recipient was accepted.
        mock_server.sendmail.return_value = {}
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
        mock_server.sendmail.return_value = {}  # all recipients accepted
        with patch("core.report.smtplib.SMTP_SSL", return_value=mock_server):
            assert _send_email_with_attachments(cfg, "Subject", "<p>body</p>") is True
            mock_server.login.assert_called_once_with("user", "pass")

    @patch("core.report.time.sleep")
    def test_quits_on_sendmail_failure(self, mock_sleep):
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
            assert mock_server.quit.call_count == 3


# ── M4/S2-11: partial-recipient refusal must not report success ────────────
#
# smtplib.sendmail() raises SMTPRecipientsRefused only when ALL recipients
# are refused; a PARTIAL refusal (one bad address among good ones) returns
# {recipient: (code, msg)} and — pre-fix — was ignored: the function logged
# "Email sent" and returned True while the refused recipient(s) got nothing.
# Session-2 experiment E8 reproduced this against the real code using a
# local STARTTLS server (scratch_q_e8.py); this class re-runs it under pytest.


@pytest.fixture
def local_smtp(tmp_path, monkeypatch):
    """Local STARTTLS SMTP server on 127.0.0.1 (advertises STARTTLS + AUTH
    PLAIN like Gmail 587). Which recipients are refused is set per-test via
    state['refused'] before the send. Yields (port, state)."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    # ── self-signed cert for 127.0.0.1 ──
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = tmp_path / "server.key"
    cert_pem = tmp_path / "server.pem"
    key_pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    # free port
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    state = {"refused": set(), "events": []}

    def serve():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_pem), str(key_pem))
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(60)
        try:
            conn, _ = srv.accept()
        except Exception:
            return
        f = conn.makefile("rb")

        def send(line):
            try:
                conn.sendall(line.encode() + b"\r\n")
            except Exception:
                pass

        in_data = False
        tls_done = False
        send("220 local.test ESMTP")
        while True:
            try:
                raw = f.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if in_data:
                if line == ".":
                    in_data = False
                    send("250 OK accepted")
                continue
            up = line.upper()
            if up.startswith("EHLO"):
                if not tls_done:
                    send("250-local.test")
                    send("250-STARTTLS")
                    send("250-AUTH PLAIN LOGIN")
                else:
                    send("250-local.test")
                    send("250-AUTH PLAIN LOGIN")
                send("250 OK")
            elif up.startswith("AUTH PLAIN"):
                send("235 Authentication successful")
            elif up == "STARTTLS":
                send("220 Ready to start TLS")
                conn = ctx.wrap_socket(conn, server_side=True)
                f = conn.makefile("rb")
                tls_done = True
            elif up.startswith("HELO"):
                send("250 local.test")
            elif up.startswith("MAIL FROM"):
                send("250 OK")
            elif up.startswith("RCPT TO"):
                addr = line.split("<")[-1].rstrip(">").strip()
                if addr in state["refused"]:
                    state["events"].append(("refused", addr))
                    send("550 5.1.1 User unknown (test refusal)")
                else:
                    state["events"].append(("ok", addr))
                    send("250 OK")
            elif up.startswith("DATA"):
                in_data = True
                send("354 End data with <CR><LF>.<CR><LF>")
            elif up.startswith("QUIT"):
                send("221 Bye")
                break
            else:
                send("250 OK")
        try:
            conn.close()
        except Exception:
            pass
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    time.sleep(0.5)

    # Test scaffolding only: smtplib verifies the TLS certificate by default;
    # relax verification for THIS test process (local self-signed cert).
    # The code under test (core.report) is not modified.
    def _no_verify(purpose=ssl.Purpose.SERVER_AUTH):
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c

    monkeypatch.setattr(ssl, "_create_stdlib_context", _no_verify)
    yield port, state
    t.join(timeout=30)


class TestSmtpPartialRefusal:
    def test_partial_refusal_is_not_success(self, local_smtp, capture_logs):
        """M4: 1 of 2 recipients refused (550) → the function must return
        False (the alert channel degrades — callers log CRITICAL + annotate
        [ALERT_NOT_DELIVERED]) and name the refused address. Pre-fix it
        returned True and logged 'Email sent'."""
        port, state = local_smtp
        state["refused"] = {"bad@nowhere.invalid"}
        cfg = NotificationConfig(
            smtp_host="127.0.0.1",
            smtp_port=port,
            smtp_username="u",
            smtp_password="p",
            sender="from@example.com",
            recipients=["bad@nowhere.invalid", "good@example.com"],
        )
        ok = _send_email_with_attachments(cfg, "M4 test", "<p>body</p>")

        assert ok is False  # M4: pre-fix returned True (false success)
        # The server actually accepted the good recipient ...
        assert ("ok", "good@example.com") in state["events"]
        # ... and the log names the refused one
        logs = capture_logs.getvalue()
        assert "bad@nowhere.invalid" in logs
        assert "refus" in logs.lower()

    def test_full_delivery_still_success(self, local_smtp):
        port, state = local_smtp
        cfg = NotificationConfig(
            smtp_host="127.0.0.1",
            smtp_port=port,
            smtp_username="u",
            smtp_password="p",
            sender="from@example.com",
            recipients=["good@example.com"],
        )
        ok = _send_email_with_attachments(cfg, "M4 ok", "<p>body</p>")
        assert ok is True
        assert ("ok", "good@example.com") in state["events"]

    def test_all_refused_still_failure(self, local_smtp, capture_logs):
        """Existing behavior guard: total refusal raises
        SMTPRecipientsRefused → permanent-error path → False (no retry)."""
        port, state = local_smtp
        state["refused"] = {"bad1@x.invalid", "bad2@y.invalid"}
        cfg = NotificationConfig(
            smtp_host="127.0.0.1",
            smtp_port=port,
            smtp_username="u",
            smtp_password="p",
            sender="from@example.com",
            recipients=["bad1@x.invalid", "bad2@y.invalid"],
        )
        ok = _send_email_with_attachments(cfg, "M4 all bad", "<p>body</p>")
        assert ok is False
        assert "permanent" in capture_logs.getvalue().lower()


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
        mock_server.sendmail.return_value = {}  # all recipients accepted
        with patch("core.report.smtplib.SMTP", return_value=mock_server):
            result = send_failure_alert(
                cfg, "TestFirm", "disk full", {"mode": "cloud"}, timestamp="2026-05-27T10:00:00Z"
            )
            assert result is True
            mock_server.sendmail.assert_called_once()
