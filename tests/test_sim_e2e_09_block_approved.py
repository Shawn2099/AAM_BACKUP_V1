"""Card S9 (docs/PLAN_E2E_SIMULATION_2026-11-23.md): WORST-A, a guard block that is approved, archive failing.

Timeline (IST, 2027): seed to 28 Mar (rig), CAL_A up to 10:00 on 1 Apr, the human approval at 10:00 on 1 Apr,
the approved re-run of FY27-28 cloud at 10:00, then the rest of CAL_A.

Scenario: from the 02:00 cloud of 1 Apr, the G3 (mass delete) guard BLOCKS FY27-28 on the cloud leg until a
human approves the request once. The product's own `flow._apply_approval` decides that; the stub below only
supplies the G3 numbers, as the real `cloud_guards_task` would after its measurement. The archive step of the
1 April switch fails on 1 and 2 April and succeeds on 3 April.

Hand expectations come from the card and the plan, never from product output. The human approval is simulated
through the product's dashboard route (POST /approvals/<id>/approve) as a signed-in user; the deployment that the
route starts is a recording double, and the approved re-run is then made explicitly with the parameters it asked for.
"""
from __future__ import annotations

import dataclasses
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pendulum
import pytest
from fastapi.testclient import TestClient

import flow
import ui
from core import guards as g
from core.manifest import ManifestDB
from core.time_utils import IST
from test_support import ui_login
from test_support.sim_calendar import CAL_A, A, B, Sim
from test_support.smtp_server import smtp_server
from tests.test_break_flow import _stub

