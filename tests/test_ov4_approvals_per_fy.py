"""OV-4.3 / OV-4.4 - approvals are per FY (plan 5.4; R-1 BLOCKER) and a closed FY leaves no open items.

R-1 scenario: FY26-27 LAN is BLOCKED (G3, 500 deletes) and the owner approves; the FY27-28 folder, run first
by the same re-run, measures 400 deletes - within the approved numbers. Without the FY check the approval would
be applied to (and consumed by) the WRONG folder."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import flow
from core import approvals as ap
from core import guards as g
from core.manifest import ALL_FYS, ManifestDB
from tests.test_ov2_fy_overlap import CFG_FY, NEXT, _cfg

NOW = "2027-01-05T03:00:00+05:30"
LATER = "2027-01-05T04:00:00+05:30"
EXPIRES = "2027-01-06T03:00:00+05:30"


def _approval(fy=CFG_FY, leg="lan", guard="G3_MASS_DELETE", planned=500, pct=5.0):
    return {"approval_id": "a1", "leg": leg, "fy": fy, "guard": guard, "approved_at": NOW,
            "expires_at": EXPIRES, "used_at": None, "dismissed_at": None,
            "approved_metrics": {"planned_deletes": planned, "delete_percent": pct}}


# ── the pure decision ─────────────────────────────────────────────────────────

def test_r1_the_other_fys_approval_is_refused_even_within_the_approved_numbers():
    fresh = {"planned_deletes": 400, "delete_percent": 4.0}            # better than approved
    d = ap.evaluate_approval(_approval(fy=CFG_FY), leg="lan", guard="G3_MASS_DELETE", fy=NEXT,
                             fresh_metrics=fresh, now_iso=LATER, tolerance_percent=0)
    assert not d.allowed and CFG_FY in d.reason and NEXT in d.reason


def test_the_same_fy_is_allowed():
    fresh = {"planned_deletes": 400, "delete_percent": 4.0}
    d = ap.evaluate_approval(_approval(fy=NEXT), leg="lan", guard="G3_MASS_DELETE", fy=NEXT,
                             fresh_metrics=fresh, now_iso=LATER, tolerance_percent=0)
    assert d.allowed


def test_an_approval_without_an_fy_is_never_valid_for_a_backup():
    a = _approval()
    a["fy"] = None
    d = ap.evaluate_approval(a, leg="lan", guard="G3_MASS_DELETE", fy=CFG_FY,
                             fresh_metrics={"planned_deletes": 1, "delete_percent": 1}, now_iso=LATER,
                             tolerance_percent=0)
    assert not d.allowed


def test_fy_is_required_not_defaulted():
    with pytest.raises(TypeError):
        ap.evaluate_approval(_approval(), leg="lan", guard="G3_MASS_DELETE",
                             fresh_metrics={}, now_iso=LATER, tolerance_percent=0)      # noqa: missing fy


def test_evaluate_request_checks_the_fy_too():
    blocked = [g.GuardResult("G3_MASS_DELETE", g.BLOCK, "x", {"planned_deletes": 400, "delete_percent": 4.0})]
    request = {"approval_id": "a1", "leg": "lan", "fy": CFG_FY, "approved_at": NOW, "expires_at": EXPIRES,
               "used_at": None, "dismissed_at": None,
               "guards": [{"guard": "G3_MASS_DELETE", "metrics": {"planned_deletes": 500, "delete_percent": 5.0}}]}
    assert not ap.evaluate_request(request, leg="lan", blocked=blocked, fy=NEXT, now_iso=LATER,
                                   tolerance_percent=0).allowed
    assert ap.evaluate_request(request, leg="lan", blocked=blocked, fy=CFG_FY, now_iso=LATER,
                               tolerance_percent=0).allowed


# ── _apply_approval with a real DB ────────────────────────────────────────────

@pytest.fixture
def world(tmp_path, monkeypatch):
    cfg_cur = _cfg(tmp_path)
    cfg_cur.paths.database_path = str(tmp_path / "m.db")
    cfg_early = cfg_cur.model_copy(deep=True)
    cfg_early.paths.source_drive = cfg_early.paths.source_drive.replace(CFG_FY, NEXT)
    db = ManifestDB(cfg_cur.paths.database_path)
    monkeypatch.setattr(flow, "now_iso", lambda: LATER)
    blocked = [g.GuardResult("G3_MASS_DELETE", g.BLOCK, "x", {"planned_deletes": 400, "delete_percent": 4.0})]
    return SimpleNamespace(db=db, cur=cfg_cur, early=cfg_early, blocked=blocked, tmp=tmp_path)


def _request(db, fy):
    aid = db.open_approval_request(
        leg="lan", blocked_run_id=f"run-{fy}", fy=fy, phrase="APPROVE", created_at=NOW,
        guards=[{"guard": "G3_MASS_DELETE", "reason": "x",
                 "metrics": {"planned_deletes": 500, "delete_percent": 5.0}}])
    db.approve_request(aid, approved_by="owner", approved_at=NOW, expires_at=EXPIRES)
    return aid


def test_apply_approval_refuses_the_other_fy_and_does_not_consume_it(world):
    aid = _request(world.db, CFG_FY)
    approved, record = flow._apply_approval(world.early, "lan", aid, "run-x", world.blocked)
    assert approved == frozenset()
    assert "approval_rejected" in record and CFG_FY in record["approval_rejected"]
    assert world.db.get_approval(aid)["status"] == ap.APPROVED            # NOT used
    assert world.db.get_approval(aid)["used_at"] is None
    # the folder it belongs to can still use it
    approved, _ = flow._apply_approval(world.cur, "lan", aid, "run-y", world.blocked)
    assert approved == frozenset({"G3_MASS_DELETE"})
    assert world.db.get_approval(aid)["status"] == ap.USED


def test_two_blocked_fys_keep_two_requests_and_a_new_block_supersedes_only_its_own(world):
    a_cur = world.db.open_approval_request(leg="lan", blocked_run_id="r1", fy=CFG_FY, guards=[],
                                           phrase="APPROVE", created_at=NOW)
    a_early = world.db.open_approval_request(leg="lan", blocked_run_id="r2", fy=NEXT, guards=[],
                                             phrase="APPROVE", created_at=NOW)
    assert {r["approval_id"] for r in world.db.open_approvals()} == {a_cur, a_early}
    a_cur2 = world.db.open_approval_request(leg="lan", blocked_run_id="r3", fy=CFG_FY, guards=[],
                                            phrase="APPROVE", created_at=LATER)
    assert {r["approval_id"] for r in world.db.open_approvals()} == {a_early, a_cur2}
    assert world.db.get_approval(a_cur)["status"] == ap.SUPERSEDED


# ── the dashboard re-run carries the FY ───────────────────────────────────────

def test_the_approved_rerun_passes_the_fy_of_the_approval(monkeypatch):
    import ui
    run = AsyncMock(return_value=SimpleNamespace(id="fr1", state=SimpleNamespace(name="Scheduled")))
    monkeypatch.setattr(ui, "arun_deployment", run)
    monkeypatch.setattr(ui, "_trigger_timeout", lambda request: 5)
    monkeypatch.setattr(ui, "_trigger_result", lambda *a, **k: {"ok": True})
    import asyncio
    asyncio.run(ui._start_approved_run({"approval_id": "a1", "leg": "lan", "fy": NEXT}, MagicMock()))
    assert run.call_args.kwargs["parameters"] == {"approval_id": "a1", "fy": NEXT}


def test_a_closed_fys_approval_cannot_be_run(tmp_path, monkeypatch):
    """backup(fy=<closed FY>) is refused (not in the active set), so an approval of it is unusable."""
    cfg = _cfg(tmp_path)
    cfg.paths.database_path = str(tmp_path / "m.db")
    monkeypatch.setattr(flow, "load_config", lambda p: cfg)
    for name in ("configure_logging", "configure_prefect_bridge", "_activate_overlap"):
        monkeypatch.setattr(flow, name, lambda *a, **k: None)
    monkeypatch.setattr(flow, "cleanup_orphaned_robocopy_logs", lambda **k: None)
    with pytest.raises(ValueError, match="not being backed up"):
        flow.backup.fn("config.yaml", "lan", approval_id="a1", fy="FY25-26")


# ── OV-4.4 closed FY ──────────────────────────────────────────────────────────

def _problem_mail(db, fy, leg="lan"):
    db.record_notification({"event_key": f"backup:{fy}:{leg}:{leg.upper()}_FAILED", "kind": "FAILURE",
                            "mode": leg, "status": "SENT", "created_at": NOW, "fy": fy,
                            "audience": "alerts"})


def test_closing_an_fy_closes_its_approvals_and_incidents_silently(tmp_path):
    db = ManifestDB(tmp_path / "m.db")
    pend = db.open_approval_request(leg="lan", blocked_run_id="r1", fy=CFG_FY, guards=[],
                                    phrase="APPROVE", created_at=NOW)
    appr = db.open_approval_request(leg="cloud", blocked_run_id="r2", fy=CFG_FY, guards=[],
                                    phrase="APPROVE", created_at=NOW)
    db.approve_request(appr, approved_by="o", approved_at=NOW, expires_at=EXPIRES)
    other = db.open_approval_request(leg="lan", blocked_run_id="r3", fy=NEXT, guards=[],
                                     phrase="APPROVE", created_at=NOW)
    _problem_mail(db, CFG_FY, "lan")
    _problem_mail(db, CFG_FY, "cloud")
    _problem_mail(db, NEXT, "lan")

    closed = db.close_fy(CFG_FY, now=LATER)

    assert closed == {"approvals": 2, "incidents": 2}
    assert db.get_approval(pend)["status"] == ap.CLOSED
    assert db.get_approval(appr)["status"] == ap.CLOSED
    assert db.get_approval(other)["status"] == ap.PENDING                       # the other FY is untouched
    assert [r["approval_id"] for r in db.open_approvals()] == [other]
    assert db.last_open_incident("lan", fy=CFG_FY) is None                       # closed ...
    assert db.last_open_incident("cloud", fy=CFG_FY) is None
    assert db.last_open_incident("lan", fy=NEXT) is not None                      # ... only for that FY
    # no mail is involved: the closing rows are not SENT mails
    rows = [r for r in db._get_conn().execute("SELECT status, kind FROM notifications WHERE fy = ?", (CFG_FY,))]
    assert ("CLOSED", "RECOVERED") in [(r[0], r[1]) for r in rows]
    # idempotent
    assert db.close_fy(CFG_FY, now=LATER) == {"approvals": 0, "incidents": 0}
    db.close()


def test_a_closed_incident_does_not_count_as_a_delivered_resolved_mail(tmp_path):
    """sent_in_mode_after(recovered=True) is the 'incident ended' test for the NEXT problem of that FY: after
    the FY is closed a later problem of the same key starts a NEW incident (it is not threaded into the old)."""
    db = ManifestDB(tmp_path / "m.db")
    _problem_mail(db, CFG_FY, "lan")
    prior = db.last_notification(f"backup:{CFG_FY}:lan:LAN_FAILED")
    assert db.sent_in_mode_after("lan", prior["id"], recovered=True, fy=CFG_FY) is False
    db.close_fy(CFG_FY, now=LATER)
    assert db.sent_in_mode_after("lan", prior["id"], recovered=True, fy=CFG_FY) is True
    db.close()


def test_the_old_fy_is_closed_whenever_the_switch_is_finished():
    """I1: also on the resume after a crash between the config write and the journal (R3), where the
    switch step itself never runs."""
    import inspect
    src = inspect.getsource(flow._rollover_resume)
    marker = 'final = db.rollover_get(from_fy, to_fy)'
    tail = src[src.index(marker):]
    assert 'rj.CONFIG_SWITCH' in tail
    assert tail.index("_close_old_fy(config, db, from_fy)") < tail.index("_resolve_unprotected_new_fy(")
    assert "_close_old_fy(" not in src[:src.index(marker)]


def test_close_old_fy_never_raises_and_closes_through_the_db(tmp_path):
    db = ManifestDB(tmp_path / "m.db")
    pend = db.open_approval_request(leg="lan", blocked_run_id="r", fy=CFG_FY, guards=[], phrase="APPROVE",
                                    created_at=NOW)
    flow._close_old_fy(None, db, CFG_FY)
    assert db.get_approval(pend)["status"] == ap.CLOSED
    flow._close_old_fy(None, MagicMock(close_fy=MagicMock(side_effect=RuntimeError("locked"))), CFG_FY)
    db.close()


def test_approved_rerun_parameters_match_the_real_flow_signatures():
    """B1: the dashboard's parameters must be accepted by the deployed flows (rollover has no `fy`)."""
    import asyncio

    import ui
    for leg, flow_obj in (("cloud", flow.backup), ("lan", flow.backup), ("rollover", flow.rollover_check_flow)):
        captured = {}

        async def fake_run(name, parameters, timeout, _c=captured):
            _c.update(parameters)
            return SimpleNamespace(id="x", state=SimpleNamespace(name="Scheduled"))

        old = (ui.arun_deployment, ui._trigger_timeout, ui._trigger_result)
        ui.arun_deployment, ui._trigger_timeout, ui._trigger_result = (
            fake_run, lambda request: 5, lambda *a, **k: {"ok": True})
        try:
            asyncio.run(ui._start_approved_run({"approval_id": "a1", "leg": leg, "fy": CFG_FY}, MagicMock()))
        finally:
            ui.arun_deployment, ui._trigger_timeout, ui._trigger_result = old
        flow_obj.validate_parameters(dict(captured))                       # raises on a mismatch
        assert ("fy" in captured) == (leg in ("cloud", "lan"))
