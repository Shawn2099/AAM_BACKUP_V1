"""Card S10 (docs/PLAN_E2E_SIMULATION_2026-11-23.md): WORST-A, a guard block that is never approved in time.

Timeline (IST, 2027): seed to 28 Mar (rig); the LAN leg of FY26-27 is G3-BLOCKED on every night from 21:00 on
31 March (the product's own `guards.g3_mass_delete` numbers, through the real `_apply_approval`). Nobody approves
until 10:00 on 3 April. The approve route starts an approved LAN re-run, which the rig runs with the parameters
the route asked for (as in S9). Then the 21:00 LAN of 3 April, the 02:00 cloud and the 06:00 rollover of 4 April.

Hand expectations come from the card and PLAN_FY_OVERLAP_2026-11-20 section 4 ("1 Apr onwards" and
"Switch blocked for days"). Only what those rows state is asserted about the approval sequence; everything else is
reported as an open question. The human approval is simulated through the product's dashboard route
(POST /approvals/<id>/approve) as a signed-in user.
"""
from __future__ import annotations

import dataclasses
import math
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

# hand values (card S10 / plan section 0)
LAN_BLOCK_FROM = pendulum.datetime(2027, 3, 31, 21, 0, tz=IST)     # the LAN of 31 March, every night after
FIRST_CHECK = pendulum.datetime(2027, 4, 3, 9, 0, tz=IST)           # the card's checkpoint
APPROVAL_TIME = pendulum.datetime(2027, 4, 3, 10, 0, tz=IST)       # the human approves
LAST_EVENT = pendulum.datetime(2027, 4, 4, 6, 0, tz=IST)           # the 4 April rollover is the last event
BLOCKED_DAYS = [pendulum.date(2027, 4, 1), pendulum.date(2027, 4, 2), pendulum.date(2027, 4, 3)]
REPEAT_WINDOW_HOURS = 51                                            # 1 Apr 06:00 -> 3 Apr 09:00
G3_METRICS = {"planned_deletes": 60, "destination_files": 100}
ROLLOVER_BLOCKED_CODE = "AAM-ROL-02"    # the fault code of the 'Last backup of the old year incomplete' mail


def _lan_guard_stub():
    """lan_guards_task double: FY26-27 is G3-blocked on every LAN night from 31 March 21:00 on. A run that carries
    an approval may pass once the product's `_apply_approval` accepts it (scheduled runs never carry one)."""
    def guard(config, fy_prefix, dry_run=None, destination_files=None, approval_id=None, run_id=None,
              already_deleted=0):
        if fy_prefix == A and pendulum.now(IST) >= LAN_BLOCK_FROM:
            limits = config.guards
            g3 = g.g3_mass_delete(G3_METRICS["planned_deletes"], G3_METRICS["destination_files"],
                                  limits.max_delete_files, limits.max_delete_percent,
                                  small_folder_files=limits.small_folder_files,
                                  small_folder_max_deletes=limits.small_folder_max_deletes,
                                  first_run=False)
            if g3.verdict != g.BLOCK:                    # the card's premise: these numbers block
                raise RuntimeError(f"S10 premise broken: G3 with {G3_METRICS} is {g3.verdict}")
            results = [g3]
            approved, extra = flow._apply_approval(config, "lan", approval_id, run_id, results)
            record_results = [dataclasses.asdict(r) for r in results]
            if "G3_MASS_DELETE" in approved:
                return {"fy": fy_prefix, "results": record_results,
                        "approved_guards": sorted(approved), **extra}
            raise g.GuardBlocked("lan", {"fy": fy_prefix, "results": record_results,
                                         "approved_guards": [], **extra})
        return {"fy": fy_prefix, "results": [], "approved_guards": []}
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


def _approval_rows(sim):
    return sim.sql("SELECT id, leg, fy, status, created_at, approved_at, used_at FROM approvals ORDER BY id")


