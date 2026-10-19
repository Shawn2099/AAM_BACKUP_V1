"""M2 2.3c — dashboard approve / dismiss / run for BLOCKED runs (plan §6.2).

Real auth (API key), real ManifestDB; only Prefect is faked.
"""

from test_support import ui_login  # noqa: E402
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pendulum
import pytest
from fastapi.testclient import TestClient

import ui
from core import approvals as ap
from core.manifest import ManifestDB
from models.config import ApprovalsConfig

KEY = {}      # the TestClient is signed in already (tests/conftest.py)
GUARDS = [{"guard": "G3_MASS_DELETE", "reason": "This backup would delete 600 of 1,000",
           "metrics": {"planned_deletes": 600, "delete_percent": 60.0}}]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(**ui_login.DASHBOARD),
        approvals=ApprovalsConfig(),
        cloud=SimpleNamespace(enabled=True), lan=SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    calls = []

    async def fake_arun_deployment(name, parameters=None, timeout=None):
        calls.append({"name": name, "parameters": parameters, "timeout": timeout})
        return SimpleNamespace(id="flow-run-1", state=None)

    monkeypatch.setattr(ui, "arun_deployment", fake_arun_deployment)
    rig = SimpleNamespace(db=db, cfg=cfg, calls=calls,
                          client=TestClient(ui.app, raise_server_exceptions=False))
    yield rig
    db.close()


def _open(rig, leg="lan"):
    return rig.db.open_approval_request(
        leg=leg, blocked_run_id="run-b", fy="FY26-27", guards=GUARDS,
        phrase=ap.request_phrase(GUARDS), created_at=pendulum.now("UTC").isoformat())


def _approve(rig, aid, phrase="APPROVE", headers=KEY):
    return rig.client.post(f"/approvals/{aid}/approve?wait=false",
                           json={"phrase": phrase}, headers=headers)


def test_list_shows_reason_numbers_and_phrase(rig):
    aid = _open(rig)
    data = rig.client.get("/approvals", headers=KEY).json()
    [row] = data["approvals"]
    assert row["approval_id"] == aid and row["leg"] == "lan" and row["status"] == ap.PENDING
    assert row["phrase"] == "APPROVE"
    assert row["guards"][0]["metrics"]["planned_deletes"] == 600


def test_approve_with_phrase_records_who_and_reruns_once_with_approval_id(rig):
    aid = _open(rig)
    resp = _approve(rig, aid, phrase="  approve ")      # case/space tolerant
    assert resp.status_code == 202, resp.text
    req = rig.db.get_approval(aid)
    assert req["status"] == ap.APPROVED
    assert "(session)" in req["approved_by"]
    hours = (pendulum.parse(req["expires_at"]) - pendulum.parse(req["approved_at"])).in_hours()
    assert hours == 12
    assert rig.calls == [{"name": "aam-backup/backup-lan",
                          "parameters": {"approval_id": aid}, "timeout": 0}]


@pytest.mark.parametrize("phrase", ["", "DELETE 600", "yes"])
def test_wrong_phrase_changes_nothing(rig, phrase):
    aid = _open(rig)
    resp = _approve(rig, aid, phrase=phrase)
    assert resp.status_code == 400 and "APPROVE" in resp.json()["detail"]
    assert rig.db.get_approval(aid)["status"] == ap.PENDING
    assert rig.calls == []


def test_approvals_refused_until_the_passwords_are_set_up(rig):
    rig.cfg.dashboard = SimpleNamespace(login_password_hash="", admin_password_hash="")  # fail closed
    aid = _open(rig)
    resp = _approve(rig, aid)
    assert resp.status_code == 503 and "not set up" in resp.json()["detail"]
    assert rig.db.get_approval(aid)["status"] == ap.PENDING and rig.calls == []


def test_unauthenticated_is_refused(rig):
    aid = _open(rig)
    stranger = TestClient(ui.app, cookies={"session": "not-a-session"})
    resp = stranger.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"})
    assert resp.status_code == 401
    assert rig.db.get_approval(aid)["status"] == ap.PENDING


