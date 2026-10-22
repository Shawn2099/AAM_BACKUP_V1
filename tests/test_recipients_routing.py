"""Two recipient lists - routing (plan docs/PLAN_RECIPIENTS_AND_SCHEDULE_ALERTS_2026-10-22.md, A2).

Checked on the wire with the real SMTP test server: the SMTP envelope AND the To header carry the
list of the email's audience; an empty list uses the other one; both empty is a recorded failure; a
refused address is a failed delivery that names it."""
from __future__ import annotations

import email
from unittest.mock import MagicMock

import pytest

from core import notify as nt
from core.manifest import ManifestDB
from models.config import AlertsConfig, NotificationConfig
from test_support.smtp_server import smtp_server

ALERTS = ["ops@firm.example", "both@firm.example"]
REPORTS = ["boss@firm.example", "both@firm.example"]


def _notif(port, **kw) -> NotificationConfig:
    base = dict(smtp_host="127.0.0.1", smtp_port=port, sender="backup@firm.example",
                recipients=ALERTS, report_recipients=REPORTS, smtp_security="none",
                smtp_auth=False, retry_attempts=1, retry_base_seconds=0.01, smtp_timeout_seconds=5)
    base.update(kw)
    return NotificationConfig(**base)


def _envelope(srv) -> list[str]:
    [msg] = srv.messages
    return sorted(r.split("<", 1)[1].split(">", 1)[0] for r in msg["rcpts"])


def _to_header(srv) -> list[str]:
    [msg] = srv.messages
    return sorted(a.strip() for a in email.message_from_string(msg["data"])["To"].split(","))


# ── resolve_recipients ───────────────────────────────────────────────────────

@pytest.mark.parametrize("audience,alerts,reports,expected,note", [
    ("alerts", ["a@x.com"], ["r@x.com"], ["a@x.com"], None),
    ("reports", ["a@x.com"], ["r@x.com"], ["r@x.com"], None),
    ("alerts", [], ["r@x.com"], ["r@x.com"], "alerts list is empty"),
    ("reports", ["a@x.com"], [], ["a@x.com"], "reports list is empty"),
    ("both", ["a@x.com", "S@x.com"], ["r@x.com", "s@X.com"], ["a@x.com", "S@x.com", "r@x.com"], None),
    ("both", [], ["r@x.com"], ["r@x.com"], None),
    ("alerts", [], [], [], "no recipients"),
    ("reports", [], [], [], "no recipients"),
    ("both", [], [], [], "no recipients"),
])
def test_resolve_recipients_table(audience, alerts, reports, expected, note):
    n = NotificationConfig(recipients=alerts, report_recipients=reports)
    got = nt.resolve_recipients(n, audience)
    assert got.addresses == expected
    if note is None:
        assert got.note is None
    else:
        assert note in got.note


def test_unknown_audience_is_a_programming_error():
    with pytest.raises(ValueError):
        nt.resolve_recipients(NotificationConfig(), "everybody")


# ── on the wire ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("audience,expected", [
    ("alerts", sorted(ALERTS)),
    ("reports", sorted(REPORTS)),
    ("both", sorted({"ops@firm.example", "both@firm.example", "boss@firm.example"})),
])
def test_envelope_and_to_header_carry_the_audience_list(audience, expected):
    with smtp_server() as srv:
        result = nt.send_email(_notif(srv.server_address[1]), "S", "<p>b</p>", audience=audience)
    assert result.status == nt.SENT
    assert _envelope(srv) == expected
    assert _to_header(srv) == expected


def test_empty_alerts_list_sends_alerts_to_the_reports_list():
    with smtp_server() as srv:
        result = nt.send_email(_notif(srv.server_address[1], recipients=[]), "S", "<p>b</p>",
                               audience="alerts")
    assert result.status == nt.SENT
    assert _envelope(srv) == sorted(REPORTS) == _to_header(srv)


def test_empty_reports_list_sends_reports_to_the_alerts_list():
    with smtp_server() as srv:
        result = nt.send_email(_notif(srv.server_address[1], report_recipients=[]), "S", "<p>b</p>",
                               audience="reports")
    assert result.status == nt.SENT
    assert _envelope(srv) == sorted(ALERTS) == _to_header(srv)


