"""FY overlap review point cards (agent_runs/OVERLAP_REVIEW.md section 2): P-1, P-2, P-5, P-6, P-8, P-9,
P-10, P-11, P-13 and T-b4.

Every oracle is independent of the code under test: fixed dates, literal FY names, folder listings,
hand-written DB rows read with sqlite3, and fake-pipeline counters. Defect-proving cards (P-6=D6, P-8=D8,
P-10=D12, and the new P-11 finding) are xfail(strict=False) with the correct expectation; T-b4 and P-9 are
characterization tests of the current behaviour (no xfail).
"""

import contextlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pendulum
import pytest
from pydantic import ValidationError

import flow
from core import approvals as ap
from core import fy_overlap as ov
from core import guards as g
from core import report
from core import rollover_journal as rj
from core.health import CLOCK_OK
from core.manifest import ManifestDB
from core.report_period import ReportPeriod
from core.time_utils import IST
from models.config import PathsConfig
from tests.test_ov2_fy_overlap import CFG_FY, NEXT, _cfg

NAS_CFG = "\\\\nas\\share\\" + CFG_FY
NAS_NEXT = "\\\\nas\\share\\" + NEXT
RUN_DATE = pendulum.date(2027, 1, 1)
ACTIVATED_AT = "2027-01-01T02:01:00+05:30"


class Clock:
    """clock_check double: always proven."""

    def __call__(self):
        return SimpleNamespace(state=CLOCK_OK, reason="", to_metrics=lambda: {"state": CLOCK_OK})


class Mail:
    """notify_fn double: collects the events, reports every mail as SENT."""

    def __init__(self):
        self.events = []

    def __call__(self, config, event, db=None):
        self.events.append(event)
        return SimpleNamespace(status="SENT")

    def keys(self):
        return [e.key for e in self.events]


def _listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def _rows(db_path, sql, args=()):
    """Read-only oracle: a fresh sqlite3 connection, not the code under test."""
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _same_path(a, b) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


@pytest.fixture
def env(tmp_path):
    """Configured FY26-27 in tmp (its folder exists), an empty DB, and the source root."""
    src = tmp_path / "src"
    (src / CFG_FY).mkdir(parents=True)
    cfg = _cfg(tmp_path, source=str(src / CFG_FY), nas=NAS_CFG)
    cfg.paths.database_path = str(tmp_path / "m.db")
    db = ManifestDB(cfg.paths.database_path)
    yield SimpleNamespace(cfg=cfg, db=db, src=src, tmp=tmp_path, db_path=cfg.paths.database_path)
    with contextlib.suppress(Exception):
        db.close()


# ── P-1 ───────────────────────────────────────────────────────────────────────

def test_p1_adopted_early_folder_keeps_its_files_gets_no_placeholder_and_uses_the_upper_prefix(env):
    """Setup: early folder already on disk as `fy27-28` holding a.xlsx. Action: activation, then the
    cloud leg's view of that folder. Expected: listing is only a.xlsx; the row keeps the real spelling;
    the cloud prefix is FY27-28 in upper case."""
    early = env.src / "fy27-28"
    early.mkdir()
    (early / "a.xlsx").write_bytes(b"ledger")

    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())

    assert res["state"] == "activated"
    assert _listing(early) == ["a.xlsx"]                                   # no placeholder written
    row = env.db.get_activation(NEXT)
    assert row["created_by_app"] == 0
    assert _same_path(row["source_path"], early) and row["source_path"].endswith("fy27-28")
    view = ov.folder_view(env.cfg, NEXT)
    assert view.paths.source_drive.endswith(NEXT)                           # cloud source folder
    assert flow._effective_fy_prefix(view) == "FY27-28"                    # cloud prefix, upper case


# ── P-2 ───────────────────────────────────────────────────────────────────────

