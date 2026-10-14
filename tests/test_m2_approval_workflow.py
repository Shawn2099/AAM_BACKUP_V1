"""M2 2.3 — approval workflow for BLOCKED runs (plan §6.2, owner DEC-3).

BLOCKED run -> one approval request (all blocked guards) + ONE attention
email pointing to the dashboard -> operator approves once (typed phrase)
-> the dashboard re-runs the leg with approval_id -> the rerun re-measures
and proceeds only if every blocked guard is covered and not worse; the
approval is used up before any transfer. Scheduled runs never consume one.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

import flow
from core import approvals as ap
from core import guards as g
from core.manifest import ManifestDB
from models.config import ApprovalsConfig, AppConfig, GuardsConfig

NOW = "2026-10-14T10:00:00+05:30"
LATER = "2026-10-14T11:00:00+05:30"


# ── Config ────────────────────────────────────────────────────────────────

def test_approvals_config_defaults_and_bounds():
    c = ApprovalsConfig()
    assert (c.expiry_hours, c.tolerance_percent) == (12, 0)
    for bad in ({"expiry_hours": 0}, {"tolerance_percent": -1}, {"typo": 1}):
        with pytest.raises(ValidationError):
            ApprovalsConfig(**bad)
    assert "approvals" in AppConfig.model_fields


# ── Pure request logic ────────────────────────────────────────────────────

def _blocked(guard="G3_MASS_DELETE", **metrics):
    metrics = metrics or {"planned_deletes": 600, "delete_percent": 60.0,
                          "destination_files": 1000}
    return g.GuardResult(guard, g.BLOCK, f"{guard} blocked", metrics)


def _request(*results, **over):
    req = {"approval_id": "a1", "leg": "lan", "status": ap.APPROVED,
           "guards": [{"guard": r.guard, "reason": r.reason, "metrics": r.metrics}
                      for r in results],
           "approved_at": NOW, "expires_at": "2026-10-14T22:00:00+05:30",
           "used_at": None, "dismissed_at": None}
    req.update(over)
    return req


def test_request_phrase_names_every_consequence():
    phrase = ap.request_phrase([
        {"guard": "G3_MASS_DELETE", "metrics": {"planned_deletes": 600}},
        {"guard": "G2_SOURCE_SHRINK", "metrics": {"drop_files": 40}},
    ])
    assert phrase == "DELETE 600 / SOURCE SHRANK 40"


def test_request_covers_all_blocked_guards_when_not_worse():
    b = _blocked()
    assert ap.evaluate_request(_request(b), leg="lan", blocked=[b], now_iso=LATER,
                               tolerance_percent=0).allowed


def test_request_rejects_uncovered_guard_worse_numbers_and_other_leg():
    b3 = _blocked()
    b2 = _blocked("G2_SOURCE_SHRINK", drop_files=10, drop_percent=50.0)
    req = _request(b3)
    r = ap.evaluate_request(req, leg="lan", blocked=[b3, b2], now_iso=LATER, tolerance_percent=0)
    assert not r.allowed and "G2_SOURCE_SHRINK" in r.reason
    worse = _blocked(planned_deletes=601, delete_percent=60.1, destination_files=1000)
    assert not ap.evaluate_request(req, leg="lan", blocked=[worse], now_iso=LATER,
                                   tolerance_percent=0).allowed
    assert not ap.evaluate_request(req, leg="cloud", blocked=[b3], now_iso=LATER,
                                   tolerance_percent=0).allowed
    assert not ap.evaluate_request(_request(b3, status=ap.PENDING, approved_at=None),
                                   leg="lan", blocked=[b3], now_iso=LATER,
                                   tolerance_percent=0).allowed


# ── Storage ───────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _open(db, leg="lan", run_id="run-1", guards=None):
    guards = guards or [{"guard": "G3_MASS_DELETE", "reason": "r",
                         "metrics": {"planned_deletes": 600}}]
    return db.open_approval_request(leg=leg, blocked_run_id=run_id, fy="FY26-27",
                                    guards=guards, phrase="DELETE 600", created_at=NOW)


def test_request_lifecycle(db):
    aid = _open(db)
    req = db.get_approval(aid)
    assert req["status"] == ap.PENDING and req["guards"][0]["guard"] == "G3_MASS_DELETE"
    assert [r["approval_id"] for r in db.open_approvals()] == [aid]

    assert db.approve_request(aid, approved_by="10.0.0.2 (session)", approved_at=NOW,
                              expires_at="2026-10-14T22:00:00+05:30")
    assert not db.approve_request(aid, approved_by="x", approved_at=NOW, expires_at=NOW)
    assert db.get_approval(aid)["status"] == ap.APPROVED

    assert db.mark_approval_used(aid, run_id="run-2", used_at=LATER)
    assert not db.mark_approval_used(aid, run_id="run-3", used_at=LATER)  # single use
    used = db.get_approval(aid)
    assert used["status"] == ap.USED and used["used_by_run_id"] == "run-2"
    assert db.open_approvals() == []


def test_new_block_supersedes_pending_request_of_same_leg_only(db):
    old_lan = _open(db, run_id="r1")
    cloud = _open(db, leg="cloud", run_id="r2")
    new_lan = _open(db, run_id="r3")
    assert db.get_approval(old_lan)["status"] == ap.SUPERSEDED
    assert db.get_approval(cloud)["status"] == ap.PENDING
    assert {r["approval_id"] for r in db.open_approvals()} == {cloud, new_lan}


def test_dismiss_and_unknown_ids(db):
    aid = _open(db)
    assert db.dismiss_request(aid, dismissed_by="ip", dismissed_at=NOW)
    assert db.get_approval(aid)["status"] == ap.DISMISSED
    assert not db.approve_request(aid, approved_by="ip", approved_at=NOW, expires_at=LATER)
    assert db.get_approval("nope") is None
    assert not db.mark_approval_used("nope", run_id="r", used_at=NOW)


# ── Pipeline: BLOCKED opens a request + ONE attention email ───────────────

def _pipeline_cfg():
    cfg = MagicMock()
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.lan.max_attempts = 2
    cfg.lan.retry_delay_seconds = 600
    cfg.lan.shutdown_on_all_retries_exhausted = False
    return cfg


def _guard_blocked(leg):
    return g.GuardBlocked(leg, {"fy": "FY26-27", "destination": "D", "results": [
        {"guard": "G1_SOURCE_HAS_FILES", "verdict": g.PASS, "reason": "ok", "metrics": {}},
        {"guard": "G3_MASS_DELETE", "verdict": g.BLOCK,
         "reason": "This backup would delete 600 of 1,000 backup files",
         "metrics": {"planned_deletes": 600, "destination_files": 1000}}]})


@patch("flow.notify")
@patch("flow.ManifestDB")
@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.lan_sync_task")
@patch("flow.lan_access_task", new=MagicMock())
@patch("flow.lan_guards_task")
@patch("flow.lan_snapshot_before_task", return_value={})
@patch("flow.lan_preflight_task")
@patch("flow.wol_check_task")
@patch("flow.health_check_task")
@patch("flow._record_run")
def test_blocked_run_opens_request_and_sends_attention_email(
    _rec, _h, _w, _pre, _snap, guards_task, _sync, _art, mdb, notify,
):
    guards_task.side_effect = _guard_blocked("lan")
    db = mdb.from_config.return_value
    db.open_approval_request.return_value = "a-new"
    with pytest.raises(g.GuardBlocked):
        flow._run_lan_pipeline(_pipeline_cfg(), "run-b", NOW)
    kw = db.open_approval_request.call_args.kwargs
    assert kw["leg"] == "lan" and kw["blocked_run_id"] == "run-b"
    assert [x["guard"] for x in kw["guards"]] == ["G3_MASS_DELETE"]   # blocked only
    assert kw["phrase"] == "DELETE 600"
    event = notify.call_args.args[1]
    assert event.kind == "ACTION_REQUIRED" and event.key == "blocked:lan"
    assert "600 of 1,000" in event.message and event.status == "LAN_BLOCKED"


def test_request_failure_never_breaks_the_run_record():
    cfg = _pipeline_cfg()
    with patch("flow.ManifestDB") as mdb, patch("flow.notify") as notify:
        mdb.from_config.side_effect = OSError("db locked")
        flow._open_block_request(cfg, "lan", "run-x", _guard_blocked("lan").record, NOW)
    notify.assert_called_once()        # the human is still told


def test_generic_failure_alert_skips_guard_blocks(tmp_path):
    cfg = MagicMock()
    cfg.cloud.enabled = False
    cfg.lan.enabled = True
    with patch("flow.load_config", return_value=cfg), \
         patch("flow.configure_logging"), patch("flow.configure_prefect_bridge"), \
         patch("flow.cleanup_orphaned_robocopy_logs"), \
         patch("flow._run_lan_pipeline", side_effect=_guard_blocked("lan")), \
         patch("flow.notify") as generic:
        with pytest.raises(ExceptionGroup):
            flow.backup.fn(config_path="x", mode="lan")
    generic.assert_not_called()


# ── Rerun with approval_id ────────────────────────────────────────────────

def _lan_cfg(tmp_path):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "ledger.xlsx").write_text("x")
    cfg = MagicMock()
    cfg.paths.source_drive = str(src)
    cfg.paths.lan_destination = "\\\\10.0.0.9\\backup\\FY26-27"
    cfg.guards = GuardsConfig()
    cfg.approvals = ApprovalsConfig()
    return cfg


def _counts(total, extras):
    return {"total": total, "copied": 0, "skipped": total, "mismatch": 0,
            "failed": 0, "extras": extras}


def _run_guards_with(tmp_path, approval, planned=600, approval_id="a1", mark_ok=True):
    db = MagicMock()
    db.guard_baseline.return_value = None
    db.get_approval.return_value = approval
    db.mark_approval_used.return_value = mark_ok
    dry = {"ok": True, "counts": _counts(1000, planned), "planned_deletes": planned,
           "canary_present": True}
    with patch("flow.ManifestDB") as mdb, \
         patch("flow.now_iso", return_value=LATER), \
         patch("flow.guards.resolve_host", return_value={"10.0.0.9"}), \
         patch("flow.guards.local_addresses", return_value={"127.0.0.1"}):
        mdb.from_config.return_value = db
        try:
            out = flow.lan_guards_task.fn(_lan_cfg(tmp_path), "FY26-27", dry, 1000,
                                          approval_id=approval_id, run_id="run-2")
        except Exception as e:  # noqa: BLE001 - returned for assertions
            out = e
    return out, db


def _g3_request(planned=600, **over):
    b = g.g3_mass_delete(planned, 1000, 500, 10)
    return _request(b, **over)


def test_approved_rerun_proceeds_once_and_uses_up_the_approval(tmp_path):
    rec, db = _run_guards_with(tmp_path, _g3_request())
    assert isinstance(rec, dict), rec
    assert rec["approved_guards"] == ["G3_MASS_DELETE"] and rec["approval_id"] == "a1"
    db.mark_approval_used.assert_called_once_with("a1", run_id="run-2", used_at=LATER)


def test_rerun_with_worse_numbers_blocks_again_and_keeps_approval(tmp_path):
    out, db = _run_guards_with(tmp_path, _g3_request(), planned=700)
    assert isinstance(out, g.GuardBlocked) and "worse than approved" in str(out)
    db.mark_approval_used.assert_not_called()


def test_approval_lost_race_blocks(tmp_path):
    out, _ = _run_guards_with(tmp_path, _g3_request(), mark_ok=False)
    assert isinstance(out, g.GuardBlocked) and "already used" in str(out)


@pytest.mark.parametrize("approval,approval_id", [
    (None, None),                                   # scheduled run: no approval
    (None, "missing"),                              # unknown id
    ("cloud", "a1"),                                # approval for the other leg
])
def test_no_valid_approval_blocks(tmp_path, approval, approval_id):
    if approval == "cloud":
        approval = _g3_request(leg="cloud")
    out, db = _run_guards_with(tmp_path, approval, approval_id=approval_id)
    assert isinstance(out, g.GuardBlocked)
    db.mark_approval_used.assert_not_called()


def test_backup_flow_hands_approval_id_to_the_pipelines():
    cfg = MagicMock()
    cfg.cloud.enabled = True
    cfg.lan.enabled = True
    with patch("flow.load_config", return_value=cfg), \
         patch("flow.configure_logging"), patch("flow.configure_prefect_bridge"), \
         patch("flow.cleanup_orphaned_robocopy_logs"), patch("flow.ManifestDB"), \
         patch("flow._run_cloud_pipeline") as cloud, patch("flow._run_lan_pipeline") as lan:
        flow.backup.fn(config_path="x", mode="all", approval_id="a1")
    assert cloud.call_args.kwargs["approval_id"] == "a1"
    assert lan.call_args.kwargs["approval_id"] == "a1"


def test_enforce_lets_approved_guards_through_and_logs_them():
    b = g.g3_mass_delete(600, 1000, 500, 10)
    rec = g.enforce("lan", [b], approved_guards=frozenset({"G3_MASS_DELETE"}), fy="FY")
    assert rec["approved_guards"] == ["G3_MASS_DELETE"]
    with pytest.raises(g.GuardBlocked):
        g.enforce("lan", [b], approved_guards=frozenset({"G2_SOURCE_SHRINK"}))
    json.dumps(rec)


def test_existing_database_gets_the_approvals_table(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    first = ManifestDB(path)
    first.open_approvals()                          # connects lazily: force the schema
    first.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE approvals")            # a DB from before 2.3
    conn.commit()
    conn.close()
    db = ManifestDB(path)
    try:
        assert db.open_approvals() == []
    finally:
        db.close()


def test_approved_cloud_mass_delete_backstop_is_the_remeasured_count(tmp_path):
    src = tmp_path / "csrc"
    src.mkdir()
    (src / "ledger.xlsx").write_text("x")
    cfg = MagicMock()
    cfg.paths.source_drive = str(src)
    cfg.cloud.bucket = "bkt"
    cfg.guards = GuardsConfig()
    cfg.approvals = ApprovalsConfig()
    request = _request(g.g3_mass_delete(600, 1000, 500, 10), leg="cloud")
    db = MagicMock()
    db.guard_baseline.return_value = None
    db.get_approval.return_value = request
    db.mark_approval_used.return_value = True

    import contextlib

    @contextlib.contextmanager
    def fake_cfg(*a, **k):
        yield "/tmp/c"

    counts = {str(src): 400, "aam_gcs:bkt/FY26-27": 1000}
    with patch("flow.ManifestDB") as mdb, patch("flow.now_iso", return_value=LATER), \
         patch("flow.temp_rclone_config", fake_cfg), \
         patch("flow.count_objects", side_effect=lambda t, *a, **k: counts[t]), \
         patch("flow.run_cloud_sync", return_value={"status": "CLOUD_COMPLETE",
                                                    "planned_deletes": 600}):
        mdb.from_config.return_value = db
        rec = flow.cloud_guards_task.fn(cfg, "FY26-27", None, approval_id="a1", run_id="r2")
    assert rec["approved_guards"] == ["G3_MASS_DELETE"]
    assert rec["max_delete"] == 600


# ── 2.3d: G5 mount marker -> BLOCKED; approval creates the marker ─────────

def test_missing_marker_no_longer_fails_preflight_it_is_measured(tmp_path):
    from core import lan_preflight

    dest = tmp_path / "dest"
    dest.mkdir()
    from test_support.fake_tools import fake_run_tool
    with fake_run_tool("core.lan_preflight", log_encoding="utf-16", exit_code=0) as calls:
        res = lan_preflight.run_lan_dry_run(str(tmp_path / "src"), str(dest))
    assert res["ok"] is True and res["canary_present"] is False
    assert "/L" in calls[0]["argv"]                      # read-only listing still runs
    (dest / ".AAM_TARGET_MOUNTED").write_text("")
    with fake_run_tool("core.lan_preflight", log_encoding="utf-16", exit_code=0):
        assert lan_preflight.run_lan_dry_run(str(tmp_path / "src"), str(dest))["canary_present"]


def _g5_rig(tmp_path, canary, dest_files, approval=None):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "ledger.xlsx").write_text("x")
    dest = tmp_path / "nas"
    dest.mkdir(exist_ok=True)
    cfg = MagicMock()
    cfg.paths.source_drive = str(src)
    cfg.paths.lan_destination = str(dest)
    cfg.guards = GuardsConfig(forbid_local_destination=False)
    cfg.approvals = ApprovalsConfig()
    db = MagicMock()
    db.guard_baseline.return_value = None
    db.get_approval.return_value = approval
    db.mark_approval_used.return_value = True
    dry = {"ok": True, "counts": _counts(10, 0), "planned_deletes": 0}
    if canary != "absent-key":
        dry["canary_present"] = canary
    with patch("flow.ManifestDB") as mdb, patch("flow.now_iso", return_value=LATER):
        mdb.from_config.return_value = db
        try:
            out = flow.lan_guards_task.fn(cfg, "FY26-27", dry, dest_files,
                                          approval_id="a1" if approval else None, run_id="r2")
        except Exception as e:  # noqa: BLE001
            out = e
    return out, dest


@pytest.mark.parametrize("canary,dest_files,match", [
    (False, 10, "already holds files"),
    (False, 0, "initialise"),
    ("absent-key", 10, "Could not check"),
])
def test_g5_blocks_missing_or_unknown_marker(tmp_path, canary, dest_files, match):
    out, dest = _g5_rig(tmp_path, canary, dest_files)
    assert isinstance(out, g.GuardBlocked) and match in str(out)
    assert not (dest / ".AAM_TARGET_MOUNTED").exists()


def test_g5_approval_creates_the_marker_and_proceeds(tmp_path):
    dest = tmp_path / "nas"
    g5 = g.g5_canary(False, True, str(dest))
    out, dest = _g5_rig(tmp_path, False, 10, approval=_request(g5))
    assert isinstance(out, dict), out
    assert out["approved_guards"] == ["G5_CANARY"] and out["marker_created"] is True
    assert (dest / ".AAM_TARGET_MOUNTED").is_file()
