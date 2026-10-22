"""Two recipient lists - every producer states its audience (plan A2).

Wire-level checks of the callers: the scheduled report and the FY-switch notice go to the REPORTS
list, the dashboard test email and the password notice to BOTH lists (each address once), the
dashboard report buttons to the REPORTS list, the preview tool only to --to."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient  # noqa: F401 - used through _client

from core import notify as nt
from core.manifest import ManifestDB
from models.config import AppConfig
from test_support import ui_login
from test_support.smtp_server import smtp_server
from tests.test_g_email_samples import _config_dict
from tests.test_m5_dashboard_audit import _client

ALERTS = ["ops@firm.example", "both@firm.example"]
REPORTS = ["boss@firm.example", "both@firm.example"]


def _cfg(tmp_path, port) -> AppConfig:
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path), "lan_destination": "\\\\nas\\share\\FY26-27",
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@firm.example",
                       "recipients": ALERTS, "report_recipients": REPORTS, "smtp_security": "none",
                       "smtp_auth": False, "retry_attempts": 1, "retry_base_seconds": 0.01},
    )


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _envelopes(srv) -> list[list[str]]:
    return [sorted(r.split("<", 1)[1].split(">", 1)[0] for r in m["rcpts"]) for m in srv.messages]


def test_the_scheduled_report_goes_to_the_reports_list(tmp_path):
    import flow
    from unittest.mock import patch

    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        with patch.object(flow.report, "build_summary_email",
                          return_value=("Weekly report", "<p>summary</p>", [])):
            flow._send_summary_report(cfg, days=7, period="Weekly")
    assert _envelopes(srv) == [sorted(REPORTS)]


def test_the_scheduled_report_uses_the_alerts_list_when_no_report_list_is_set(tmp_path):
    import flow
    from unittest.mock import patch

    with smtp_server() as srv:
        cfg = _cfg(tmp_path, srv.server_address[1])
        cfg.notifications.report_recipients = []
        with patch.object(flow.report, "build_summary_email",
                          return_value=("Weekly report", "<p>summary</p>", [])):
            flow._send_summary_report(cfg, days=7, period="Weekly")
    assert _envelopes(srv) == [sorted(ALERTS)]


def test_the_test_email_goes_to_both_lists_once_each(tmp_path, db, monkeypatch):
    with smtp_server() as srv:
        client = _client(monkeypatch, _cfg(tmp_path, srv.server_address[1]), db)
        resp = client.post("/api/settings/test-email")
    assert resp.status_code == 200 and resp.json()["status"] == "sent"
    assert _envelopes(srv) == [sorted({"ops@firm.example", "both@firm.example", "boss@firm.example"})]
    for address in ("ops@firm.example", "boss@firm.example", "both@firm.example"):
        assert address in resp.json()["detail"]


def test_the_test_email_with_no_address_anywhere_says_what_to_add(tmp_path, db, monkeypatch):
    cfg = _cfg(tmp_path, 1)
    cfg.notifications.recipients, cfg.notifications.report_recipients = [], []
    client = _client(monkeypatch, cfg, db)
    resp = client.post("/api/settings/test-email")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "Add at least one email address" in detail and "mail server" not in detail.lower()


def test_the_dashboard_weekly_button_sends_to_the_reports_list(tmp_path, db, monkeypatch):
    with smtp_server() as srv:
        client = _client(monkeypatch, _cfg(tmp_path, srv.server_address[1]), db)
        resp = client.post("/trigger/report/weekly/email")
    assert resp.status_code == 200, resp.text
    assert _envelopes(srv) == [sorted(REPORTS)]
    assert "boss@firm.example" in resp.json()["detail"] and "ops@firm.example" not in resp.json()["detail"]


def test_the_password_changed_notice_goes_to_both_lists(tmp_path, db, monkeypatch):
    import ui

    with smtp_server() as srv:
        monkeypatch.setattr(ui, "_cfg", lambda: _cfg(tmp_path, srv.server_address[1]))
        monkeypatch.setattr(ui, "get_db", lambda: db)
        ui._announce_password_change(None, "Dashboard", "tester")
    assert _envelopes(srv) == [sorted({"ops@firm.example", "both@firm.example", "boss@firm.example"})]


def _tool():
    spec = importlib.util.spec_from_file_location(
        "preview_emails", Path(__file__).resolve().parent.parent / "tools" / "preview_emails.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_preview_tool_never_reaches_either_real_list(tmp_path):
    tool = _tool()
    with smtp_server() as srv:
        d = _config_dict(tmp_path)
        d["notifications"] = {"smtp_host": "127.0.0.1", "smtp_port": srv.server_address[1],
                              "sender": "b@firm.example", "recipients": ALERTS,
                              "report_recipients": REPORTS, "smtp_security": "none",
                              "smtp_auth": False, "retry_attempts": 1, "retry_base_seconds": 0.01}
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump(d), encoding="utf-8")
        assert tool.main(["--config", str(cfg), "--to", "owner@firm.example"]) == 0
    assert srv.messages and all(len(e) == 1 and e == ["owner@firm.example"] for e in _envelopes(srv))


def test_the_fy_switch_notice_is_a_reports_audience_event():
    import inspect
    import flow

    src = inspect.getsource(flow)
    block = src.split('key=f"rollover:done:{to_fy}"', 1)[1].split("))", 1)[0]
    assert "audience=AUDIENCE_REPORTS" in block
