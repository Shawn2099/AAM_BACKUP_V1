"""WP8 - the integrity audit per FY folder (plan 5.8, R-9): every active folder is audited, new FY first,
under ONE backup slot, ONE audit_max_hours budget and ONE NAS wake / shutdown; rows, audit ids, the 'waiting
for the next backup' reference and the alerts are per FY; one folder's failure never stops the other."""
from contextlib import contextmanager
from unittest.mock import MagicMock

import pendulum
import pytest

import flow
from core.budget import BudgetExceeded
from core.manifest import ManifestDB
from tests.test_ov2_fy_overlap import CFG_FY, NEXT, _cfg

JAN = pendulum.datetime(2027, 1, 5, 2, 1, tz="Asia/Kolkata")
VERIFIED = {"status": "VERIFIED", "scope": "full", "files_checked": 10, "bytes_checked": None,
            "mismatches": 0, "detail": "verified clean"}
FAILED = {"status": "VERIFICATION_FAILED", "scope": "full", "files_checked": 10, "bytes_checked": None,
          "mismatches": 2, "detail": "2 files differ"}


class Rig:
    def __init__(self):
        self.events = []         # ordered: slot, wol, audit calls, shutdown
        self.cloud = []          # (source, prefix, pending)
        self.lan = []            # (source, dest, pending)
        self.shutdowns = []
        self.alerts = []
        self.results = {}        # (leg, fy) -> dict | Exception
        self.pending_calls = []  # (leg, fy) asked of db.last_successful_run
        self.slot_error = None
        self.wol_error = None


def _fy_of_path(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\").split("\\")[-1]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig()
    cfg = _cfg(tmp_path)
    cfg.paths.database_path = str(tmp_path / "m.db")
    cfg.lan.shutdown_after_backup = True
    cfg.wol.enabled = True
    r.cfg = cfg
    db = ManifestDB(cfg.paths.database_path)
    db.record_activation(NEXT, activated_at=JAN.isoformat(), source_path=f"D:\\{NEXT}",
                         created_by_app=True, clock_state="OK")
    db.close()
    r.db_path = cfg.paths.database_path

    monkeypatch.setattr(flow, "load_config", lambda p: cfg)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow, "configure_prefect_bridge", lambda *a, **k: None)
    monkeypatch.setattr(flow, "_scheduled_time", lambda: JAN)
    monkeypatch.setattr(flow, "_open_run_artefacts", lambda c, i: None)
    monkeypatch.setattr(flow, "temp_rclone_config", MagicMock())
    monkeypatch.setattr(flow, "notify", lambda c, ev: r.alerts.append(ev))

    orig = ManifestDB.last_successful_run

    def spy(self, leg, *a, **k):
        r.pending_calls.append((leg, k.get("fy")))
        return orig(self, leg, *a, **k)

    monkeypatch.setattr(ManifestDB, "last_successful_run", spy)

    @contextmanager
    def slot(config):
        if r.slot_error is not None:
            raise r.slot_error
        r.events.append("slot+")
        try:
            yield
        finally:
            r.events.append("slot-")

    monkeypatch.setattr(flow, "_backup_slot", slot)

    def wol(config):
        r.events.append("wol")
        if r.wol_error is not None:
            raise r.wol_error

    monkeypatch.setattr(flow, "wol_check_task", wol)
    monkeypatch.setattr(flow, "lan_shutdown_task",
                        lambda config, d=None: r.shutdowns.append(d) or r.events.append("shutdown"))

    def fake(leg):
        def audit(source, *args, **kw):
            fy = _fy_of_path(source)
            r.events.append(f"{leg}:{fy}")
            (r.cloud if leg == "cloud" else r.lan).append((source, args, kw))
            res = r.results.get((leg, fy), dict(VERIFIED))
            if isinstance(res, Exception):
                raise res
            return dict(res)
        return audit

    monkeypatch.setattr("core.integrity.audit_cloud", fake("cloud"))
    monkeypatch.setattr("core.integrity.audit_lan", fake("lan"))
    return r


def run(r, mode="all", **kw):
    return flow.integrity_audit_flow.fn("x.yaml", mode, **kw)


def rows(r):
    db = ManifestDB(r.db_path)
    try:
        return db._get_conn().execute(
            "SELECT audit_id, mode, fy, status, detail FROM integrity_audits ORDER BY rowid").fetchall()
    finally:
        db.close()


