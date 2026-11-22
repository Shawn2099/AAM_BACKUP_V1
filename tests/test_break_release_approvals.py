"""U24 (agent_runs/TEST_DESIGN_RELEASE.md) - a blocked backup can only be approved for the active FY.

A guard blocked the LAN backup of FY25-26 long ago. Since then the office moved on: the configured FY is
FY26-27 and the overlap is off, so FY25-26 is no longer backed up. Approving that old request must not
start a run, must not mark the approval as used, and must answer with a client error.

Oracle: the HTTP status is 4xx, the approval row stays PENDING, and the deployment mock is called 0 times.
Control: the same approval for the active FY is accepted and starts exactly one run, so the refusal above
is specific to the closed folder and not a generic failure of the endpoint.

Prefect, the busy checks and the config are faked; the approval database is a temporary one.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import ui
from core import approvals as ap
from core.manifest import ManifestDB
from models.config import AppConfig, DashboardConfig, LanConfig, PathsConfig, RolloverConfig
from test_support import ui_login
from tests.test_config_cross_field import _quiet_legs

CONFIGURED = "FY26-27"
CLOSED = "FY25-26"
GUARDS = [{"guard": "G3_MASS_DELETE", "reason": "This backup would delete 600 of 1,000",
           "metrics": {"planned_deletes": 600, "delete_percent": 60.0}}]
JSON_ACCEPT = {"Accept": "application/json"}


def _cfg(tmp_path) -> AppConfig:
    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}')
    return AppConfig(
        paths=PathsConfig(source_drive=f"D:\\{CONFIGURED}", lan_destination=f"\\\\nas\\share\\{CONFIGURED}",
                          gcs_key_path=str(key), runtime_dir=str(tmp_path / "rt"),
                          database_path=str(tmp_path / "manifest.db")),
        lan=LanConfig(enabled=True),
        rollover=RolloverConfig(overlap_enabled=False),
        **_quiet_legs(dashboard=DashboardConfig(**ui_login.DASHBOARD)),
    )


@pytest.fixture
def rig(monkeypatch, tmp_path):
    ui._RATE_LIMITS.clear()
    cfg = _cfg(tmp_path)
    db = ManifestDB(str(tmp_path / "manifest.db"))
    calls: list[dict] = []

    async def fake_arun(name, parameters=None, timeout=None):
        calls.append({"name": name, "parameters": parameters})
        return SimpleNamespace(id="flow-run-1", state=None)

    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "arun_deployment", fake_arun)
    monkeypatch.setattr(ui, "_configured_fy", lambda: CONFIGURED)
    monkeypatch.setattr(ui, "_active_fy_list", lambda: [CONFIGURED])
    client = TestClient(ui.app, raise_server_exceptions=False, follow_redirects=False, headers=JSON_ACCEPT,
                        cookies={"session": ui_login.SESSION_TOKEN})
    yield SimpleNamespace(db=db, calls=calls, client=client)
    db.close()
    ui._RATE_LIMITS.clear()


def _open(db, fy: str) -> str:
    return db.open_approval_request(leg="lan", blocked_run_id="run-b", fy=fy, guards=GUARDS,
                                    phrase=ap.request_phrase(GUARDS),
                                    created_at=ui.now_iso())


def _approve(rig, aid: str):
    return rig.client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": ap.APPROVE_PHRASE})


def test_approving_a_closed_fy_is_refused_and_starts_no_run(rig):
    aid = _open(rig.db, CLOSED)
    r = _approve(rig, aid)
    assert 400 <= r.status_code < 500, r.status_code
    assert rig.db.get_approval(aid)["status"] == ap.PENDING
    assert rig.calls == []


def test_approving_the_active_fy_starts_exactly_one_run(rig):
    aid = _open(rig.db, CONFIGURED)
    r = _approve(rig, aid)
    assert r.status_code == 202, r.text
    assert len(rig.calls) == 1
    assert rig.calls[0]["name"] == "aam-backup/backup-lan"
    assert rig.calls[0]["parameters"]["approval_id"] == aid
    assert rig.calls[0]["parameters"]["fy"] == CONFIGURED