def _open(rows, fy=A):
    """Approval rows of `fy` still open (PENDING or APPROVED), as (id, leg, status) in id order."""
    return [(i, leg, status) for (i, leg, f, status, *_rest) in rows if f == fy and status in ("PENDING", "APPROVED")]


def _blocked_mails(subjects):
    return [s for s in subjects if ROLLOVER_BLOCKED_CODE in s]


def _scenario(sim, mp):
    mp.setattr(flow, "lan_guards_task", _stub(_lan_guard_stub()))
    out = {"trace": []}
    sim.seed_to_mar28()

    # 1 Apr 00:00 .. 3 Apr 09:00 (card: "play CAL_A to Apr 3 09:00"); a snapshot after every event
    for ev in [e for e in CAL_A if e.at < FIRST_CHECK]:
        sim.play([ev])
        out["trace"].append({"kind": ev.kind, "at": ev.at, "switch_writes": list(sim.switch_writes),
                           "open": _open(_approval_rows(sim))})
    out["errors_first_phase"] = [(ev.kind, ev.at.isoformat(), type(e).__name__) for ev, e in sim.errors]
    out["checkpoint_open"] = _open(_approval_rows(sim))
    out["checkpoint_switch_writes"] = list(sim.switch_writes)
    out["checkpoint_blocked_mails"] = _blocked_mails(sim.subjects())
    out["checkpoint_source"] = str(sim.cfg.paths.source_drive)
    out["stale_repeat_hours"] = sim.cfg.alerts.stale_repeat_hours

    # the human approval (dashboard route) at 10:00 on 3 April; the route starts the approved LAN re-run
    [(aid,)] = sim.sql("SELECT approval_id FROM approvals WHERE leg = 'lan' AND fy = ? AND status = 'PENDING'", A)
    out["approval_id"] = aid
    sim.go(APPROVAL_TIME)
    resp, started = _approve_via_dashboard(sim, mp, aid)
    out["approve_status_code"] = resp.status_code
    out["approve_starts"] = started                    # product-view: what the route asked the deployment to run
    out["open_after_approve"] = _open(_approval_rows(sim))
    out["approved_run_error"] = None
    if started:
        params = started[0][1]
        try:
            flow.backup.fn("config.yaml", "lan", approval_id=params["approval_id"], fy=params["fy"])
        except Exception as e:  # noqa: BLE001 - recorded for the tests
            out["approved_run_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    else:
        out["approved_run_error"] = "the approval route started no run"
    out["rows_after_approved_run"] = _approval_rows(sim)

    # the rest up to the 4 April rollover (the 21:00 LAN of 3 April, the 02:00 cloud and the 06:00 rollover of 4 April)
    sim.play([e for e in CAL_A if APPROVAL_TIME < e.at <= LAST_EVENT])
    out["errors_all"] = [(ev.kind, ev.at.isoformat(), type(e).__name__) for ev, e in sim.errors]
    out["switch_writes_end"] = list(sim.switch_writes)
    out["approval_rows"] = _approval_rows(sim)
    out["runs"] = sim.runs()
    out["subjects"] = sim.subjects()
    return out


@pytest.fixture(scope="module")
def s10(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("s10")
    with pytest.MonkeyPatch.context() as mp, smtp_server() as srv:
        sim = Sim(tmp, mp, srv)
        try:
            yield _scenario(sim, mp)
        finally:
            sim.close()


# ── one fact per test ─────────────────────────────────────────────────────────────────────────

def test_s10_exactly_one_open_pending_lan_request_for_fy26_27_at_the_checkpoint(s10):
    pending = [row for row in s10["checkpoint_open"] if row[2] == "PENDING" and row[1] == "lan"]
    assert len(pending) == 1, s10["checkpoint_open"]


def test_s10_never_more_than_one_open_pending_lan_request_at_any_event(s10):
    worst = max(sum(1 for row in t["open"] if row[1] == "lan" and row[2] == "PENDING") for t in s10["trace"])
    assert worst <= 1, worst


def test_s10_no_switch_happens_on_1_to_3_april(s10):
    rollovers = [t for t in s10["trace"] if t["kind"] == "rollover"]
    assert len(rollovers) >= 3, [t["at"].isoformat() for t in rollovers]          # 30 Mar .. 3 Apr 06:00 events
    assert all(t["switch_writes"] == [] for t in rollovers), [(t["at"], t["switch_writes"]) for t in rollovers]
    assert s10["checkpoint_switch_writes"] == [], s10["checkpoint_switch_writes"]
    assert s10["checkpoint_source"].endswith(A), s10["checkpoint_source"]


def test_s10_the_blocked_rollover_is_reported_by_mail(s10):
    assert len(s10["checkpoint_blocked_mails"]) >= 1, s10["checkpoint_blocked_mails"]


def test_s10_blocked_rollover_mails_stay_within_one_per_stale_repeat_window(s10):
    count = len(s10["checkpoint_blocked_mails"])
    bound = math.ceil(REPEAT_WINDOW_HOURS / s10["stale_repeat_hours"])
    assert count <= bound, (count, bound, s10["stale_repeat_hours"], s10["checkpoint_blocked_mails"])


def test_s10_fy27_28_is_backed_up_on_each_night_on_both_legs(s10):
    for day in BLOCKED_DAYS:
        d = day.format("YYYY-MM-DD")
        for mode, hour in (("cloud", "02"), ("lan", "21")):
            rows = [r for r in s10["runs"] if r["mode"] == mode and r["fy"] == B
                    and r["started_at"].startswith(f"{d}T{hour}")]
            assert rows and all(r["status"] in OK for r in rows), (d, mode, rows)


def test_s10_fy26_27_cloud_is_backed_up_on_each_night_while_the_switch_is_pending(s10):
    for day in BLOCKED_DAYS:
        d = day.format("YYYY-MM-DD")
        rows = [r for r in s10["runs"] if r["mode"] == "cloud" and r["fy"] == A and r["started_at"].startswith(d)]
        assert rows and any(r["status"] in OK for r in rows), (d, rows)


def test_s10_the_approval_is_accepted_and_the_leg_request_is_approved(s10):
    assert 200 <= s10["approve_status_code"] < 300, s10["approve_status_code"]    # product-view: accepted
    leg_states = [status for (_i, leg, status) in s10["open_after_approve"] if leg == "lan"]
    assert leg_states == ["APPROVED"], s10["open_after_approve"]


def test_s10_the_approved_lan_rerun_uses_the_approval_once(s10):
    assert s10["approved_run_error"] is None, s10["approved_run_error"]
    used = [r for r in s10["rows_after_approved_run"] if r[1] == "lan" and r[2] == A and r[3] == "USED"]
    assert len(used) == 1, s10["rows_after_approved_run"]


def test_s10_no_switch_on_4_april_06_00_while_no_rollover_approval_has_been_given(s10):
    """Plan section 4 row 'Switch blocked for days': the leg approval first, then the G6 approval. No G6 approval
    is given in this card, so no switch is asserted for the 4 April rollover."""
    assert s10["switch_writes_end"] == [], s10["switch_writes_end"]


def test_s10_a_rollover_approval_is_not_requested_before_the_leg_approval_was_given(s10):
    """Plan section 4 row 'Switch blocked for days': 'a final-backup G3 block needs the per-leg approval first,
    then the G6 approval'. Asserted as stated: no rollover (G6) request exists before the leg approval is given."""
    rows = s10["approval_rows"]
    leg_given = next(r[5] for r in rows if r[1] == "lan" and r[2] == A and r[5] is not None)
    early_g6 = [r for r in rows if r[1] == "rollover" and r[4] < leg_given]
    assert early_g6 == [], [(r[0], r[4]) for r in early_g6]
