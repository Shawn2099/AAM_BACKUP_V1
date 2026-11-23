"""Plan A8: the dashboard shows an open rollover step by step, and the manual 'Run FY rollover
now' can continue an unfinished rollover even after the config already points at the new FY."""

from test_support import ui_login  # noqa: E402
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import ui
from core import rollover_journal as rj
from core.manifest import ManifestDB

H = {}      # the TestClient is signed in already (tests/conftest.py)
FROM, TO = "FY26-27", "FY27-28"


def test_describe_steps_gives_plain_labels_in_order():
    journal = {"steps": {name: {"state": rj.PENDING, "attempts": 0, "last_error": None,
                                "finished_at": None} for name in rj.STEPS}}
    journal["steps"][rj.NAS_FOLDER].update(state=rj.FAILED, attempts=2, last_error="NAS off")
    rows = rj.describe_steps(journal)
    assert [r["step"] for r in rows] == list(rj.STEPS)
    nas = next(r for r in rows if r["step"] == rj.NAS_FOLDER)
    assert nas["state"] == rj.FAILED and nas["attempts"] == 2 and nas["error"] == "NAS off"
    assert all(r["label"] and "_" not in r["label"] for r in rows)


@pytest.fixture
def rig(monkeypatch, tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    # the config already points at the NEW FY (the switch is done)
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(**ui_login.DASHBOARD),
        paths=SimpleNamespace(source_drive="E:\\DATA\\FY27-28",
                              lan_destination="\\\\nas\\share\\FY27-28"))
    calls = []

    async def fake_arun_deployment(name, parameters=None, timeout=None):
        calls.append(name)
        return SimpleNamespace(id="fr-1", state=None)

    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "arun_deployment", fake_arun_deployment)
    monkeypatch.setattr(ui, "get_fy_prefix", lambda: TO)
    yield SimpleNamespace(client=TestClient(ui.app, raise_server_exceptions=False),
                          calls=calls, db=db)
    db.close()


def test_no_journal_means_no_card(rig):
    body = rig.client.get("/api/rollover", headers=H).json()
    assert body["journal"] is None and body["can_run"] is False


def test_open_journal_is_listed_and_can_be_continued(rig):
    rig.db.rollover_open(FROM, TO, "t0")
    for step in (rj.FINAL_BACKUPS, rj.SOURCE_FOLDER, rj.CONFIG_SWITCH):
        rig.db.rollover_step_done(FROM, TO, step, "t1")
    rig.db.rollover_step_failed(FROM, TO, rj.NAS_FOLDER, "t2", error="NAS off")
    body = rig.client.get("/api/rollover", headers=H).json()
    assert body["journal"]["from_fy"] == FROM and body["journal"]["to_fy"] == TO
    states = {s["step"]: s["state"] for s in body["journal"]["steps"]}
    assert states[rj.CONFIG_SWITCH] == rj.DONE and states[rj.NAS_FOLDER] == rj.FAILED
    assert body["can_run"] is True and "not finished" in body["message"]
    r = rig.client.post("/api/rollover/run?wait=false", headers=H)
    assert r.status_code == 202 and rig.calls == ["rollover-check/rollover-check"]


def test_a_finished_journal_is_not_shown(rig):
    rig.db.rollover_open(FROM, TO, "t0")
    for step in rj.STEPS:
        rig.db.rollover_step_done(FROM, TO, step, "t1")
    assert rig.client.get("/api/rollover", headers=H).json()["journal"] is None