# ── folders, order, rows ──────────────────────────────────────────────────────

def test_both_folders_are_audited_new_fy_first_under_one_slot_and_one_shutdown(rig):
    run(rig)
    assert rig.events == ["slot+", f"cloud:{NEXT}", f"cloud:{CFG_FY}",
                          "wol", f"lan:{NEXT}", "wol", f"lan:{CFG_FY}", "shutdown", "slot-"]
    assert len(rig.shutdowns) == 1


def test_each_folder_gets_its_own_source_destination_prefix_and_row(rig):
    run(rig)
    assert [c[0] for c in rig.cloud] == [f"D:\\{NEXT}", f"D:\\{CFG_FY}"]
    assert [c[1][1] for c in rig.cloud] == [NEXT, CFG_FY]            # (bucket, prefix, ...)
    assert [(c[0], c[1][0]) for c in rig.lan] == [
        (f"D:\\{NEXT}", f"\\\\nas\\share\\{NEXT}"), (f"D:\\{CFG_FY}", f"\\\\nas\\share\\{CFG_FY}")]
    got = rows(rig)
    assert [(m, fy, s) for _a, m, fy, s, _d in got] == [
        ("cloud", NEXT, "VERIFIED"), ("cloud", CFG_FY, "VERIFIED"),
        ("lan", NEXT, "VERIFIED"), ("lan", CFG_FY, "VERIFIED")]
    for aid, m, fy, _s, _d in got:
        assert aid.endswith(f"-{m}-{fy}-full")


def test_pending_reference_is_each_fys_last_successful_run(rig):
    run(rig)
    assert sorted(rig.pending_calls) == sorted(
        [("cloud", NEXT), ("cloud", CFG_FY), ("lan", NEXT), ("lan", CFG_FY)])


def test_one_active_fy_behaves_as_today_with_the_fy_in_the_audit_id(rig, monkeypatch):
    monkeypatch.setattr(flow, "_active_fys", lambda c: [CFG_FY])
    run(rig)
    assert rig.events == ["slot+", f"cloud:{CFG_FY}", "wol", f"lan:{CFG_FY}", "shutdown", "slot-"]
    assert [(m, fy) for _a, m, fy, _s, _d in rows(rig)] == [("cloud", CFG_FY), ("lan", CFG_FY)]
    assert all(a.endswith(f"-{m}-{CFG_FY}-full") for a, m, *_ in rows(rig))
    assert rig.alerts == []


# ── fy parameter ──────────────────────────────────────────────────────────────

def test_fy_narrows_the_audit_to_that_folder(rig):
    run(rig, fy=CFG_FY)
    assert rig.events == ["slot+", f"cloud:{CFG_FY}", "wol", f"lan:{CFG_FY}", "shutdown", "slot-"]
    assert {fy for _a, _m, fy, _s, _d in rows(rig)} == {CFG_FY}


def test_fy_outside_the_active_set_is_refused_and_nothing_runs(rig):
    with pytest.raises(ValueError, match="not being backed up right now.*refusing to run it"):
        run(rig, fy="FY25-26")
    assert rig.events == [] and rows(rig) == []


# ── failures and alerts per FY ────────────────────────────────────────────────

def test_one_folders_failure_never_stops_the_other_and_alerts_per_fy(rig):
    rig.results[("cloud", NEXT)] = FAILED
    rig.results[("lan", CFG_FY)] = RuntimeError("share vanished")
    with pytest.raises(ExceptionGroup) as ei:
        run(rig)
    assert len(ei.value.exceptions) == 2
    assert [(m, fy, s) for _a, m, fy, s, _d in rows(rig)] == [
        ("cloud", NEXT, "VERIFICATION_FAILED"), ("cloud", CFG_FY, "VERIFIED"),
        ("lan", NEXT, "VERIFIED"), ("lan", CFG_FY, "AUDIT_INCOMPLETE")]
    keys = [(a.key, a.fy, a.kind) for a in rig.alerts]
    assert keys == [(f"audit:{NEXT}:all:cloud=VERIFICATION_FAILED", NEXT, "ACTION_REQUIRED"),
                    (f"audit:{CFG_FY}:all:lan=AUDIT_INCOMPLETE", CFG_FY, "WARNING")]
    assert NEXT in rig.alerts[0].message and "FY26-27" not in rig.alerts[0].message