OK = {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES", "LAN_COMPLETE", "LAN_NO_CHANGES"}   # copied by value (plan section 0)

# hand values (card S9 / plan section 0)
BLOCK_START = pendulum.datetime(2027, 4, 1, 2, 0, tz=IST)      # the 02:00 cloud of 1 April
APPROVAL_TIME = pendulum.datetime(2027, 4, 1, 10, 0, tz=IST)   # the human approves, then the approved re-run
ARCHIVE_OK_FROM = pendulum.datetime(2027, 4, 3, 0, 0, tz=IST)  # archive fails 1 and 2 April, works from 3 April
G3_METRICS = {"planned_deletes": 60, "destination_files": 100}  # the same numbers every time
EXPECTED_ARCHIVE_DAYS = [date(2027, 4, 1), date(2027, 4, 2), date(2027, 4, 3)]


def _guard_stub(state):
    """cloud_guards_task double: FY27-28 is G3-blocked from 02:00 on 1 April until an approval is applied.

    The G3 result is the product's own `guards.g3_mass_delete` fed with the hand numbers (60 deletes of
    100 files), so its metrics have the same shape as a real measurement. Before the block window it passes
    with no results (the real measurement found nothing to block)."""
    def guard(config, fy_prefix, artefacts_dir=None, approval_id=None, run_id=None, already_deleted=0):
        if fy_prefix == B and pendulum.now(IST) >= BLOCK_START and not state["cleared"]:
            limits = config.guards
            g3 = g.g3_mass_delete(G3_METRICS["planned_deletes"], G3_METRICS["destination_files"],
                                  limits.max_delete_files, limits.max_delete_percent,
                                  small_folder_files=limits.small_folder_files,
                                  small_folder_max_deletes=limits.small_folder_max_deletes,
                                  first_run=False)
            if g3.verdict != g.BLOCK:                    # the card's premise: these numbers block
                raise RuntimeError(f"S9 premise broken: G3 with {G3_METRICS} is {g3.verdict}")
            results = [g3]
            # the product's own approval gate: consumes the approval only for THIS run (plan 6.2)
            approved, extra = flow._apply_approval(config, "cloud", approval_id, run_id, results)
            record_results = [dataclasses.asdict(r) for r in results]
            if "G3_MASS_DELETE" in approved:
                state["cleared"] = True
                return {"fy": fy_prefix, "max_delete": 500, "results": record_results,
                        "approved_guards": sorted(approved), **extra}
            raise g.GuardBlocked("cloud", {"fy": fy_prefix, "max_delete": 500, "results": record_results,
                                           "approved_guards": [], **extra})
        return {"fy": fy_prefix, "max_delete": 500, "results": [], "approved_guards": []}
    return guard


def _approve_via_dashboard(sim, mp, approval_id):
    """POST /approvals/<id>/approve as a signed-in user. Returns (response, deployment starts recorded)."""
    ui_login.register(ui)
    db = ManifestDB.from_config(sim.cfg)
    started = []

    async def recording_deployment(name=None, parameters=None, timeout=None):
        started.append((name, parameters))
        return SimpleNamespace(id="flow-run-1", state=None)

    mp.setattr(ui, "get_db", lambda: db)
    mp.setattr(ui, "_cfg", lambda: sim.cfg)
    mp.setattr(ui, "arun_deployment", recording_deployment)
    mp.setattr(ui, "_is_running", AsyncMock(return_value=False))
    mp.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    try:
        resp = TestClient(ui.app, raise_server_exceptions=False).post(
            f"/approvals/{approval_id}/approve?wait=false", json={"phrase": "APPROVE"},
            headers=ui_login.HEADERS)
    finally:
        db.close()
    return resp, started


def _scenario(sim, mp):
    """Play the whole card once. Results are recorded here; each test asserts one fact."""
    state = {"cleared": False}
    mp.setattr(flow, "cloud_guards_task", _stub(_guard_stub(state)))

    def archive(**kw):                              # fails 1 and 2 April, works from 3 April (card S9)
        sim.archive_calls.append({**kw, "at": sim.now})
        return sim.now >= ARCHIVE_OK_FROM
    mp.setattr(flow.fy_rollover, "run_archive_transition", archive)

    out = {}
    sim.seed_to_mar28()
    # CAL_A up to (not including) the approval time: the 02:00 cloud blocks FY27-28, the 06:00 rollover switches
    sim.play([ev for ev in CAL_A if ev.at < APPROVAL_TIME])
    out["errors_before_approval"] = [(ev.kind, ev.at.isoformat(), type(e).__name__) for ev, e in sim.errors]
    out["pending_before"] = sim.approvals()
    out["blocked_rows"] = [r for r in sim.runs() if r["mode"] == "cloud" and r["fy"] == B
                         and r["started_at"].startswith("2027-04-01T02:00")]
    out["switch_writes_after_0600"] = list(sim.switch_writes)
    out["fy_a_rows_after_switch"] = sim.sql("SELECT * FROM run_history WHERE fy = ? ORDER BY id", A)

    # the human approval (dashboard route), then the approved re-run of FY27-28 cloud with the parameters it asked for
    [(aid,)] = sim.sql("SELECT approval_id FROM approvals WHERE leg = 'cloud' AND fy = ? AND status = 'PENDING'", B)
    out["approval_id"] = aid
    sim.go(APPROVAL_TIME)
    resp, started = _approve_via_dashboard(sim, mp, aid)
    out["approve_status_code"] = resp.status_code
    out["approve_starts"] = started                    # product-view: what the route asked the deployment to run
    out["approvals_after_approve"] = sim.approvals()
    out["rerun_error"] = None
    if started:
        params = started[0][1]
        try:
            flow.backup.fn("config.yaml", "cloud", approval_id=params["approval_id"], fy=params["fy"])
        except Exception as e:  # noqa: BLE001 - recorded for the tests
            out["rerun_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    else:
        out["rerun_error"] = "the approval route started no run"
    out["approvals_after_rerun"] = sim.approvals()
    out["rerun_rows"] = [r for r in sim.runs() if r["mode"] == "cloud" and r["fy"] == B
                       and r["started_at"].startswith("2027-04-01T10:00")]

    # the rest of CAL_A (21:00 LAN of 1 April onward)
    sim.play([ev for ev in CAL_A if ev.at > APPROVAL_TIME])
    out["errors_all"] = [(ev.kind, ev.at.isoformat(), type(e).__name__) for ev, e in sim.errors]
    out["switch_writes"] = list(sim.switch_writes)
    out["archive_calls"] = [dict(c) for c in sim.archive_calls]
    out["fy_a_rows_end"] = sim.sql("SELECT * FROM run_history WHERE fy = ? ORDER BY id", A)
    out["subjects"] = sim.subjects()
    out["approvals_end"] = sim.approvals()
    return out


@pytest.fixture(scope="module")
def s9(tmp_path_factory):
    """The card runs once per module; every test below asserts one fact from the same run."""
    tmp = tmp_path_factory.mktemp("s9")
    with pytest.MonkeyPatch.context() as mp, smtp_server() as srv:
        sim = Sim(tmp, mp, srv)
        try:
            yield _scenario(sim, mp)
        finally:
            sim.close()


# ── one fact per test ─────────────────────────────────────────────────────────────────────────

def test_s9_block_leaves_exactly_one_pending_cloud_request_for_fy27_28(s9):
    assert s9["pending_before"] == [("cloud", B, "PENDING")], s9["pending_before"]


def test_s9_the_0200_cloud_run_of_fy27_28_is_recorded_as_blocked(s9):
    assert [r["status"] for r in s9["blocked_rows"]] == ["CLOUD_BLOCKED"], s9["blocked_rows"]


def test_s9_the_dashboard_approval_is_accepted_and_the_request_is_approved(s9):
    assert 200 <= s9["approve_status_code"] < 300, s9["approve_status_code"]     # product-view: accepted (202 today)
    assert s9["approvals_after_approve"] == [("cloud", B, "APPROVED")], s9["approvals_after_approve"]


def test_s9_the_approval_is_used_once_by_the_fy27_28_cloud_rerun(s9):
    assert s9["rerun_error"] is None, s9["rerun_error"]
    assert s9["approvals_after_rerun"] == [("cloud", B, "USED")], s9["approvals_after_rerun"]
    assert s9["approvals_end"] == [("cloud", B, "USED")], s9["approvals_end"]


def test_s9_the_fy27_28_cloud_rerun_row_is_ok(s9):
    assert len(s9["rerun_rows"]) == 1, s9["rerun_rows"]
    assert s9["rerun_rows"][0]["status"] in OK, s9["rerun_rows"]


def test_s9_the_switch_happens_on_1_april_with_fy27_28_as_the_new_folder(s9):
    assert s9["switch_writes_after_0600"] == [(B, A)], s9["switch_writes_after_0600"]
    assert s9["switch_writes"] == [(B, A)], s9["switch_writes"]


def test_s9_archive_is_called_for_fy26_27_only_on_1_to_3_april(s9):
    calls = s9["archive_calls"]
    assert [c["old_fy"] for c in calls] == [A, A, A], calls
    assert [c["at"].date() for c in calls] == EXPECTED_ARCHIVE_DAYS, [c["at"] for c in calls]


def test_s9_archive_warnings_are_at_most_two(s9):
    warnings = [x for x in s9["subjects"] if "WARNING" in x and "Old year not moved" in x]
    assert len(warnings) <= 2, warnings


def test_s9_fy26_27_rows_are_frozen_after_the_switch(s9):
    # No FY26-27 run row is added or changed after the switch. Retention (db_retention_days, the purge that
    # runs inside backup()) may remove old rows; that is not a write and is not asserted here (see the report).
    after = {row[0]: row for row in s9["fy_a_rows_after_switch"]}
    end = {row[0]: row for row in s9["fy_a_rows_end"]}
    added = sorted(set(end) - set(after))
    changed = sorted(i for i in end if i in after and end[i] != after[i])
    assert added == [], f"FY26-27 rows added after the switch: {added}"
    assert changed == [], f"FY26-27 rows changed after the switch: {changed}"
