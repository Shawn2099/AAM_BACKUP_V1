"""opus-decision L1(c): a RECOVERED mail after a failure alert that was never delivered is not suppressed.

Live-stack finding (LIVE_RESULTS): SENT RESOLVED, then a FAILED failure row (SMTP down), then the problem
ends. The new RECOVERED looked like a repeat of the earlier RESOLVED and was suppressed, so nobody ever heard
about the problem. It must go out and say that the failure alert was lost."""
from __future__ import annotations

import pendulum
import pytest

from core import notify as nt
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import AppConfig

NOW = pendulum.datetime(2026, 11, 23, 12, 0, tz=IST)


@pytest.fixture(autouse=True)
def _frozen():
    with pendulum.travel_to(NOW, freeze=True):
        yield


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _cfg(tmp_path) -> AppConfig:
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": "\\\\nas\\share\\FY26-27",
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": 1, "sender": "b@x.example",
                       "recipients": ["o@x.example"], "smtp_security": "none", "smtp_auth": False,
                       "retry_attempts": 1, "retry_base_seconds": 0.01},
    )


def _row(db, key, kind, status, minutes_ago, subject="s", mode="lan"):
    db.record_notification({
        "event_key": key, "kind": kind, "mode": mode, "subject": subject, "status": status,
        "created_at": NOW.subtract(minutes=minutes_ago).isoformat(), "fault_code": "AAM-LAN-01",
        "message_id": f"<{key}-{status}-{minutes_ago}>", "thread_id": None, "audience": "alerts"})


def _recovered():
    return nt.AlertEvent(key="recovered:lan", kind="RECOVERED", mode="lan", title="t", message="m")


def _capture(monkeypatch):
    sent = []

    def fake_send(notifications, subject, body, **kw):
        sent.append((subject, body))
        return nt.DeliveryResult(nt.SENT, attempts=1, message_id="<new>")
    monkeypatch.setattr(nt, "send_email", fake_send)
    return sent


def test_recovered_after_an_undelivered_failure_is_sent_and_says_it_was_lost(tmp_path, db, monkeypatch):
    sent = _capture(monkeypatch)
    _row(db, "recovered:lan", "RECOVERED", "SENT", 600)                       # an earlier incident ended
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "FAILED", 120, subject="LAN backup failed (lost)")
    result = nt.notify(_cfg(tmp_path), _recovered(), db=db)
    assert result.status == nt.SENT, result
    assert len(sent) == 1
    body = sent[0][1]
    assert "could not be delivered" in body and "LAN backup failed (lost)" in body


def test_recovered_with_a_delivered_history_stays_suppressed(tmp_path, db, monkeypatch):
    """The existing cooldown behaviour (SENT-only history) is unchanged."""
    sent = _capture(monkeypatch)
    _row(db, "recovered:lan", "RECOVERED", "SENT", 60)
    result = nt.notify(_cfg(tmp_path), _recovered(), db=db)
    assert result.status == nt.SUPPRESSED
    assert sent == []


def test_a_failed_row_of_another_leg_does_not_unsuppress(tmp_path, db, monkeypatch):
    sent = _capture(monkeypatch)
    _row(db, "recovered:lan", "RECOVERED", "SENT", 600)
    _row(db, "backup:cloud:CLOUD_FAILED", "FAILURE", "FAILED", 120, mode="cloud")
    result = nt.notify(_cfg(tmp_path), _recovered(), db=db)
    assert result.status == nt.SUPPRESSED and sent == []


def test_a_failure_that_was_delivered_does_not_get_the_lost_line(tmp_path, db, monkeypatch):
    sent = _capture(monkeypatch)
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "FAILED", 200, subject="first, lost")
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "SENT", 100, subject="second, delivered")
    result = nt.notify(_cfg(tmp_path), _recovered(), db=db)
    assert result.status == nt.SENT
    assert "could not be delivered" not in sent[0][1]