def test_cannot_approve_twice_or_a_dismissed_request(rig):
    aid = _open(rig)
    assert _approve(rig, aid).status_code == 202
    assert _approve(rig, aid).status_code == 409
    other = _open(rig, leg="cloud")
    assert rig.client.post(f"/approvals/{other}/dismiss", headers=KEY).status_code == 200
    assert rig.db.get_approval(other)["status"] == ap.DISMISSED
    assert _approve(rig, other).status_code == 409
    assert rig.client.post("/approvals/nope/approve", json={"phrase": "x"},
                           headers=KEY).status_code == 404


@pytest.mark.parametrize("running,code", [(True, 409), (None, 503)])
def test_leg_busy_or_unknown_changes_nothing(rig, monkeypatch, running, code):
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=running))
    aid = _open(rig)
    assert _approve(rig, aid).status_code == code
    assert rig.db.get_approval(aid)["status"] == ap.PENDING and rig.calls == []


def test_trigger_failure_keeps_approval_and_run_retries_it(rig, monkeypatch):
    aid = _open(rig)

    async def broken(name, parameters=None, timeout=None):
        raise RuntimeError("deployment not found")

    monkeypatch.setattr(ui, "arun_deployment", broken)
    resp = _approve(rig, aid)
    assert resp.status_code == 500 and "approved" in resp.json()["detail"].lower()
    assert rig.db.get_approval(aid)["status"] == ap.APPROVED

    async def ok(name, parameters=None, timeout=None):
        rig.calls.append({"name": name, "parameters": parameters})
        return SimpleNamespace(id="flow-run-2", state=None)

    monkeypatch.setattr(ui, "arun_deployment", ok)
    assert rig.client.post(f"/approvals/{aid}/run?wait=false", headers=KEY).status_code == 202
    assert rig.calls[-1]["parameters"] == {"approval_id": aid}


def test_run_refuses_pending_used_and_expired(rig):
    pending = _open(rig)
    assert rig.client.post(f"/approvals/{pending}/run", headers=KEY).status_code == 409
    used = _open(rig, leg="cloud")
    past = pendulum.now("UTC").subtract(hours=13).isoformat()
    rig.db.approve_request(used, approved_by="t", approved_at=past,
                           expires_at=pendulum.now("UTC").subtract(hours=1).isoformat())
    resp = rig.client.post(f"/approvals/{used}/run", headers=KEY)
    assert resp.status_code == 409 and "expired" in resp.json()["detail"]
    assert rig.calls == []


def test_rollover_approval_reruns_the_rollover_deployment(rig):
    guards = [{"guard": "G6_FY_ROLLOVER", "reason": "final LAN backup failed",
               "metrics": {"from_fy": "FY26-27", "to_fy": "FY27-28"}}]
    aid = rig.db.open_approval_request(
        leg="rollover", blocked_run_id="run-r", fy="FY26-27", guards=guards,
        phrase=ap.request_phrase(guards), created_at=pendulum.now("UTC").isoformat())
    resp = _approve(rig, aid, phrase="approve")
    assert resp.status_code == 202, resp.text
    assert rig.calls == [{"name": "rollover-check/rollover-check",
                          "parameters": {"approval_id": aid}, "timeout": 0}]


def test_rollover_deployment_is_tagged_for_the_busy_check():
    import inspect

    import serve
    src = inspect.getsource(serve)
    block = src[src.index('name="rollover-check"'):]
    tags_line = next(line for line in block.splitlines() if "tags=" in line)
    assert '"rollover"' in tags_line


def test_a_request_stored_with_an_old_long_phrase_is_confirmed_with_approve(rig):
    """Requests created before the one-word rule keep working (the card shows APPROVE)."""
    aid = rig.db.open_approval_request(
        leg="lan", blocked_run_id="r-old", fy="FY26-27", guards=GUARDS,
        phrase="DELETE 600", created_at=pendulum.now("UTC").isoformat())
    assert rig.client.get("/approvals", headers=KEY).json()["approvals"][0]["phrase"] == "APPROVE"
    assert _approve(rig, aid, phrase="DELETE 600").status_code == 400
    assert _approve(rig, aid, phrase="APPROVE").status_code in (200, 202)