def test_p2_placeholder_failure_defers_then_the_next_night_activates_with_one_info_mail(env, monkeypatch):
    """Night 1: the folder is created, the placeholder write raises. Night 2: the write works.
    Expected: night 1 no activation row, the pending marker holds the folder; night 2 a row with
    created_by_app=1 and exactly one INFO start mail (no deferral mail: it was never past the hours)."""
    real_write = ov.write_placeholder_if_empty
    calls = []

    def flaky(folder, fy, name):
        calls.append(str(folder))
        if len(calls) == 1:
            raise OSError("the disk was busy")
        return real_write(folder, fy, name)

    monkeypatch.setattr(ov, "write_placeholder_if_empty", flaky)
    mail = Mail()
    folder = env.src / NEXT

    night1 = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=mail)
    assert night1["state"] == "deferred"
    assert folder.is_dir()                                                  # created on night 1
    assert env.db.get_activation(NEXT) is None
    assert _same_path(env.db._meta(f"overlap_created_pending:{NEXT}"), folder)
    assert mail.events == []

    night2 = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=mail)
    assert night2["state"] == "activated"
    row = env.db.get_activation(NEXT)
    assert row["created_by_app"] == 1
    assert _listing(folder) == [env.cfg.rollover.source_placeholder_name]
    assert mail.keys() == [f"overlap:start:{NEXT}"]
    assert mail.events[0].kind == "INFO"


# ── P-5 ───────────────────────────────────────────────────────────────────────

def test_p5_prefilled_cloud_prefix_is_a_first_run_block_then_one_approval_then_normal(env, monkeypatch):
    """Setup: the early folder has 10 files; gs://bucket/FY27-28 already holds those 10 plus 3 objects
    that are not in the source. Night 1 (no baseline) must BLOCK on G3 (first backup), one approval
    request exists; after approval the run may delete exactly the 3 planned objects (max_delete 3); the
    night after that, with a baseline, runs normally with nothing approved."""
    early = env.src / NEXT
    early.mkdir()
    for i in range(10):
        (early / f"f{i:02d}.xlsx").write_bytes(b"x")
    view = ov.folder_view(env.cfg, NEXT)
    state = {"dst": 13, "extra": 3}          # destination objects; objects the dry run would delete

    def count(target, *args, **kwargs):
        return state["dst"] if str(target).startswith("aam_gcs:") else 10

    def dry_run(**kwargs):
        return {"status": "CLOUD_COMPLETE", "error": None, "planned_deletes": state["extra"]}

    monkeypatch.setattr(flow, "count_objects", count)
    monkeypatch.setattr(flow, "run_cloud_sync", dry_run)
    monkeypatch.setattr(flow, "temp_rclone_config", lambda *a, **k: contextlib.nullcontext("rclone.conf"))
    notes = []
    monkeypatch.setattr(flow, "notify", lambda config, event, db=None: notes.append(event))

    # night 1: first run into a filled prefix
    with pytest.raises(g.GuardBlocked) as ei:
        flow.cloud_guards_task.fn(view, NEXT, None, run_id="night-1")
    assert "first backup of this folder" in str(ei.value)
    rec1 = ei.value.record
    g3 = next(r for r in rec1["results"] if r["guard"] == "G3_MASS_DELETE")
    assert g3["verdict"] == g.BLOCK
    assert g3["metrics"]["planned_deletes"] == 3 and g3["metrics"]["destination_files"] == 13
    flow._open_block_request(view, "cloud", "night-1", rec1, ACTIVATED_AT)

    db = ManifestDB(env.db_path)
    pending = db.open_approvals()
    assert len(pending) == 1 and pending[0]["fy"] == NEXT and pending[0]["leg"] == "cloud"
    aid = pending[0]["approval_id"]
    now = pendulum.now(IST)
    assert db.approve_request(aid, approved_by="test", approved_at=now.isoformat(),
                              expires_at=now.add(hours=12).isoformat())
    db.close()

    # approved night: the backstop allows exactly the 3 planned deletes, and the approval is used up
    rec2 = flow.cloud_guards_task.fn(view, NEXT, None, approval_id=aid, run_id="night-2")
    assert rec2["approved_guards"] == ["G3_MASS_DELETE"]
    assert rec2["max_delete"] == 3
    state["dst"] -= min(rec2["max_delete"], state["extra"])                # the mirror removes those 3
    assert state["dst"] == 10
    db = ManifestDB(env.db_path)
    assert db.get_approval(aid)["status"] == ap.USED
    # the next night has a baseline (the approved night's successful run row) and nothing to delete
    db.insert_run({"run_id": "night-2-ok", "mode": "cloud", "fy": NEXT, "started_at": ACTIVATED_AT,
                   "ended_at": ACTIVATED_AT, "status": "CLOUD_COMPLETE",
                   "extended_metrics": json.dumps({"guards": rec2})})
    db.close()
    state["extra"] = 0
    rec3 = flow.cloud_guards_task.fn(view, NEXT, None, run_id="night-3")
    assert "approved_guards" not in rec3
    assert rec3["source_file_count"] == 10 and rec3["destination_files"] == 10


