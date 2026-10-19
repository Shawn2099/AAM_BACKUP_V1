"""Step 3 of docs/PLAN_AUDIT_PENDING: "Check now" never backs up by itself. When the last good
backup is old it says so, action first, and lets the person back up first or check anyway."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pendulum
import pytest
from starlette.requests import Request

import ui
from core.messages import looks_action_first
from core.time_utils import IST
from models.config import AuditConfig

ROOT = Path(__file__).resolve().parent.parent


def _req(query=""):
    return Request({"type": "http", "query_string": query.encode(), "client": ("127.0.0.1", 1),
                    "headers": []})


def _body(resp):
    return json.loads(bytes(resp.body).decode())


def _ago(minutes):
    return pendulum.now(IST).subtract(minutes=minutes).isoformat()


def _setup(monkeypatch, last, minutes=60, cloud=True, lan=True):
    cfg = SimpleNamespace(cloud=SimpleNamespace(enabled=cloud), lan=SimpleNamespace(enabled=lan),
                          audit=SimpleNamespace(backup_first_prompt_minutes=minutes))
    db = MagicMock()
    db.last_successful_run.side_effect = lambda leg: last.get(leg)
    monkeypatch.setattr(ui, "_require_auth", lambda r: None)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda k, m: True)
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    started = AsyncMock(return_value=SimpleNamespace(id="r1", state=SimpleNamespace(type=None, is_final=lambda: False)))
    monkeypatch.setattr(ui, "arun_deployment", started)
    return started


def test_old_backup_asks_first_and_starts_nothing(monkeypatch):
    started = _setup(monkeypatch, {"lan": {"started_at": _ago(600)}, "cloud": {"started_at": _ago(10)}})
    resp = asyncio.run(ui.trigger_verify("all", _req()))
    body = _body(resp)
    assert resp.status_code == 409 and body["status"] == "backup_first"
    assert looks_action_first(body["detail"]) and body["detail"].startswith("Back up now first")
    assert "last LAN backup was at" in body["detail"] and "cloud backup" not in body["detail"]
    started.assert_not_called()


def test_recent_backups_start_the_check_straight_away(monkeypatch):
    started = _setup(monkeypatch, {"lan": {"started_at": _ago(5)}, "cloud": {"started_at": _ago(5)}})
    asyncio.run(ui.trigger_verify("all", _req()))
    started.assert_called_once()


def test_check_anyway_skips_the_question(monkeypatch):
    started = _setup(monkeypatch, {"lan": {"started_at": _ago(900)}, "cloud": {"started_at": _ago(900)}})
    asyncio.run(ui.trigger_verify("all", _req("anyway=1")))
    started.assert_called_once()


def test_a_leg_with_no_good_backup_yet_is_named(monkeypatch):
    _setup(monkeypatch, {"lan": None, "cloud": {"started_at": _ago(5)}})
    resp = asyncio.run(ui.trigger_verify("all", _req()))
    assert resp.status_code == 409 and "no successful LAN backup yet" in _body(resp)["detail"]


def test_only_the_legs_being_checked_are_asked_about(monkeypatch):
    started = _setup(monkeypatch, {"lan": {"started_at": _ago(900)}, "cloud": {"started_at": _ago(5)}})
    asyncio.run(ui.trigger_verify("cloud", _req()))
    started.assert_called_once()


def test_a_broken_pre_check_never_blocks_the_safety_check(monkeypatch):
    started = _setup(monkeypatch, {})
    monkeypatch.setattr(ui, "get_db", MagicMock(side_effect=RuntimeError("db locked")))
    asyncio.run(ui.trigger_verify("all", _req()))
    started.assert_called_once()


def test_the_question_threshold_is_a_config_key_with_bounds():
    assert AuditConfig.model_fields["backup_first_prompt_minutes"].default == 60
    with pytest.raises(Exception):
        AuditConfig(backup_first_prompt_minutes=-1)


def test_the_script_offers_check_anyway_after_the_question():
    js = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
    body = js.split("async function triggerVerify", 1)[1].split("function updateVerifyUi", 1)[0]
    assert "backup_first" in body and "anyway=1" in body and "confirm(" in body
