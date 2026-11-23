"""Owner decision B (2026-11-23): a block caused by the backup TARGET itself (not mounted or
unreachable, this very server, or a marker missing on a folder that holds files) is never
approvable; a new EMPTY folder stays approvable. Mail text, dashboard card text and the approve
endpoint must agree (audit CRITICAL #1: NAS-07/NAS-10 said "Do NOT approve" while the guard text
and the card offered Approve).
"""

import re
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pendulum
import pytest
from fastapi.testclient import TestClient

import flow
import ui
from core import approvals as ap
from core import faults, guards
from core.manifest import ManifestDB
from models.config import ApprovalsConfig
from test_support import ui_login  # noqa: F401
from test_a2_interrupted_run_status import _make_config

DEST = r"\\nas\share\FY26-27"

LOCAL = guards.g4_destination_identity(DEST, None, {"127.0.0.1"}, {"10.0.0.5"}, True)
UNRESOLVED = guards.g4_destination_identity(DEST, None, None, {"10.0.0.5"}, True)
MARKER_LOST = guards.g5_canary(False, True, DEST)            # folder with files, no marker
UNCHECKED = guards.g5_canary(None, None, DEST)                # target not reachable
EMPTY_NEW = guards.g5_canary(False, False, DEST)              # new empty folder

NOT_APPROVABLE = [("same-machine", LOCAL), ("unresolved-host", UNRESOLVED),
                  ("marker-lost-with-files", MARKER_LOST), ("not-reachable", UNCHECKED)]


def _entry(r):
    return {"guard": r.guard, "reason": r.reason, "metrics": r.metrics, "fault_code": r.fault_code,
            "approvable": r.approvable}


def _mail_text(results):
    """The mail body as composed: the alert message plus the catalog steps of its fault code."""
    record = {"fy": "FY26-27", "results": [asdict(r) for r in results]}
    with patch("flow.notify") as notify, patch("flow.ManifestDB") as db:
        db.from_config.return_value.open_approval_request.return_value = "appr-1"
        flow._open_block_request(_make_config(), "lan", "run-1", record, "2026-11-23T02:00:00")
    event = notify.call_args.args[1]
    fault = faults.get(event.fault_code)
    return event.message + "\n" + "\n".join(fault.steps)


def _contradicts(text):
    """Both 'Do NOT approve' and an instruction to approve."""
    if "Do NOT approve" not in text:
        return False
    rest = text.replace("Do NOT approve", "")
    return bool(re.search(r"\b(approve|approving)\b", rest, re.I)
                and re.search(r"(approve (this|on|only|to)|either approve|Approve\b)", rest))


@pytest.mark.parametrize("label,result", NOT_APPROVABLE)
def test_target_problems_are_not_approvable_and_say_so(label, result):
    assert not result.passed and result.approvable is False
    text = _mail_text([result])
    assert "cannot be approved" in text
    assert "either approve this backup once" not in text
    assert not _contradicts(text), text
    assert not _contradicts(result.reason), result.reason
    assert not re.search(r"Approve (to|only)", result.reason)


def test_nas07_and_nas10_texts_agree_with_the_guard_reason():
    for result in (LOCAL, MARKER_LOST):
        assert "Do NOT approve" in result.reason
        assert "next backup clears this by itself" in result.reason
        fault = faults.get(result.fault_code)
        assert any("Do NOT approve" in s for s in fault.steps)


def test_new_empty_folder_stays_approvable_and_says_exactly_what_approving_does():
    assert EMPTY_NEW.fault_code == "AAM-NAS-09" and EMPTY_NEW.approvable is True
    assert "creates the marker file" in EMPTY_NEW.reason and "first copy" in EMPTY_NEW.reason
    text = _mail_text([EMPTY_NEW])
    assert "approve this backup once" in text and "Do NOT approve" not in text
    assert "creates the marker file" in text
    assert "Check" in text


def test_destination_changed_is_still_approvable():
    changed = guards.g4_destination_identity(DEST, r"\\old\share\FY26-27", {"10.0.0.9"}, {"10.0.0.5"}, True)
    assert changed.fault_code == "AAM-NAS-08" and changed.approvable is True