# ── P-6 (D6) ──────────────────────────────────────────────────────────────────

def test_p6_a_skipped_config_switch_counts_as_finished_for_the_overlap(env):
    env.db.record_activation(NEXT, activated_at=ACTIVATED_AT, source_path="x", created_by_app=True)
    env.db.rollover_open(CFG_FY, NEXT, ACTIVATED_AT)
    env.db.rollover_step_skipped(CFG_FY, NEXT, rj.CONFIG_SWITCH, ACTIVATED_AT, "not applicable here")
    step = env.db.rollover_get(CFG_FY, NEXT)["steps"][rj.CONFIG_SWITCH]
    assert rj.is_finished(step) is True                                    # the premise, from the journal rule
    assert ov.active_folders(env.cfg, env.db, pendulum.date(2027, 4, 2)) == [CFG_FY]


# ── P-8 (D8) ──────────────────────────────────────────────────────────────────

@pytest.mark.xfail(strict=False, reason="DEFECT D8: manifest.close_fy closes only the cloud and lan incidents "
                   "(manifest.py:1238); FY26-27's open audit incident stays open after the 1 April switch")
def test_p8_the_switch_closes_the_open_audit_incident_of_the_old_fy(env):
    for mode, key in (("cloud", f"cloud:{CFG_FY}:upload"), ("audit", f"audit:{CFG_FY}:all:cloud=VERIFICATION_FAILED")):
        env.db.record_notification({
            "event_key": key, "kind": "ACTION_REQUIRED", "mode": mode, "subject": f"{mode} problem",
            "status": "SENT", "created_at": "2027-03-28T10:00:00+05:30", "fy": CFG_FY})
    env.db.close_fy(CFG_FY, now="2027-04-01T06:00:00+05:30")
    env.db.close()
    closed = _rows(env.db_path,
                   "SELECT mode FROM notifications WHERE fy = ? AND kind = 'RECOVERED' AND status = 'CLOSED' "
                   "ORDER BY mode", (CFG_FY,))
    assert [m for (m,) in closed] == ["audit", "cloud"]                    # both incidents end silently


# ── P-9 (D11) ─────────────────────────────────────────────────────────────────

def test_p9_a_mapped_drive_nas_path_is_refused_by_the_path_field_with_its_own_reason(tmp_path):
    """Characterization. The drive-letter refusal comes from PathsConfig's own field validator
    (models/config.py:117), which runs before the share-root check at config.py:1308 that D11 names.
    The 1308 check is therefore not what refuses `Z:` today."""
    with pytest.raises(ValidationError) as ei:
        PathsConfig(source_drive="D:\\FY26-27", lan_destination="Z:\\FY26-27",
                    gcs_key_path=str(tmp_path / "k.json"))
    assert "drive letter does not work" in str(ei.value)
    with pytest.raises(ValueError, match="drive letter does not work"):
        _cfg(tmp_path, nas="Z:\\FY26-27")                                  # overlap on (the default)


def test_p9_a_share_root_nas_path_is_refused_only_while_the_overlap_is_on(tmp_path):
    """Characterization of models/config.py:1308-1316: \\\\nas\\FY26-27 is the share root itself."""
    with pytest.raises(ValueError, match="share root itself"):
        _cfg(tmp_path, nas="\\\\nas\\FY26-27")
    _cfg(tmp_path, nas="\\\\nas\\FY26-27", overlap_enabled=False)         # overlap off: accepted


# ── P-10 (D12) ────────────────────────────────────────────────────────────────

def _activated_early_fy(env):
    ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    assert env.db.get_activation(NEXT) is not None


