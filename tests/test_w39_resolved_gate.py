"""W39 (owner decision 2026-11-23): a normal recovery sends NO 'working again' mail; it is written to the audit
log (notifications row, status LOGGED_ONLY). A RESOLVED mail is sent only when the problem mail was DELIVERED and
the problem lasted at least alerts.resolved_mail_min_hours. A recovery after an UNDELIVERED failure still mails
(the L1 'alert was lost' rule). alerts.send_resolved=false = never any RESOLVED mail."""
from __future__ import annotations

import pendulum
import pytest

from core import notify as nt
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import AlertsConfig, AppConfig, model_default

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


def _cfg(tmp_path, **alerts) -> AppConfig:
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": "\\\\nas\\share\\FY26-27",
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False}, alerts=alerts,
        notifications={"smtp_host": "127.0.0.1", "smtp_port": 1, "sender": "b@x.example",
                       "recipients": ["o@x.example"], "smtp_security": "none", "smtp_auth": False,
                       "retry_attempts": 1, "retry_base_seconds": 0.01},
    )


def _row(db, key, kind, status, hours_ago, mode="lan"):
    db.record_notification({
        "event_key": key, "kind": kind, "mode": mode, "subject": "s", "status": status,
        "created_at": NOW.subtract(hours=hours_ago).isoformat(), "fault_code": "AAM-LAN-01",
        "message_id": f"<{key}-{status}-{hours_ago}>", "thread_id": None, "audience": "alerts",
        "recipients": ["o@x.example"]})


def _recovered(mode="lan"):
    return nt.AlertEvent(key=f"recovered:{mode}", kind="RECOVERED", mode=mode,
                         title="LAN backup recovered", message="m")


def _capture(monkeypatch):
    sent = []

    def fake_send(notifications, subject, body, **kw):
        sent.append(subject)
        return nt.DeliveryResult(nt.SENT, attempts=1, message_id="<new>", recipients=["o@x.example"])
    monkeypatch.setattr(nt, "send_email", fake_send)
    return sent


def test_default_and_bounds():
    assert model_default(AlertsConfig, "resolved_mail_min_hours") == 24
    AlertsConfig(resolved_mail_min_hours=1)
    AlertsConfig(resolved_mail_min_hours=720)
    for bad in (0, 721):
        with pytest.raises(Exception):
            AlertsConfig(resolved_mail_min_hours=bad)


def test_quick_recovery_of_a_delivered_failure_sends_no_mail_and_logs(tmp_path, db, monkeypatch):
    sent = _capture(monkeypatch)
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "SENT", 2)
    result = nt.notify(_cfg(tmp_path), _recovered(), db=db)
    assert result.status == nt.LOGGED_ONLY
    assert sent == []
    row = db.last_notification("recovered:lan")
    assert row["status"] == "LOGGED_ONLY" and row["kind"] == "RECOVERED"
    assert "recovered after 2h" in row["error"]


def test_recovery_after_min_hours_with_a_delivered_failure_sends_one_resolved_mail(tmp_path, db, monkeypatch):
    sent = _capture(monkeypatch)
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "SENT", 30)
    result = nt.notify(_cfg(tmp_path), _recovered(), db=db)
    assert result.status == nt.SENT and len(sent) == 1


def test_boundary_exactly_min_hours_sends_just_under_does_not(tmp_path, db, monkeypatch):
    sent = _capture(monkeypatch)
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "SENT", 24)
    assert nt.notify(_cfg(tmp_path), _recovered(), db=db).status == nt.SENT
    sent.clear()
    _row(db, "backup:cloud:CLOUD_FAILED", "FAILURE", "SENT", 23.99, mode="cloud")
    assert nt.notify(_cfg(tmp_path), _recovered("cloud"), db=db).status == nt.LOGGED_ONLY
    assert sent == []


def test_min_hours_is_configurable(tmp_path, db, monkeypatch):
    sent = _capture(monkeypatch)
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "SENT", 3)
    assert nt.notify(_cfg(tmp_path, resolved_mail_min_hours=2), _recovered(), db=db).status == nt.SENT
    assert len(sent) == 1


def test_recovery_after_an_undelivered_failure_still_mails_the_lost_alert_notice(tmp_path, db, monkeypatch):
    sent = _capture(monkeypatch)
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "FAILED", 1)          # short, but the person never heard
    result = nt.notify(_cfg(tmp_path), _recovered(), db=db)
    assert result.status == nt.SENT and len(sent) == 1


def test_send_resolved_false_never_mails(tmp_path, db, monkeypatch):
    sent = _capture(monkeypatch)
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "SENT", 100)
    result = nt.notify(_cfg(tmp_path, send_resolved=False), _recovered(), db=db)
    assert result.status != nt.SENT and sent == []


def test_recovery_with_no_delivered_or_lost_problem_mail_is_logged_only(tmp_path, db, monkeypatch):
    sent = _capture(monkeypatch)
    result = nt.notify(_cfg(tmp_path), _recovered(), db=db)
    assert result.status == nt.LOGGED_ONLY and sent == []


def test_a_logged_only_recovery_closes_the_incident(tmp_path, db, monkeypatch):
    """A later problem is a new incident: a quick recovery is measured from THAT problem, not the first."""
    sent = _capture(monkeypatch)
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "SENT", 60)
    _row(db, "recovered:lan", "RECOVERED", "LOGGED_ONLY", 50)
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "SENT", 1)
    assert nt.notify(_cfg(tmp_path), _recovered(), db=db).status == nt.LOGGED_ONLY
    assert sent == []
