"""F4: RecipientsPartlyRefused records SENT_PARTIAL, feeds cooldown, and retries 4xx per-recipient."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from core import notify
from core.manifest import ManifestDB
from models.config import load_config


def test_f4_partly_refused_5xx_is_sent_partial_and_feeds_cooldown(tmp_path):
    cfg = load_config()
    cfg.notifications.smtp_host = "smtp.example.com"
    cfg.notifications.smtp_auth = False
    cfg.notifications.sender = "aam@example.com"
    cfg.notifications.recipients = ["good@example.com", "bad@example.com"]
    cfg.notifications.send_on_failure = True
    cfg.notifications.retry_attempts = 3
    cfg.notifications.retry_base_seconds = 0.01

    db = ManifestDB(tmp_path / "m.db")
    try:
        server = MagicMock()
        server.esmtp_features = {}
        server.sendmail.return_value = {"bad@example.com": (550, b"User unknown")}

        @contextmanager
        def fake_session(_n):
            yield server

        ev = notify.AlertEvent(
            key="backup:lan:LAN_FAILED",
            kind="FAILURE",
            mode="lan",
            title="LAN failed",
            message="Test",
            status="LAN_FAILED",
            fault_code="AAM-LAN-01",
        )
        with patch.object(notify, "_smtp_session", fake_session):
            r1 = notify.notify(cfg, ev, db=db)
            r2 = notify.notify(cfg, ev, db=db)

        assert r1.status == notify.SENT_PARTIAL
        assert server.sendmail.call_count == 1
        assert r2.status == notify.SUPPRESSED
    finally:
        db.close()


def test_f4_partly_refused_4xx_retries_only_refused_recipient():
    cfg = load_config()
    cfg.notifications.smtp_host = "smtp.example.com"
    cfg.notifications.smtp_auth = False
    cfg.notifications.sender = "aam@example.com"
    cfg.notifications.recipients = ["good@example.com", "temp@example.com"]
    cfg.notifications.retry_attempts = 3
    cfg.notifications.retry_base_seconds = 0.01

    calls = []

    def fake_sendmail(_sender, recips, _msg):
        calls.append(list(recips))
        if len(calls) == 1:
            return {"temp@example.com": (451, b"Try again later")}
        return {}

    server = MagicMock()
    server.esmtp_features = {}
    server.sendmail.side_effect = fake_sendmail

    @contextmanager
    def fake_session(_n):
        yield server

    with patch.object(notify, "_smtp_session", fake_session):
        res = notify.send_email(cfg.notifications, "subj", "body", audience=notify.AUDIENCE_ALERTS)

    assert res.status == notify.SENT
    assert calls == [["good@example.com", "temp@example.com"], ["temp@example.com"]]