def test_p10_control_with_the_start_month_unchanged_the_activated_fy_stays_active_in_february(env):
    _activated_early_fy(env)
    assert ov.active_folders(env.cfg, env.db, pendulum.date(2027, 2, 6)) == [NEXT, CFG_FY]


@pytest.mark.xfail(strict=False, reason="DEFECT D12: fy_overlap.in_window reads early_start_month on every run "
                   "(fy_overlap.py:134), so raising it to 3 on 5 Feb drops the activated FY27-28 until 1 March; "
                   "the activation row is still there")
def test_p10_raising_early_start_month_in_february_keeps_the_activated_fy_active(env):
    _activated_early_fy(env)
    changed = env.cfg.model_copy(deep=True)
    changed.rollover.early_start_month = 3                                 # Settings change on 5 Feb
    assert env.db.get_activation(NEXT) is not None
    assert ov.active_folders(changed, env.db, pendulum.date(2027, 2, 6)) == [NEXT, CFG_FY]


# ── P-11 (new: not in D1-D13) ─────────────────────────────────────────────────

@pytest.fixture
def lan_night(env, monkeypatch):
    """One LAN flow night with the two folders, real _record_running / _record_run / report reads, fake
    pipelines that advance a fake now_iso() the way a long first folder would."""
    env.db.record_activation(NEXT, activated_at=ACTIVATED_AT, source_path=str(env.src / NEXT),
                             created_by_app=True, clock_state="OK")
    env.cfg.schedule.lan_cron = "0 21 * * *"
    env.cfg.lan.shutdown_after_backup = False
    clock = {"now": "2027-03-31T21:01:00+05:30"}
    seen = []
    real_record_run = flow._record_run

    def pipeline(config, run_id, started_at, monotonic_start, **kw):
        fy = flow._effective_fy_prefix(config)
        seen.append((fy, started_at))
        if fy == NEXT:
            clock["now"] = "2027-04-01T03:10:00+05:30"                     # the first folder runs to 03:10
        real_record_run(config, run_id, "lan", started_at, "LAN_COMPLETE", 0, None)

    monkeypatch.setattr(flow, "load_config", lambda p: env.cfg)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow, "configure_prefect_bridge", lambda *a, **k: None)
    monkeypatch.setattr(flow, "cleanup_orphaned_robocopy_logs", lambda **k: None)
    monkeypatch.setattr(flow, "_activate_overlap", lambda *a, **k: None)
    monkeypatch.setattr(flow, "_flow_run_prefix", lambda: "p11")
    monkeypatch.setattr(flow, "_scheduled_time", lambda: pendulum.datetime(2027, 3, 31, 21, 1, tz="Asia/Kolkata"))
    monkeypatch.setattr(flow, "_scheduled_lateness_seconds", lambda: 0.0)
    monkeypatch.setattr(flow, "now_iso", lambda: clock["now"])
    monkeypatch.setattr(flow, "_backup_slot", lambda config: contextlib.nullcontext())
    monkeypatch.setattr(flow, "_run_lan_pipeline", pipeline)
    monkeypatch.setattr(flow, "_lan_shutdown_decision", lambda config, votes, folders: None)
    monkeypatch.setattr(flow, "notify", lambda config, event, db=None: None)
    monkeypatch.setattr(ov, "run_date", lambda scheduled=None: pendulum.date(2027, 3, 31))
    return SimpleNamespace(env=env, seen=seen, clock=clock)


@pytest.mark.xfail(strict=False, reason="NEW (not D1-D13): _run_leg_folders passes now_iso() per folder "
                   "(flow.py:3290, pipeline(view, run_id, now_iso(), ...)), so the second folder's row is dated "
                   "when its pipeline starts (here 1 April 03:10), not the scheduled 31 March night. Confirmed "
                   "on the row assertion; the March-report consequence is expected but not yet observed")
