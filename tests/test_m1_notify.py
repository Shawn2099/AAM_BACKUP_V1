"""M1 1.6 / plan §5.5 — single alert path: real SMTP transport, retry,
de-duplication, delivery recorded, dashboard link (DEC-3)."""

import time
from unittest.mock import patch

import pytest

from core import notify as nt
from core.manifest import ManifestDB
from models.config import AppConfig
from test_support.smtp_server import smtp_server

UNC = "\\\\nas\\backup\\FY26-27"


def _cfg(tmp_path, port=25, **notif) -> AppConfig:
    n = {"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "backup@test.local",
         "recipients": ["ops@test.local"], "smtp_security": "none", "smtp_auth": False,
         "retry_attempts": 3, "retry_base_seconds": 0.01, "smtp_timeout_seconds": 5,
         "send_on_failure": True}
    n.update(notif)
    return AppConfig(
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": UNC,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080"},
        notifications=n, alerts={"repeat_cooldown_hours": 12, "body_max_chars": 200},
    )


def _event(key="backup:lan:LAN_FAILED", kind="FAILURE", message="robocopy exit 16"):
    return nt.AlertEvent(key=key, kind=kind, mode="lan", title="LAN backup failed",
                         message=message, status="LAN_FAILED", exit_code=16)


def _decoded(raw: str) -> str:
    """Decode every MIME part (bodies are base64) into one searchable text."""
    import email

    msg = email.message_from_string(raw)
    parts = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True) or b""
        parts.append(payload.decode("utf-8", "replace"))
        if part.get_filename():
            parts.append(f"[attachment {part.get_filename()}]")
    return "\n".join(parts)


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def test_real_smtp_delivery_with_dashboard_link_and_record(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1])
        result = nt.notify(cfg, _event(), db=db)
    assert result.status == nt.SENT
    assert len(srv.messages) == 1
    data = _decoded(srv.messages[0]["data"])
    assert "http://backup-server:8080" in data and "Copy to the NAS failed" in data
    row = db.last_notification("backup:lan:LAN_FAILED")
    assert row["status"] == nt.SENT and row["kind"] == "FAILURE"


def test_repeat_within_cooldown_is_suppressed_and_recorded(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1])
        assert nt.notify(cfg, _event(), db=db).status == nt.SENT
        second = nt.notify(cfg, _event(), db=db)
        other = nt.notify(cfg, _event(key="backup:cloud:CLOUD_FAILED"), db=db)
    assert second.status == nt.SUPPRESSED and "cooldown" in second.error
    assert other.status == nt.SENT
    assert len(srv.messages) == 2
    assert db.last_notification("backup:lan:LAN_FAILED")["status"] == nt.SUPPRESSED


def test_transient_failure_retries_then_records_failed(tmp_path, db):
    with smtp_server(fail_with="451 try again later") as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1], retry_attempts=3)
        result = nt.notify(cfg, _event(), db=db)
    assert result.status == nt.FAILED and result.attempts == 3
    assert srv.mail_attempts == 3
    failures = db.notification_failures_since(days=1)
    assert len(failures) == 1 and failures[0]["event_key"] == "backup:lan:LAN_FAILED"


def test_transient_4xx_on_mail_is_retried_despite_sender_refused_type(tmp_path, db):
    """smtplib raises SMTPSenderRefused for 451 too; it must be retried."""
    import smtplib

    from core.notify import _is_permanent_smtp_error

    assert not _is_permanent_smtp_error(smtplib.SMTPSenderRefused(451, b"later", "a@b"))
    assert _is_permanent_smtp_error(smtplib.SMTPSenderRefused(550, b"no", "a@b"))
    assert not _is_permanent_smtp_error(
        smtplib.SMTPRecipientsRefused({"x@y": (450, b"busy")}))
    assert _is_permanent_smtp_error(smtplib.SMTPRecipientsRefused({"x@y": (550, b"no")}))
    assert _is_permanent_smtp_error(smtplib.SMTPAuthenticationError(535, b"bad creds"))
    assert not _is_permanent_smtp_error(ConnectionRefusedError())


def test_permanent_failure_is_not_retried(tmp_path, db):
    with smtp_server(fail_with="550 sender rejected") as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1], retry_attempts=5)
        result = nt.notify(cfg, _event(), db=db)
    assert result.status == nt.FAILED
    assert srv.mail_attempts == 1


def test_failed_send_does_not_start_cooldown(tmp_path, db):
    with smtp_server(fail_with="451 later") as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1], retry_attempts=1)
        assert nt.notify(cfg, _event(), db=db).status == nt.FAILED
    with smtp_server() as srv2:
        cfg = _cfg(tmp_path, port=srv2.server_address[1])
        assert nt.notify(cfg, _event(), db=db).status == nt.SENT


def test_misconfigured_email_is_a_recorded_failure_not_a_silent_skip(tmp_path, db):
    cfg = _cfg(tmp_path, smtp_host="")
    result = nt.notify(cfg, _event(), db=db)
    assert result.status == nt.FAILED and "smtp_host" in result.error
    assert db.notification_failures_since(days=1)


def test_auth_required_without_credentials_fails_loudly(tmp_path, db):
    cfg = _cfg(tmp_path, smtp_auth=True, smtp_username="", smtp_password="")
    result = nt.notify(cfg, _event(), db=db)
    assert result.status == nt.FAILED and "smtp_auth" in result.error


def test_disabled_alerts_are_recorded_as_disabled(tmp_path, db):
    cfg = _cfg(tmp_path, send_on_failure=False)
    assert nt.notify(cfg, _event(), db=db).status == nt.DISABLED
    assert db.last_notification("backup:lan:LAN_FAILED")["status"] == nt.DISABLED


def test_long_message_truncated_in_body_and_attached_in_full(tmp_path, db):
    long = "E" * 5000
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1])
        nt.notify(cfg, _event(message=long), db=db)
    data = _decoded(srv.messages[0]["data"])
    # audit 2026-10-18: the full text is in the support report attachment
    assert "truncated" in data and "_AAM-LAN-01.txt]" in data and "[attachment AAM-support_" in data
    assert "E" * 5000 in data


def test_notify_never_raises(tmp_path):
    cfg = _cfg(tmp_path)        # no mail server listens on the default port
    with patch.object(nt, "_notify", side_effect=RuntimeError("db exploded")):
        result = nt.notify(cfg, _event())
    # D1: a broken alert pipeline now falls back to a short notice; with no mail server that fails
    # too - and still never raises
    assert result.status == nt.FAILED


def test_ssl_auto_mode_follows_port():
    class N:
        smtp_security = "auto"
        smtp_port = 465
    assert nt._smtp_security(N) == "ssl"
    N.smtp_port = 587
    assert nt._smtp_security(N) == "starttls"


def test_dashboard_url_default_from_bind_and_port(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.dashboard.public_url = ""
    assert nt.dashboard_url(cfg) == "http://127.0.0.1:8080"
