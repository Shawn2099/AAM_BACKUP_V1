"""A switch per alert class (plan docs/PLAN_ALERT_CONTROLS_2026-10-22.md, step C).

Everything stays ON by default (today's behaviour). Switching a class off records its mails as DISABLED, shows
an amber dashboard note and a line in the weekly / monthly report; the password-changed notice and the test
email have no switch."""
from __future__ import annotations

import pytest

from core import alert_class as ac
from core import config_edit as ce
from core import notify as nt
from core.attention import AMBER, RED, compute_attention
from core.manifest import ManifestDB
from models.config import AlertsConfig, AppConfig
from test_support import ui_login
from test_support.smtp_server import smtp_server

SWITCHES = {
    ac.DECISION: "send_decision_needed", ac.OVERDUE: "send_overdue", ac.ACTION: "send_action_needed",
    ac.RESOLVED: "send_resolved", ac.INFO: "send_info",
}
# one event per class: (kind, key)
EVENTS = {
    ac.FAILURE: ("FAILURE", "backup:lan:LAN_FAILED"),
    ac.DECISION: ("ACTION_REQUIRED", "blocked:lan"),
    ac.OVERDUE: ("ACTION_REQUIRED", "stale:lan"),
    ac.ACTION: ("ACTION_REQUIRED", "rollover:clock_behind"),
    ac.RESOLVED: ("RECOVERED", "recovered:lan"),
    ac.INFO: ("INFO", "rollover:done:FY27-28"),
    ac.SECURITY: ("INFO", "password:admin"),
    ac.TEST: ("INFO", "test:now"),
}


def _cfg(tmp_path, port=25, send_on_failure=True, **alerts) -> AppConfig:
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path), "lan_destination": "\\\\nas\\share",
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@firm.example",
                       "recipients": ["ops@firm.example"], "smtp_security": "none", "smtp_auth": False,
                       "retry_attempts": 1, "retry_base_seconds": 0.01, "send_on_failure": send_on_failure},
        alerts=alerts)


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _event(cls):
    kind, key = EVENTS[cls]
    return nt.AlertEvent(key=key, kind=kind, mode="lan", title="t", message="m", fault_code="AAM-SYS-03")


def test_every_switch_defaults_to_on():
    a = AlertsConfig()
    for name in SWITCHES.values():
        assert getattr(a, name) is True, name


@pytest.mark.parametrize("cls", sorted(SWITCHES))
def test_switching_a_class_off_disables_only_that_class(tmp_path, db, cls):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], **{SWITCHES[cls]: False})
        off = nt.notify(cfg, _event(cls), db=db)
        assert off.status == nt.DISABLED and SWITCHES[cls] in off.error
        assert srv.messages == []
        for other in (ac.FAILURE, *[c for c in SWITCHES if c != cls]):
            if other == ac.RESOLVED:
                continue                      # a RESOLVED needs an open incident (step E)
            assert nt.notify(cfg, _event(other), db=db).status == nt.SENT, other


def test_the_failure_switch_is_still_notifications_send_on_failure(tmp_path, db):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], send_on_failure=False)
        assert nt.notify(cfg, _event(ac.FAILURE), db=db).status == nt.DISABLED
        assert nt.notify(cfg, _event(ac.DECISION), db=db).status == nt.SENT


def test_security_and_test_mails_have_no_switch(tmp_path, db):
    every_switch_off = {name: False for name in SWITCHES.values()}
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], send_on_failure=False, **every_switch_off)
        assert nt.notify(cfg, _event(ac.SECURITY), db=db).status == nt.SENT
        assert nt.notify(cfg, _event(ac.TEST), db=db).status == nt.SENT


def test_the_short_fallback_notice_obeys_the_switches_too(tmp_path):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1], send_on_failure=True, send_overdue=False)
        off = nt._fallback_alert(cfg, _event(ac.OVERDUE), RuntimeError("x"))
        on = nt._fallback_alert(cfg, _event(ac.DECISION), RuntimeError("x"))
    assert off.status == nt.DISABLED and on.status == nt.SENT


def test_the_switches_are_advanced_keys_that_need_the_typed_phrase():
    for name in SWITCHES.values():
        key = f"alerts.{name}"
        assert key not in ce.BASIC_KEYS and ce.is_risky(key), key


# ── dashboard ────────────────────────────────────────────────────────────────

def _codes(items, level=None):
    return {i["code"] for i in items if level is None or i["level"] == level}


def test_nothing_is_flagged_while_every_class_is_on(tmp_path, db):
    assert not [c for c in _codes(compute_attention(db, _cfg(tmp_path))) if c.startswith("alerts_off")]


@pytest.mark.parametrize("cls", sorted(SWITCHES))
def test_each_class_that_is_off_gets_its_own_amber_note(tmp_path, db, cls):
    items = compute_attention(db, _cfg(tmp_path, **{SWITCHES[cls]: False}))
    mine = [i for i in items if i["code"] == f"alerts_off:{cls}"]
    assert len(mine) == 1 and mine[0]["level"] == AMBER
    assert not _codes(items, RED) & {f"alerts_off:{cls}"}


@pytest.mark.parametrize("cls", [ac.DECISION, ac.OVERDUE, ac.ACTION, ac.RESOLVED])
def test_the_notes_for_the_important_classes_say_to_watch_the_dashboard(tmp_path, db, cls):
    item = next(i for i in compute_attention(db, _cfg(tmp_path, **{SWITCHES[cls]: False}))
                if i["code"] == f"alerts_off:{cls}")
    assert "dashboard" in item["message"].lower()


def test_the_failure_note_no_longer_claims_other_mails_are_still_sent(tmp_path, db):
    [item] = [i for i in compute_attention(db, _cfg(tmp_path, send_on_failure=False))
              if i["code"] == "alerts_off"]
    assert "still emailed" not in item["message"]


def test_the_report_says_which_classes_are_off(tmp_path):
    from core import report
    from core.report_period import ReportPeriod
    import pendulum
    from core.time_utils import IST

    rp = ReportPeriod(pendulum.datetime(2026, 10, 12, tz=IST), pendulum.datetime(2026, 10, 19, tz=IST),
                      "12-18 Oct 2026", "Weekly")
    off = ac.switched_off(_cfg(tmp_path, send_overdue=False, send_resolved=False))
    assert off == [ac.OVERDUE, ac.RESOLVED]
    model = report.build_report_model(ManifestDB(str(tmp_path / "r.db")), rp, ("lan",), alerts_off=off)
    html = report.render_report_html(model, "Firm", is_email=True, name="Weekly")
    assert "switched off" in html.lower() and "overdue" in html.lower()
    clean = report.render_report_html(
        report.build_report_model(ManifestDB(str(tmp_path / "r2.db")), rp, ("lan",), alerts_off=[]),
        "Firm", is_email=True, name="Weekly")
    assert "switched off" not in clean.lower()
