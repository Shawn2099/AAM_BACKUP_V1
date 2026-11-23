"""W39: the read-only 'Alerts history' page and its JSON (core/alerts_history.py, ui.py /alerts-history)."""
from __future__ import annotations

import json
import shutil
from unittest.mock import MagicMock

import pendulum
import pytest
from fastapi.testclient import TestClient

import ui
from core import alerts_history
from core.manifest import ManifestDB
from models.config import CONFIG_PATH, DashboardConfig, load_config, model_default
from test_support import ui_login

SIGNED = {"session": ui_login.SESSION_TOKEN}
ADDRESS = "secret.person@firm.example"
NOW = pendulum.datetime(2026, 11, 23, 12, 0, tz="UTC")


def _client(cookies=None) -> TestClient:
    return TestClient(ui.app, raise_server_exceptions=False, follow_redirects=False,
                      headers={"Accept": "application/json"}, cookies=dict(cookies or {}))


def _row(db, key, kind, status, hours_ago, *, mode="lan", subject="LAN backup failed", recipients=None,
         error=None, fy="FY26-27", fault="AAM-LAN-01"):
    db.record_notification({
        "event_key": key, "kind": kind, "mode": mode, "subject": subject, "status": status, "error": error,
        "created_at": NOW.subtract(hours=hours_ago).isoformat(), "fault_code": fault, "message_id": None,
        "audience": "alerts", "recipients": recipients, "fy": fy})


@pytest.fixture
def rig(monkeypatch, tmp_path):
    ui._RATE_LIMITS.clear()
    db = ManifestDB(str(tmp_path / "manifest.db"))
    cfg_file = tmp_path / "config.yaml"
    shutil.copy(CONFIG_PATH, cfg_file)
    cfg = load_config(str(cfg_file))
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", MagicMock(return_value=db))
    yield db, cfg
    db.close()


def test_page_size_key_default_and_bounds():
    assert model_default(DashboardConfig, "alerts_history_page_size") == 50
    DashboardConfig(alerts_history_page_size=10)
    DashboardConfig(alerts_history_page_size=500)
    for bad in (9, 501):
        with pytest.raises(Exception):
            DashboardConfig(alerts_history_page_size=bad)


def test_routes_need_a_sign_in(rig):
    c = _client()
    assert c.get("/api/alerts-history").status_code == 401
    assert c.get("/alerts-history", headers={"Accept": "text/html"}).status_code == 303
    assert _client(SIGNED).get("/alerts-history", headers={"Accept": "text/html"}).status_code == 200


def test_rows_shape_newest_first_and_logged_only_visible(rig):
    db, _ = rig
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "SENT", 5, recipients=[ADDRESS, "b@x.example"])
    _row(db, "recovered:lan", "RECOVERED", "LOGGED_ONLY", 3, subject="LAN backup recovered", fault=None,
         error="recovered after 2h 0m (problem first recorded 2026-11-23T07:00); under 24h, so no 'resolved' email")
    body = _client(SIGNED).get("/api/alerts-history").json()
    assert body["total"] == 2 and body["page"] == 1 and body["pages"] == 1
    first, second = body["rows"]
    assert first["status"] == "LOGGED_ONLY" and first["kind"] == "RECOVERED" and first["class"] == "resolved"
    assert first["recovered_after"] == "2h 0m"
    assert first["title"] == "LAN backup recovered" and first["leg"] == "lan" and first["fy"] == "FY26-27"
    assert second["status"] == "SENT" and second["recipient_count"] == 2 and second["fault_code"] == "AAM-LAN-01"
    assert second["class"] == "failure" and second["recovered_after"] is None
    assert first["time"].endswith("IST") or "20" in first["time"]
    assert set(first) >= {"id", "time", "kind", "class", "fault_code", "title", "fy", "leg", "status",
                          "recipient_count", "recovered_after"}


def test_no_address_and_only_the_first_line_of_a_title(rig):
    db, _ = rig
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "FAILED", 1, recipients=[ADDRESS],
         subject=f"Failed for {ADDRESS}\nsecond line with a body detail",
         error=f"refused: {ADDRESS}")
    raw = _client(SIGNED).get("/api/alerts-history").text
    assert ADDRESS not in raw and "second line" not in raw and "refused" not in raw
    row = json.loads(raw)["rows"][0]
    assert row["recipient_count"] == 1


def test_recovered_after_for_a_sent_resolved_mail(rig):
    db, _ = rig
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "SENT", 30)
    _row(db, "recovered:lan", "RECOVERED", "SENT", 1, subject="resolved")
    rows = _client(SIGNED).get("/api/alerts-history").json()["rows"]
    assert rows[0]["recovered_after"] == "1d 5h"


def test_paging_uses_the_configured_size(rig):
    db, cfg = rig
    cfg.dashboard.alerts_history_page_size = 10
    for i in range(25):
        _row(db, f"backup:lan:K{i}", "FAILURE", "SENT", 100 - i, subject=f"n{i}")
    c = _client(SIGNED)
    p1 = c.get("/api/alerts-history?page=1").json()
    p3 = c.get("/api/alerts-history?page=3").json()
    p4 = c.get("/api/alerts-history?page=4").json()
    assert p1["pages"] == 3 and p1["total"] == 25 and len(p1["rows"]) == 10
    assert p1["rows"][0]["title"] == "n24"                  # newest first
    assert len(p3["rows"]) == 5 and p4["rows"] == []


def test_the_view_does_not_write(rig):
    db, _ = rig
    _row(db, "backup:lan:LAN_FAILED", "FAILURE", "SENT", 1)
    before = db.notifications_page(100)[1]
    _client(SIGNED).get("/api/alerts-history")
    assert db.notifications_page(100)[1] == before
    assert alerts_history.build_page(db, 1, 10)["total"] == before
