"""F5 (manual-button review 2026-11-23): a run the guards BLOCKED or that ended PARTIAL is a failed Prefect run, but
the button used to say 'ended as Failed' with no pointer. It now says what happened and where to go."""
import json
from types import SimpleNamespace

import pytest
from prefect.client.schemas.objects import StateType

import ui


def _run(rid="abc"):
    state = SimpleNamespace(type=StateType.FAILED, name="Failed", is_final=lambda: True)
    return SimpleNamespace(id=rid, state=state)


def _answer(monkeypatch, statuses):
    monkeypatch.setattr(ui, "_flow_run_statuses", lambda run_id: statuses)
    resp = ui._trigger_result("LAN", _run())
    return resp.status_code, json.loads(resp.body)


@pytest.mark.parametrize("status", ["LAN_BLOCKED", "CLOUD_BLOCKED"])
def test_blocked_points_at_the_pending_decision(monkeypatch, status):
    code, body = _answer(monkeypatch, [status])
    assert code >= 400 and body["status"] == "failed" and body["outcome"] == "blocked"
    assert "stopped for your safety" in body["detail"] and "pending decision" in body["detail"]
    assert "ended as" not in body["detail"]


@pytest.mark.parametrize("status", ["LAN_PARTIAL", "CLOUD_PARTIAL"])
def test_partial_says_only_partly_done(monkeypatch, status):
    code, body = _answer(monkeypatch, [status])
    assert code >= 400 and body["outcome"] == "partial"
    assert "only partly done" in body["detail"] and "ended as" not in body["detail"]


def test_block_wins_when_two_folders_ended_differently(monkeypatch):
    _, body = _answer(monkeypatch, ["LAN_PARTIAL", "LAN_BLOCKED"])
    assert body["outcome"] == "blocked"


@pytest.mark.parametrize("statuses", [[], ["LAN_FAILED"]])
def test_other_failures_keep_the_old_text(monkeypatch, statuses):
    _, body = _answer(monkeypatch, statuses)
    assert "ended as Failed" in body["detail"] and "outcome" not in body


def test_lookup_reads_the_rows_of_this_flow_run(tmp_path, monkeypatch):
    from core.manifest import ManifestDB
    db = ManifestDB(str(tmp_path / "m.db"))
    for rid, status in (("flowA-lan", "LAN_BLOCKED"), ("flowA-lan-FY27-28", "LAN_PARTIAL"), ("flowB-lan", "LAN_COMPLETE")):
        db.insert_run({"run_id": rid, "mode": "lan", "fy": "FY26-27", "started_at": "2026-11-23T01:00:00+05:30",
                       "status": status})
    monkeypatch.setattr(ui, "get_db", lambda: db)
    assert sorted(ui._flow_run_statuses("flowA")) == ["LAN_BLOCKED", "LAN_PARTIAL"]
    db.close()
