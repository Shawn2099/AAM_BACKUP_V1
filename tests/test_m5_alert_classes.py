"""Audit 2026-10-18 item 1 (docs/EMAIL_CONTENT_SPEC.md §2, §6): the
"Email me when something goes wrong" switch silences only routine failure /
warning emails. Emails that need a decision or warn that protection is lost
(blocked, overdue, safety-check differences, services down, no backups at all)
are always sent, the switch is an Advanced, confirmed setting, and the
dashboard shows amber while it is off."""

import pytest

import flow
from core import config_edit as ce
from core import notify as nt
from core.attention import AMBER, RED, compute_attention
from core.manifest import ManifestDB
from models.config import AppConfig
from test_support.smtp_server import smtp_server

UNC = "\\\\nas\\backup\\FY26-27"


def _cfg(tmp_path, port=25, send_on_failure=False) -> AppConfig:
    return AppConfig(
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": UNC,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080"},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@test.local",
                       "recipients": ["ops@test.local"], "smtp_security": "none",
                       "smtp_auth": False, "retry_attempts": 1, "retry_base_seconds": 0.01,
                       "smtp_timeout_seconds": 5, "send_on_failure": send_on_failure},
        alerts={"repeat_cooldown_hours": 12},
    )


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _event(kind, key="k"):
    return nt.AlertEvent(key=key, kind=kind, mode="lan", title="t", message="m")


@pytest.mark.parametrize("kind", ["ACTION_REQUIRED", "RECOVERED"])
def test_decision_and_recovered_emails_ignore_the_switch(tmp_path, db, kind):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1], send_on_failure=False)
        assert nt.notify(cfg, _event(kind), db=db).status == nt.SENT
    assert len(srv.messages) == 1


@pytest.mark.parametrize("kind", ["FAILURE", "WARNING"])
def test_routine_emails_obey_the_switch(tmp_path, db, kind):
    cfg = _cfg(tmp_path, send_on_failure=False)
    assert nt.notify(cfg, _event(kind), db=db).status == nt.DISABLED


@pytest.mark.parametrize("outcomes,kind", [
    (["lan=VERIFICATION_FAILED"], "ACTION_REQUIRED"),
    (["cloud=AUDIT_INCOMPLETE", "lan=VERIFICATION_FAILED"], "ACTION_REQUIRED"),
    (["lan=NOT_RECORDED"], "ACTION_REQUIRED"),          # unknown result: never routine
    (["cloud=AUDIT_INCOMPLETE"], "WARNING"),
    (["cloud=AUDIT_INCOMPLETE", "lan=AUDIT_INCOMPLETE"], "WARNING"),
])
def test_audit_differences_need_action_could_not_run_is_a_warning(outcomes, kind):
    assert flow._audit_alert_kind(outcomes) == kind


def test_no_backups_at_all_report_is_always_sent(tmp_path):
    """The report is not a routine alert: it goes out even with send_on_failure off, and an empty
    period says so (plan C P1)."""
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1], send_on_failure=False)
        flow._send_summary_report(cfg, days=7, period="Weekly")
    assert len(srv.messages) == 1
    d = ManifestDB.from_config(cfg)
    try:
        assert d.last_notification("report:weekly")["status"] == nt.SENT
    finally:
        d.close()


def test_switch_is_advanced_and_needs_confirmation():
    assert "notifications.send_on_failure" not in ce.BASIC_KEYS
    assert ce.is_risky("notifications.send_on_failure")


def test_dashboard_amber_while_routine_alerts_are_off(tmp_path, db):
    items = compute_attention(db, _cfg(tmp_path, send_on_failure=False))
    off = [i for i in items if i["code"] == "alerts_off"]
    assert len(off) == 1 and off[0]["level"] == AMBER
    assert not [i for i in compute_attention(db, _cfg(tmp_path, send_on_failure=True))
                if i["code"] == "alerts_off"]
    # amber only: /health stays 200 and the watchdog's stale check is untouched
    assert all(i["level"] != RED for i in off)
