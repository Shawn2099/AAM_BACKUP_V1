"""Dashboard findings of the 2026-10-18 audit (items 3, 4, 7 + low items).

3  a dashboard that cannot reach the server says so (no stale green);
4  "Send test email" through the real alert path; amber until mail settings
   that changed were tested;
7  separate rate limits per action, every limit a config key;
low: fault code + plain title on run rows, no zero badges on blocked rows,
   truthful LAN confirm text, IST integrity time, no dead flash code."""

from test_support import ui_login  # noqa: E402
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import ui
from core import notify as nt
from core.attention import AMBER, compute_attention
from core.manifest import ManifestDB
from core.time_utils import now_iso
from models.config import AppConfig, DashboardConfig
from test_support.smtp_server import smtp_server

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
SETTINGS_JS = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
KEY = {}      # the TestClient is signed in already (tests/conftest.py)
UNC = "\\\\nas\\share\\FY26-27"


def _cfg(tmp_path, port=25, **dash) -> AppConfig:
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path), "lan_destination": UNC,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD, **dash},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@firm.example",
                       "recipients": ["o@firm.example"], "smtp_security": "none",
                       "smtp_auth": False, "retry_attempts": 1, "retry_base_seconds": 0.01,
                       "send_on_failure": False},
    )


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _client(monkeypatch, cfg, db):
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    ui._RATE_LIMITS.clear()
    return TestClient(ui.app, raise_server_exceptions=False)


# ── low: fault code on the run rows ──────────────────────────────────────

def test_run_rows_carry_the_fault_code_and_plain_title(tmp_path, db, monkeypatch):
    db.insert_run({"run_id": "r1", "mode": "lan", "started_at": now_iso(), "status": "LAN_FAILED",
                   "extended_metrics": json.dumps({"fault_code": "AAM-NAS-01"})})
    db.insert_run({"run_id": "r0", "mode": "lan", "started_at": "2026-10-01T21:00:00+05:30",
                   "status": "LAN_FAILED"})                       # old row: no code stored
    data = _client(monkeypatch, _cfg(tmp_path), db).get("/status", headers=KEY).json()
    new, old = data["recent_runs"][0], data["recent_runs"][1]
    assert (new["fault_code"], new["fault_title"]) == ("AAM-NAS-01", "NAS not reachable")
    assert old["fault_code"] is None and old["fault_title"] is None   # never invented
    assert data["lan"]["last_run"]["fault_code"] == "AAM-NAS-01"
    assert "fault_title" in JS and "fault_code" in JS


def test_blocked_rows_show_no_zero_badges_and_rollover_rows_are_named():
    # LAN badges only when the run measured them; cloud badges likewise
    assert "'added' in metrics" in JS and "'total_files' in metrics" in JS
    assert "rollover" in JS.split("safeMode", 1)[1].split(";", 1)[0]


def test_integrity_time_is_shown_in_ist():
    assert "formatFullDate(integrity.checked_at)" in JS


def test_lan_confirm_text_follows_the_settings():
    assert "Includes WoL + sync + shutdown" not in JS
    assert "lan_wol" in TEMPLATE and "lan_shutdown" in TEMPLATE


def test_dead_flash_code_is_gone():
    assert "flash_map" not in (ROOT / "ui.py").read_text(encoding="utf-8")


# ── 3: server unreachable ────────────────────────────────────────────────

def test_dashboard_says_when_it_cannot_reach_the_server():
    assert 'id="server-banner"' in TEMPLATE and 'id="last-updated"' in TEMPLATE
    assert "Cannot reach the backup server since" in JS
    body = JS.split("async function updateStatus", 1)[1].split("\nsetInterval", 1)[0]
    # a non-OK answer and a network error both mark the page stale - never silently ignored
    assert body.count("setServerUnreachable(true") >= 2
    assert "setServerUnreachable(false" in body


# ── 4: test email ────────────────────────────────────────────────────────

def test_send_test_email_goes_through_the_alert_path(tmp_path, db, monkeypatch):
    with smtp_server() as srv:
        client = _client(monkeypatch, _cfg(tmp_path, port=srv.server_address[1]), db)
        resp = client.post("/api/settings/test-email", headers=KEY)
    assert resp.status_code == 200 and resp.json()["status"] == "sent"
    assert len(srv.messages) == 1                        # sent although send_on_failure is off
    row = db.latest_notification_for_mode("test", 1)
    assert row["status"] == nt.SENT and row["kind"] == "INFO"
    assert "sendTestEmail" in SETTINGS_JS


def test_test_email_failure_is_reported(tmp_path, db, monkeypatch):
    client = _client(monkeypatch, _cfg(tmp_path, port=1), db)           # nothing listens
    resp = client.post("/api/settings/test-email", headers=KEY)
    assert resp.status_code == 502 and resp.json()["status"] == "failed"


@pytest.mark.anonymous
def test_test_email_needs_sign_in(tmp_path, db, monkeypatch):
    client = _client(monkeypatch, _cfg(tmp_path), db)
    assert client.post("/api/settings/test-email").status_code == 401


def test_amber_until_changed_mail_settings_were_tested(tmp_path, db):
    cfg = _cfg(tmp_path)
    codes = lambda: {i["code"] for i in compute_attention(db, cfg) if i["level"] == AMBER}  # noqa: E731
    assert "email_untested" not in codes()
    db.record_config_change({"changed_at": now_iso(), "changed_by": "x", "key": "notifications.smtp_host",
                             "old_value": "a", "new_value": "b", "risky": 0, "backup_file": ""})
    assert "email_untested" in codes()
    db.record_notification({"event_key": "test:1", "kind": "INFO", "mode": "test", "status": nt.SENT,
                            "created_at": now_iso()})
    assert "email_untested" not in codes()


# ── 7: rate limits ───────────────────────────────────────────────────────

def test_rate_limits_and_dashboard_timings_are_config_keys():
    d = DashboardConfig()
    assert (d.rate_limit_window_seconds, d.rate_limit_triggers, d.rate_limit_logins,
            d.rate_limit_reports, d.rate_limit_settings, d.rate_limit_decisions) == (300, 5, 10, 10, 20, 10)
    assert (d.session_hours, d.config_refresh_seconds, d.trigger_wait_seconds) == (24, 300, 180)
    src = (ROOT / "ui.py").read_text(encoding="utf-8")
    assert not re.search(r"^_RATE_(WINDOW|MAX_\w+)\s*=\s*\d", src, re.MULTILINE)
    assert not re.search(r"^(TRIGGER_WAIT_SECONDS|_CONFIG_TTL\b.*=\s*\d)", src, re.MULTILINE)


def test_settings_saves_do_not_use_up_the_approval_limit(tmp_path, db, monkeypatch):
    cfg = _cfg(tmp_path, rate_limit_triggers=1, rate_limit_settings=2, rate_limit_decisions=1)
    client = _client(monkeypatch, cfg, db)
    monkeypatch.setattr(ui, "_settings_config_path", lambda: str(tmp_path / "none.yaml"))
    for _ in range(2):
        client.post("/api/settings", headers=KEY, json={"changes": {}})
    assert client.post("/api/settings", headers=KEY, json={"changes": {}}).status_code == 429
    # approvals keep their own budget
    resp = client.post("/approvals/nope/dismiss", headers=KEY)
    assert resp.status_code != 429