def test_a_clean_audit_of_two_folders_sends_nothing(rig):
    run(rig)
    assert rig.alerts == []


def test_slot_not_obtained_records_every_leg_and_fy(rig):
    rig.slot_error = TimeoutError("slot timed out")
    with pytest.raises(ExceptionGroup):
        run(rig)
    assert [(m, fy, s) for _a, m, fy, s, _d in rows(rig)] == [
        ("cloud", NEXT, "AUDIT_INCOMPLETE"), ("cloud", CFG_FY, "AUDIT_INCOMPLETE"),
        ("lan", NEXT, "AUDIT_INCOMPLETE"), ("lan", CFG_FY, "AUDIT_INCOMPLETE")]
    assert sorted(a.fy for a in rig.alerts) == [CFG_FY, NEXT]
    assert rig.events == []


# ── one budget, one NAS decision ──────────────────────────────────────────────

def _budget_for(monkeypatch, allowed_calls):
    state = {"n": 0}

    class FakeBudget:
        def __init__(self, leg, seconds, started):
            pass

        def cap(self, timeout_s, reserve_s=0.0):
            state["n"] += 1
            if state["n"] > allowed_calls:
                raise BudgetExceeded("audit: run budget of 12 h used up (reserve 0 min kept for the remaining steps)")
            return float(timeout_s)

    monkeypatch.setattr(flow, "RunBudget", FakeBudget)


def test_folders_share_one_budget_the_one_with_none_left_is_incomplete_for_its_fy(rig, monkeypatch):
    _budget_for(monkeypatch, 1)       # only the very first folder gets time
    with pytest.raises(ExceptionGroup):
        run(rig, mode="cloud")
    got = rows(rig)
    assert [(fy, s) for _a, _m, fy, s, _d in got] == [(NEXT, "VERIFIED"), (CFG_FY, "AUDIT_INCOMPLETE")]
    assert "budget" in got[1][4] and "could not run" in got[1][4]
    assert rig.events == ["slot+", f"cloud:{NEXT}", "slot-"]


def test_no_shutdown_when_the_last_lan_folder_was_skipped_for_budget(rig, monkeypatch):
    _budget_for(monkeypatch, 3)       # cloud x2, first LAN folder; the second LAN folder has none
    with pytest.raises(ExceptionGroup):
        run(rig)
    assert rig.shutdowns == []
    assert "shutdown" not in rig.events


def test_no_shutdown_when_the_first_lan_folder_was_skipped_for_budget(rig, monkeypatch):
    _budget_for(monkeypatch, 2)       # cloud x2 only
    with pytest.raises(ExceptionGroup):
        run(rig)
    assert rig.shutdowns == []


def test_no_shutdown_when_the_nas_did_not_wake_for_a_folder(rig):
    rig.wol_error = RuntimeError("did not become usable")
    with pytest.raises(ExceptionGroup):
        run(rig, mode="lan")
    assert rig.shutdowns == []
    assert [s for _a, _m, _fy, s, _d in rows(rig)] == ["AUDIT_INCOMPLETE", "AUDIT_INCOMPLETE"]


def test_a_failing_lan_audit_still_shuts_the_nas_down_once_as_today(rig):
    rig.results[("lan", CFG_FY)] = FAILED
    rig.results[("lan", NEXT)] = RuntimeError("boom")
    with pytest.raises(ExceptionGroup):
        run(rig, mode="lan")
    assert len(rig.shutdowns) == 1


def test_shutdown_switched_off_means_no_shutdown(rig):
    rig.cfg.lan.shutdown_after_backup = False
    run(rig, mode="lan")
    assert rig.shutdowns == []


def test_cloud_mode_never_touches_the_nas(rig):
    run(rig, mode="cloud")
    assert "wol" not in rig.events and rig.shutdowns == []


def test_the_flow_signature_keeps_every_old_parameter_and_fy_is_last_and_optional():
    import inspect
    params = list(inspect.signature(flow.integrity_audit_flow.fn).parameters.values())
    assert [p.name for p in params] == ["config_path", "mode", "fy"]
    assert params[2].default is None