def test_local_destination_allowed_by_the_documented_switch():
    assert guards.g4_destination_identity(DEST, None, {"127.0.0.1"}, {"10.0.0.5"}, False).passed


def test_request_with_any_unapprovable_guard_is_unapprovable():
    g3 = {"guard": "G3_MASS_DELETE", "reason": "x", "metrics": {}}
    assert ap.request_approvable([g3])
    assert not ap.request_approvable([g3, _entry(MARKER_LOST)])
    assert ap.request_approvable([_entry(EMPTY_NEW)])


def test_legacy_stored_g5_request_is_judged_by_its_numbers():
    legacy = lambda **m: {"guard": "G5_CANARY", "reason": "r", "metrics": m}      # noqa: E731
    assert not ap.entry_approvable(legacy(canary_present=False, destination_has_files=True))
    assert not ap.entry_approvable(legacy(canary_present=None, destination_has_files=None))
    assert ap.entry_approvable(legacy(canary_present=False, destination_has_files=False))


def test_flow_never_applies_an_approval_to_a_target_problem(tmp_path):
    with patch("flow.ManifestDB") as db:
        out, rec = flow._apply_approval(_make_config(), "lan", "appr-1", "run-2", [LOCAL])
    assert out == frozenset() and "cannot be approved" in rec["approval_rejected"]
    db.from_config.assert_not_called()      # nothing is even consumed


# ── dashboard: card + endpoint ────────────────────────────────────────────

@pytest.fixture
def rig(monkeypatch, tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    cfg = SimpleNamespace(dashboard=SimpleNamespace(**ui_login.DASHBOARD), approvals=ApprovalsConfig(),
                          cloud=SimpleNamespace(enabled=True), lan=SimpleNamespace(enabled=True))
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "_configured_fy", lambda: "FY26-27")
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    calls = []

    async def fake_run(name, parameters=None, timeout=None):
        calls.append(name)
        return SimpleNamespace(id="fr-1", state=None)

    monkeypatch.setattr(ui, "arun_deployment", fake_run)
    yield SimpleNamespace(db=db, calls=calls, client=TestClient(ui.app, raise_server_exceptions=False))
    db.close()


def _open(rig, result, drop_flag=False):
    entry = _entry(result)
    if drop_flag:
        entry.pop("approvable"), entry.pop("fault_code")
    return rig.db.open_approval_request(
        leg="lan", blocked_run_id="run-b", fy="FY26-27", guards=[entry], phrase="APPROVE",
        created_at=pendulum.now("UTC").isoformat())


@pytest.mark.parametrize("label,result", NOT_APPROVABLE)
def test_endpoint_refuses_target_problems_with_409(rig, label, result):
    aid = _open(rig, result)
    resp = rig.client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"})
    assert resp.status_code == 409, resp.text
    assert "cannot be approved" in resp.json()["detail"]
    assert rig.db.get_approval(aid)["status"] == ap.PENDING and rig.calls == []
    view = rig.client.get("/approvals").json()["approvals"][0]
    assert view["approvable"] is False
    assert view["not_approvable_note"].startswith("Do NOT approve")
    assert not _contradicts(view["not_approvable_note"] + " " + view["guards"][0]["reason"])


def test_endpoint_refuses_a_legacy_request_of_the_marker_lost_case(rig):
    aid = _open(rig, MARKER_LOST, drop_flag=True)
    resp = rig.client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"})
    assert resp.status_code == 409 and rig.calls == []


def test_endpoint_still_approves_the_new_empty_folder(rig):
    aid = _open(rig, EMPTY_NEW)
    resp = rig.client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"})
    assert resp.status_code == 202, resp.text
    assert rig.db.get_approval(aid)["status"] == ap.APPROVED and rig.calls == ["aam-backup/backup-lan"]
    assert rig.client.get("/approvals").json()["approvals"][0]["approvable"] is True


def test_card_hides_the_approve_button_when_not_approvable():
    js = (Path(__file__).resolve().parent.parent / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
    card = js[js.index("function approvalCard"):js.index("async function updateApprovals")]
    branch = card[card.index("a.approvable === false"):card.index("} else if (a.status === 'PENDING')")]
    assert "not_approvable_note" in branch and "'approve'" not in branch and "dismiss" in branch