@pytest.mark.parametrize("audience", ["alerts", "reports", "both"])
def test_both_lists_empty_is_a_recorded_failure_and_nothing_is_sent(audience):
    with smtp_server() as srv:
        result = nt.send_email(_notif(srv.server_address[1], recipients=[], report_recipients=[]),
                               "S", "<p>b</p>", audience=audience)
    assert result.status == nt.FAILED and "no recipients" in result.error.lower()
    assert srv.messages == [] and srv.mail_attempts == 0


def test_a_refused_address_is_a_failed_delivery_that_names_it():
    with smtp_server(refuse_rcpt=("boss@firm.example",)) as srv:
        result = nt.send_email(_notif(srv.server_address[1]), "S", "<p>b</p>", audience="reports")
    assert result.status == nt.FAILED
    assert "boss@firm.example" in result.error
    assert srv.mail_attempts == 1                      # a refused address is not retried


def test_all_addresses_refused_is_still_a_failed_delivery_naming_them():
    with smtp_server(refuse_rcpt=("firm.example",)) as srv:
        result = nt.send_email(_notif(srv.server_address[1]), "S", "<p>b</p>", audience="alerts")
    assert result.status == nt.FAILED and "ops@firm.example" in result.error


def test_the_attachment_rung_resend_keeps_the_same_list():
    big = {"filename": "logs.zip", "content": b"x" * 4096}
    with smtp_server(reject_attachments="554 5.7.1 attachment refused") as srv:
        result = nt.send_email(_notif(srv.server_address[1]), "S", "<p>b</p>", audience="reports",
                               attachment_rungs=[[big], []])
    assert result.status == nt.SENT and result.rung == 1
    assert _envelope(srv) == sorted(REPORTS) == _to_header(srv)


# ── notify(): the audience of an event ───────────────────────────────────────

def _config(port, **kw):
    cfg = MagicMock()
    cfg.notifications = _notif(port, **kw)
    cfg.alerts = AlertsConfig()
    cfg.firm_name = "Firm"
    cfg.dashboard.public_url = "http://backup-server:8080"
    return cfg


def _notify(tmp_path, event, **kw):
    cfg_holder = {}
    with smtp_server() as srv:
        cfg = _config(srv.server_address[1], **kw)
        cfg.paths.database_path = str(tmp_path / "m.db")
        cfg_holder["db"] = ManifestDB(str(tmp_path / "m.db"))
        try:
            result = nt.notify(cfg, event, db=cfg_holder["db"])
        finally:
            cfg_holder["db"].close()
    return result, srv


def test_an_alert_event_defaults_to_the_alerts_list(tmp_path):
    ev = nt.AlertEvent(key="k", kind="FAILURE", mode="cloud", title="t", message="m")
    assert ev.audience == "alerts"
    result, srv = _notify(tmp_path, ev)
    assert result.status == nt.SENT
    assert _envelope(srv) == sorted(ALERTS)


def test_a_reports_event_goes_to_the_reports_list(tmp_path):
    ev = nt.AlertEvent(key="fy", kind="INFO", mode="rollover", title="t", message="m",
                       audience="reports")
    result, srv = _notify(tmp_path, ev)
    assert result.status == nt.SENT
    assert _envelope(srv) == sorted(REPORTS)


def test_the_test_email_event_goes_to_both_lists_once(tmp_path):
    ev = nt.AlertEvent(key="test", kind="INFO", mode="test", title="t", message="m", audience="both")
    result, srv = _notify(tmp_path, ev)
    assert result.status == nt.SENT
    assert _envelope(srv) == sorted({"ops@firm.example", "both@firm.example", "boss@firm.example"})


def test_the_short_fallback_notice_uses_the_events_audience(tmp_path):
    ev = nt.AlertEvent(key="fb", kind="INFO", mode="x", title="t", message="m", audience="reports")
    with smtp_server() as srv:
        cfg = _config(srv.server_address[1])
        result = nt._fallback_alert(cfg, ev, RuntimeError("boom"))
    assert result.status == nt.SENT
    assert _envelope(srv) == sorted(REPORTS)
