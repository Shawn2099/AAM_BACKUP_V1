"""M3 3.5 — dashboard truthfulness (plan WP9).

Findings: /health always returned "healthy" (even when its own check
crashed); an 11-day outage (Aug 22 - Sep 1) was invisible; undelivered alert
emails were invisible; the FY shown came from the calendar while backups
used the configured FY.
"""

from test_support import ui_login  # noqa: E402
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pendulum
import pytest
from fastapi.testclient import TestClient

import ui
from core import approvals as ap
from core.attention import AMBER, RED, compute_attention
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import DashboardConfig


def _cfg(cloud=True, lan=True, stale=26):
    return SimpleNamespace(
        cloud=SimpleNamespace(enabled=cloud), lan=SimpleNamespace(enabled=lan),
        dashboard=SimpleNamespace(stale_after_hours=stale, alert_failure_window_days=7,
                                  **ui_login.DASHBOARD,
                                  public_url="http://backup-server:8080"),
        paths=SimpleNamespace(source_drive="E:\\DATA\\FY26-27"),
        firm_name="T",
        notifications=SimpleNamespace(send_on_failure=True),
        schedule=SimpleNamespace(cloud_cron="0 22 * * *", lan_cron="0 21 * * *",
                                 timezone="Asia/Kolkata"),
    )


def _ago(hours):
    return pendulum.now(IST).subtract(hours=hours).isoformat()


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _run(db, run_id, mode, status, hours_ago, em=None):
    db.insert_run({"run_id": run_id, "mode": mode, "started_at": _ago(hours_ago),
                   "ended_at": _ago(hours_ago), "status": status, "extended_metrics": em})


def _codes(items, level=None):
    return {i["code"] for i in items if level is None or i["level"] == level}


def test_all_good_means_nothing_to_attend(db):
    _run(db, "c", "cloud", "CLOUD_NO_CHANGES_COMPLETE", 3)
    _run(db, "l", "lan", "LAN_COMPLETE", 5)
    assert compute_attention(db, _cfg()) == []


def test_stale_and_never_succeeded_legs_are_red(db):
    _run(db, "c", "cloud", "CLOUD_COMPLETE", 11 * 24)          # the 11-day outage
    _run(db, "l1", "lan", "LAN_FAILED", 1)                     # failures don't count
    items = compute_attention(db, _cfg())
    assert _codes(items, RED) == {"stale:cloud", "stale:lan"}
    msgs = " ".join(i["message"] for i in items)
    assert "264 h" in msgs and "never completed successfully" in msgs.lower()


def test_disabled_leg_is_never_stale(db):
    _run(db, "c", "cloud", "CLOUD_COMPLETE", 1)
    assert compute_attention(db, _cfg(lan=False)) == []


def test_open_approval_audit_and_undelivered_alerts(db):
    _run(db, "c", "cloud", "CLOUD_COMPLETE", 1)
    _run(db, "l", "lan", "LAN_COMPLETE", 1)
    db.open_approval_request(leg="lan", blocked_run_id="b", fy="FY26-27",
                             guards=[{"guard": "G3_MASS_DELETE", "reason": "r", "metrics": {}}],
                             phrase="DELETE 9", created_at=_ago(1))
    db.record_audit({"audit_id": "a1", "mode": "cloud", "started_at": _ago(2),
                     "status": "VERIFICATION_FAILED", "detail": "content-changed=1",
                     "created_at": _ago(2)})
    db.record_audit({"audit_id": "a2", "mode": "lan", "started_at": _ago(2),
                     "status": "AUDIT_INCOMPLETE", "detail": "DNS", "created_at": _ago(2)})
    db.record_notification({"event_key": "backup:lan:LAN_FAILED", "kind": "FAILURE",
                            "subject": "s", "status": "FAILED", "error": "535 auth",
                            "created_at": _ago(3)})
    items = compute_attention(db, _cfg())
    assert {"approval:lan", "audit:cloud", "alerts_undelivered"} <= _codes(items, RED)
    assert "audit:lan" in _codes(items, AMBER)


def test_degraded_run_logs_are_amber(db):
    _run(db, "c", "cloud", "CLOUD_COMPLETE", 1, em='{"artefacts_degraded": true}')
    _run(db, "l", "lan", "LAN_COMPLETE", 1)
    assert _codes(compute_attention(db, _cfg()), AMBER) == {"run_logs:cloud"}


def test_dashboard_config_keys():
    d = DashboardConfig()
    assert (d.stale_after_hours, d.alert_failure_window_days) == (26, 7)


# ── /health and /status ───────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch, db):
    cfg = _cfg()
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    return TestClient(ui.app, raise_server_exceptions=False)


def test_health_is_503_with_reasons_when_anything_is_red(client, db):
    resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "attention" and any("cloud" in r.lower() for r in body["reasons"])


def test_health_is_200_when_nothing_is_red(client, db):
    _run(db, "c", "cloud", "CLOUD_COMPLETE", 1)
    _run(db, "l", "lan", "LAN_COMPLETE", 1)
    resp = client.get("/health")
    assert resp.status_code == 200 and resp.json()["status"] == "healthy"


def test_health_that_cannot_be_evaluated_is_not_healthy(client, monkeypatch):
    monkeypatch.setattr(ui, "get_db", lambda: (_ for _ in ()).throw(OSError("db gone")))
    resp = client.get("/health")
    assert resp.status_code == 503 and resp.json()["status"] == "unknown"


def test_status_carries_the_attention_banner_and_the_backups_fy(client, db):
    data = client.get("/status").json()
    assert {i["code"] for i in data["attention"]} >= {"stale:cloud", "stale:lan"}
    assert data["fy_prefix"] == "FY26-27"        # the configured FY the backups use


def test_dashboard_renders_the_banner():
    js = open("static/js/dashboard.js", encoding="utf-8").read()
    assert "attention-banner" in js and "data.attention" in js