def test_p11_both_folders_of_a_night_started_at_the_scheduled_time_and_the_march_report_counts_both(lan_night):
    """Setup: LAN scheduled 2027-03-31 21:01; the first folder (FY27-28) runs to 04-01 03:10. Action: the
    LAN leg (both folders). Expected: both rows are dated 31 March (scheduled start, as a late run keeps
    it); the March monthly report counts 31 of 31 nights for both FYs."""
    env = lan_night.env
    for fy in (CFG_FY, NEXT):
        d = pendulum.date(2027, 3, 1)
        while d <= pendulum.date(2027, 3, 30):
            night = pendulum.datetime(d.year, d.month, d.day, 21, 0, tz=IST).isoformat()
            env.db.insert_run({"run_id": f"seed-{fy}-{d}", "mode": "lan", "fy": fy, "started_at": night,
                               "ended_at": night,
                               "status": "LAN_COMPLETE", "files_copied": 1, "bytes_copied": 1})
            d = d.add(days=1)
    flow.backup.fn("config.yaml", "lan")
    env.db.close()

    rows = _rows(env.db_path, "SELECT fy, started_at FROM run_history WHERE run_id LIKE 'p11-%'")
    assert sorted(rows) == sorted([(CFG_FY, "2027-03-31T21:01:00+05:30"), (NEXT, "2027-03-31T21:01:00+05:30")])

    db = ManifestDB(env.db_path)
    cfg = env.cfg
    rp = ReportPeriod.previous_month(pendulum.datetime(2027, 4, 1, 8, tz=IST))
    model = report.build_report_model(db, rp, legs=("lan",), schedule=cfg.schedule, config=cfg)
    db.close()
    by_fy = {s["fy"]: s for s in model["sections"]}
    for fy in (NEXT, CFG_FY):
        assert by_fy[fy]["summaries"][0]["scheduled"] == 31
        assert by_fy[fy]["summaries"][0]["total"] == 31


# ── P-13 ──────────────────────────────────────────────────────────────────────

def test_p13_integrity_audit_after_the_switch_audits_only_the_new_fy(tmp_path, monkeypatch):
    """Setup: config switched on 1 April (configured FY27-28); FY27-28 activation row kept. Action: the weekly
    audit on Sunday 2027-04-04. Expected: only FY27-28 is audited (cloud and lan), no integrity_audits row
    for FY26-27."""
    src = tmp_path / "src"
    (src / NEXT).mkdir(parents=True)
    cfg = _cfg(tmp_path, source=str(src / NEXT), nas=NAS_NEXT)
    cfg.paths.database_path = str(tmp_path / "m.db")
    cfg.lan.shutdown_after_backup = True
    cfg.wol.enabled = True
    db = ManifestDB(cfg.paths.database_path)
    db.record_activation(NEXT, activated_at=ACTIVATED_AT, source_path=str(src / NEXT),
                         created_by_app=True, clock_state="OK")
    db.close()

    events, shutdowns, alerts = [], [], []

    def audit(leg):
        def run(source, *args, **kwargs):
            events.append(f"{leg}:{os.path.basename(str(source).rstrip(chr(92)))}")
            return {"status": "VERIFIED", "scope": "full", "files_checked": 1, "bytes_checked": None,
                    "mismatches": 0, "detail": "verified clean"}
        return run

    monkeypatch.setattr(flow, "load_config", lambda p: cfg)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow, "configure_prefect_bridge", lambda *a, **k: None)
    monkeypatch.setattr(flow, "_scheduled_time", lambda: pendulum.datetime(2027, 4, 4, 13, 0, tz="Asia/Kolkata"))
    monkeypatch.setattr(flow, "_open_run_artefacts", lambda c, i: None)
    monkeypatch.setattr(flow, "temp_rclone_config", lambda *a, **k: contextlib.nullcontext("rc"))
    monkeypatch.setattr(flow, "notify", lambda config, event, db=None: alerts.append(event))
    monkeypatch.setattr(flow, "_backup_slot", lambda config: contextlib.nullcontext())
    monkeypatch.setattr(flow, "wol_check_task", lambda config: events.append("wol"))
    monkeypatch.setattr(flow, "lan_shutdown_task", lambda *a, **k: shutdowns.append(k or a))
    monkeypatch.setattr("core.integrity.audit_cloud", audit("cloud"))
    monkeypatch.setattr("core.integrity.audit_lan", audit("lan"))

    flow.integrity_audit_flow.fn("config.yaml", "all")

    assert [e for e in events if e != "wol"] == [f"cloud:{NEXT}", f"lan:{NEXT}"]
    got = _rows(cfg.paths.database_path, "SELECT fy, mode FROM integrity_audits ORDER BY rowid")
    assert got and {fy for fy, _mode in got} == {NEXT}
    assert sorted(mode for _fy, mode in got) == ["cloud", "lan"]
    assert len(shutdowns) == 1 and alerts == []


