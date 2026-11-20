"""OV-7.1 - OV-7.4: dashboard per financial year (plan 5.7).

/status carries folders[] (one entry per active FY, new first) next to the unchanged top-level fields; the
per-FY triggers (?fy=) only accept an active FY and are refused while any backup or safety check runs; the
rollover card lists the active set; the 'NOT backed up until the switch' texts no longer say that during
the overlap (R-13)."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pendulum
import pytest
from fastapi.exceptions import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import ui
from core import config_help, fy_overlap
from core.manifest import ALL_FYS, ManifestDB
from core.time_utils import IST
from models.config import AppConfig, RolloverConfig
from test_support import ui_login

ROOT = Path(__file__).resolve().parent.parent
A, B = "FY26-27", "FY27-28"
ACTIVATED = "2027-01-01T02:05:00+05:30"


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / A), "lan_destination": "\\\\nas\\share\\" + A,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
    )


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


@pytest.fixture
def world(monkeypatch, cfg, db):
    cfg.cloud.enabled = True            # switched on after validation: no bucket / key file needed here
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_require_auth", lambda request: None)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    ui._RATE_LIMITS.clear()
    return SimpleNamespace(cfg=cfg, db=db)


def overlap_on(monkeypatch, db, day=(2027, 1, 15)):
    """January 2027 with FY27-28 activated: the active set is [FY27-28, FY26-27]."""
    monkeypatch.setattr(fy_overlap, "run_date", lambda scheduled=None: pendulum.date(*day))
    db.record_activation(B, activated_at=ACTIVATED, source_path="D:\\" + B, created_by_app=True, clock_state="OK")


def _run(db, fy, leg, status, started, run_id=None):
    db.insert_run({"run_id": run_id or f"{fy}-{leg}-{status}", "mode": leg, "fy": fy, "started_at": started,
                   "ended_at": started, "status": status})


def _client():
    return TestClient(ui.app, raise_server_exceptions=False)


def _req(query=""):
    return Request({"type": "http", "query_string": query.encode(), "client": ("127.0.0.1", 5), "headers": []})


def _body(resp):
    return json.loads(bytes(resp.body).decode())


# ── OV-7.1: /status folders[] ─────────────────────────────────────────────────────────────────

def test_one_fy_status_is_todays_payload_plus_one_folder(world):
    _run(world.db, A, "cloud", "CLOUD_COMPLETE", "2026-12-20T02:00:00+05:30")
    _run(world.db, A, "lan", "LAN_COMPLETE", "2026-12-20T21:00:00+05:30")
    data = _client().get("/status").json()
    for key in ("firm", "fy_prefix", "attention", "verify_running", "schedule", "cloud", "lan", "manifest",
                "health", "recent_runs"):
        assert key in data
    assert data["fy_prefix"] == A
    assert len(data["folders"]) == 1
    f = data["folders"][0]
    assert (f["fy"], f["role"], f["active_since"]) == (A, "current", None)
    for leg in ("cloud", "lan"):          # the one card says exactly what the top level says
        for key in ("enabled", "last_run", "last_success", "timing", "last_run_formatted", "integrity", "nights"):
            assert f[leg][key] == data[leg][key], (leg, key)
        assert f[leg]["running"] is False and f[leg]["run_state"] == "idle"


def test_two_fys_new_first_with_their_own_history_and_since(world, monkeypatch):
    overlap_on(monkeypatch, world.db)
    _run(world.db, A, "cloud", "CLOUD_COMPLETE", "2027-01-14T02:00:00+05:30")
    _run(world.db, B, "cloud", "CLOUD_FAILED", "2027-01-14T02:01:00+05:30")
    data = _client().get("/status").json()
    assert [f["fy"] for f in data["folders"]] == [B, A]
    new, old = data["folders"]
    assert (new["role"], old["role"]) == ("early", "current")
    assert new["active_since"] == ACTIVATED and new["active_since_formatted"]
    assert new["cloud"]["last_run"]["status"] == "CLOUD_FAILED"
    assert old["cloud"]["last_run"]["status"] == "CLOUD_COMPLETE"
    # the early FY has no success of its own and must not borrow the other FY's
    assert new["cloud"]["last_success"] is None and old["cloud"]["last_success"]
    # the old top-level fields stay the configured FY's
    assert data["fy_prefix"] == A and data["cloud"]["last_run"]["status"] == "CLOUD_COMPLETE"


def test_running_comes_from_the_running_row_of_that_fy(world, monkeypatch):
    overlap_on(monkeypatch, world.db)
    _run(world.db, B, "cloud", "CLOUD_RUNNING", "2027-01-15T02:00:00+05:30")
    _run(world.db, A, "cloud", "CLOUD_COMPLETE", "2027-01-14T02:00:00+05:30")
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=True))
    new, old = _client().get("/status").json()["folders"]
    assert new["cloud"]["running"] is True and new["cloud"]["run_state"] == "running"
    assert old["cloud"]["running"] is False
    # a RUNNING row the scheduler knows nothing about (crash) is not shown as running
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    new, _ = _client().get("/status").json()["folders"]
    assert new["cloud"]["running"] is False


def test_overlap_off_gives_one_folder_even_with_an_activation_row(world, monkeypatch, cfg):
    overlap_on(monkeypatch, world.db)
    cfg.rollover.overlap_enabled = False
    assert [f["fy"] for f in _client().get("/status").json()["folders"]] == [A]


# ── OV-7.3: per-FY triggers ───────────────────────────────────────────────────────────────────

def _fake_deploy(monkeypatch, calls):
    async def fake(name=None, parameters=None, timeout=None):
        calls.append((name, parameters))
        return SimpleNamespace(id="run-1", state=SimpleNamespace(type=None, is_final=lambda: False))
    monkeypatch.setattr(ui, "arun_deployment", fake)


@pytest.mark.parametrize("fn,deployment", [("trigger_cloud", "aam-backup/backup-cloud"),
                                           ("trigger_lan", "aam-backup/backup-lan")])
def test_backup_trigger_passes_the_active_fy(world, monkeypatch, fn, deployment):
    overlap_on(monkeypatch, world.db)
    calls = []
    _fake_deploy(monkeypatch, calls)
    resp = asyncio.run(getattr(ui, fn)(_req(f"fy={B}")))
    assert resp.status_code == 202
    assert calls == [(deployment, {"fy": B})]


@pytest.mark.parametrize("fn", ["trigger_cloud", "trigger_lan"])
def test_backup_trigger_refuses_an_fy_that_is_not_active(world, monkeypatch, fn):
    calls = []
    _fake_deploy(monkeypatch, calls)               # single FY: FY27-28 is not active
    with pytest.raises(HTTPException) as exc:
        asyncio.run(getattr(ui, fn)(_req(f"fy={B}")))
    assert exc.value.status_code == 409 and not calls
    with pytest.raises(HTTPException) as exc:
        asyncio.run(getattr(ui, fn)(_req("fy=FY25-26")))      # a closed / past FY
    assert exc.value.status_code == 409 and not calls


def test_backup_trigger_without_fy_sends_no_parameters(world, monkeypatch):
    calls = []
    _fake_deploy(monkeypatch, calls)
    assert asyncio.run(ui.trigger_cloud(_req())).status_code == 202
    assert calls == [("aam-backup/backup-cloud", None)]


def test_manual_backup_refused_while_the_other_leg_runs(world, monkeypatch):
    calls = []
    _fake_deploy(monkeypatch, calls)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(side_effect=lambda leg: leg == "lan"))
    resp = asyncio.run(ui.trigger_cloud(_req()))
    assert resp.status_code == 400 and _body(resp)["status"] == "already_running" and not calls


def test_manual_backup_refused_while_a_safety_check_runs(world, monkeypatch):
    calls = []
    _fake_deploy(monkeypatch, calls)
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=True))
    for fn in (ui.trigger_cloud, ui.trigger_lan):
        resp = asyncio.run(fn(_req()))
        assert resp.status_code == 400 and _body(resp)["status"] == "already_running"
    assert not calls


def test_manual_backup_fails_closed_when_the_scheduler_is_silent(world, monkeypatch):
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ui.trigger_cloud(_req()))
    assert exc.value.status_code == 503


def test_verify_trigger_passes_fy_and_refuses_inactive(world, monkeypatch):
    overlap_on(monkeypatch, world.db)
    calls = []
    _fake_deploy(monkeypatch, calls)
    resp = asyncio.run(ui.trigger_verify("cloud", _req(f"fy={B}&anyway=1")))
    assert resp.status_code == 202
    assert calls == [("integrity-audit/integrity-audit", {"mode": "cloud", "fy": B})]
    calls.clear()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ui.trigger_verify("cloud", _req("fy=FY30-31&anyway=1")))
    assert exc.value.status_code == 409 and not calls
    resp = asyncio.run(ui.trigger_verify("all", _req("anyway=1")))       # no fy: every active FY, as before
    assert calls == [("integrity-audit/integrity-audit", {"mode": "all"})]


def test_flows_accept_fy_but_the_rollover_check_does_not():
    import inspect
    import flow
    for fn in (flow.backup, flow.integrity_audit_flow):
        assert "fy" in inspect.signature(fn.fn).parameters
    assert "fy" not in inspect.signature(flow.rollover_check_flow.fn).parameters


# ── OV-7.2 / OV-7.4: cards, rollover card, texts ──────────────────────────────────────────────

def test_dashboard_draws_one_card_per_fy():
    js = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
    html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "renderFolders(data)" in js and "data.folders" in js and "Backed up since" in js
    assert "/trigger/' + leg + '?fy='" in js and "/trigger/verify/' + leg + '?fy='" in js
    assert 'id="fy-cards"' in html and 'id="route-section"' in html


def test_rollover_card_lists_the_active_set(world, monkeypatch):
    overlap_on(monkeypatch, world.db)
    d = _client().get("/api/rollover").json()
    assert [(f["fy"], f["role"]) for f in d["active_fys"]] == [(B, "early"), (A, "current")]
    assert d["active_fys"][0]["active_since"] == ACTIVATED
    settings_js = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    assert "d.active_fys" in settings_js and 'id="ro-active"' in (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")


def test_no_text_says_the_next_year_is_not_backed_up_until_the_switch_without_the_overlap():
    desc = RolloverConfig.model_fields["create_source_folder_early"].description
    help_text = " ".join(config_help.HELP["rollover.create_source_folder_early"])
    for text in (desc, help_text):
        assert "backed up every night as well" in text
        assert "NOT backed up until the switch is done; the dashboard" not in text
        assert "are not backed up until the switch is done; the dashboard" not in text


def test_overlap_and_small_folder_settings_are_advanced_and_labelled():
    from core import config_edit
    for key in ("rollover.overlap_enabled", "rollover.early_start_month", "rollover.activation_alert_after_hours",
                "rollover.sibling_ignore", "guards.small_folder_files", "guards.small_folder_max_deletes"):
        assert key not in config_edit.BASIC_KEYS and config_edit.is_risky(key), key
        assert key in config_help.HELP, key