# ── T-b4 (D4, owner decision: characterization) ────────────────────────────────

class _SpentBudget(flow.RunBudget):
    """RunBudget whose remaining time is what the early folder has spent (the 2 GB first upload of D4 is
    represented by the early folder spending the whole cloud budget)."""

    spent = 0.0

    def remaining(self, clock=None):
        return self.seconds - _SpentBudget.spent


def test_t_b4_characterization_early_fy_uses_the_leg_budget_and_the_configured_fy_is_missed_each_night(env, monkeypatch):
    """Characterization of D4 (current behaviour, owner decision pending): cloud_max_hours is small, the
    early FY27-28 spends all of it on each of three nights, so FY26-27 gets CLOUD_MISSED every night.
    Expected rows (documented, not a bug verdict): three CLOUD_MISSED rows for FY26-27 and three
    CLOUD_PARTIAL rows for FY27-28."""
    env.db.record_activation(NEXT, activated_at="2027-01-05T02:01:00+05:30", source_path=str(env.src / NEXT),
                             created_by_app=True, clock_state="OK")
    (env.src / NEXT).mkdir()
    env.cfg.budget.cloud_max_hours = 0.5
    budget_seconds = 0.5 * 3600
    real_pipeline = flow._run_cloud_pipeline
    notes = []

    def pipeline(config, run_id, started_at, monotonic_start, **kw):
        fy = flow._effective_fy_prefix(config)
        if fy == NEXT:
            _SpentBudget.spent = budget_seconds                            # the early folder uses it all
            flow._record_run(config, run_id, "cloud", started_at, "CLOUD_PARTIAL", 0,
                             "catching up: the cloud budget was used up")
            return
        return real_pipeline(config, run_id, started_at, monotonic_start, **kw)

    monkeypatch.setattr(flow, "load_config", lambda p: env.cfg)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow, "configure_prefect_bridge", lambda *a, **k: None)
    monkeypatch.setattr(flow, "cleanup_orphaned_robocopy_logs", lambda **k: None)
    monkeypatch.setattr(flow, "_activate_overlap", lambda *a, **k: None)
    monkeypatch.setattr(flow, "_flow_run_prefix", lambda: "tb4")
    monkeypatch.setattr(flow, "_scheduled_lateness_seconds", lambda: 0.0)
    monkeypatch.setattr(flow, "_backup_slot", lambda config: contextlib.nullcontext())
    monkeypatch.setattr(flow, "_run_cloud_pipeline", pipeline)
    monkeypatch.setattr(flow, "RunBudget", _SpentBudget)
    monkeypatch.setattr(flow, "notify", lambda config, event, db=None: notes.append(event))

    for night in range(3):
        _SpentBudget.spent = 0.0
        day = pendulum.datetime(2027, 1, 5 + night, 2, 1, tz="Asia/Kolkata")
        monkeypatch.setattr(flow, "_scheduled_time", lambda day=day: day)
        # each night is its own flow run (run ids come from the flow-run prefix)
        monkeypatch.setattr(flow, "_flow_run_prefix", lambda n=night: f"tb4-night{n}")
        with pytest.raises(ExceptionGroup):
            flow.backup.fn("config.yaml", "cloud")

    missed = _rows(env.cfg.paths.database_path,
                   "SELECT COUNT(*) FROM run_history WHERE mode = 'cloud' AND fy = ? AND status = 'CLOUD_MISSED'",
                   (CFG_FY,))[0][0]
    partial = _rows(env.cfg.paths.database_path,
                    "SELECT COUNT(*) FROM run_history WHERE mode = 'cloud' AND fy = ? AND status = 'CLOUD_PARTIAL'",
                    (NEXT,))[0][0]
    assert missed == 3
    assert partial == 3
    assert any(n.key.startswith("missed:") for n in notes)
