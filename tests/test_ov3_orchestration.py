"""WP3 - orchestration of the FY overlap (plan 5.3; R-3, R-4, R-7, R-16, R-18): the slot is held once per leg
around every folder, new FY first, one folder's failure never stops the other, both folders share the leg
budget (the second one is MISSED when none is left), and the NAS is powered off ONCE after the last folder."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pendulum
import pytest

import flow
from core.manifest import ManifestDB
from tests.test_ov2_fy_overlap import CFG_FY, NEXT, _cfg

JAN = pendulum.datetime(2027, 1, 5, 2, 1, tz="Asia/Kolkata")


class Rig:
    def __init__(self):
        self.events = []           # ordered: slot enter/exit, pipeline calls
        self.calls = []            # (leg, fy, kwargs)
        self.shutdowns = []
        self.recorded = []         # _record_run(view, run_id, leg, started, status, ...)
        self.alerts = []
        self.fail = {}             # (leg, fy) -> Exception
        self.votes = {}            # (leg, fy) -> bool | None (None = never votes)
        self.slot_error = None
        self.slot_open = False


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig()
    cfg = _cfg(tmp_path)
    cfg.paths.database_path = str(tmp_path / "m.db")
    r.cfg = cfg
    db = ManifestDB(cfg.paths.database_path)
    db.record_activation(NEXT, activated_at=JAN.isoformat(), source_path=f"D:\\{NEXT}",
                         created_by_app=True, clock_state="OK")
    db.close()

    monkeypatch.setattr(flow, "load_config", lambda p: cfg)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow, "configure_prefect_bridge", lambda *a, **k: None)
    monkeypatch.setattr(flow, "cleanup_orphaned_robocopy_logs", lambda **k: None)
    monkeypatch.setattr(flow, "_activate_overlap", lambda *a, **k: None)
    monkeypatch.setattr(flow, "_scheduled_time", lambda: JAN)
    monkeypatch.setattr(flow, "_scheduled_lateness_seconds", lambda: 0.0)
    monkeypatch.setattr(flow, "_flow_run_prefix", lambda: "flow1")

    @contextmanager
    def slot(config):
        if r.slot_error is not None:
            raise r.slot_error
        r.events.append("slot+")
        r.slot_open = True
        try:
            yield
        finally:
            r.slot_open = False
            r.events.append("slot-")

    monkeypatch.setattr(flow, "_backup_slot", slot)

    def make(leg):
        def pipeline(config, run_id, started_at, monotonic_start, **kw):
            fy = flow._effective_fy_prefix(config)
            assert r.slot_open, "a pipeline ran without the slot held by the leg loop"
            assert kw.get("slot_held") is True
            r.events.append(f"{leg}:{fy}")
            r.calls.append((leg, fy, run_id, config, kw))
            vote = r.votes.get((leg, fy), True)
            if leg == "lan" and vote is not None:
                kw["shutdown_votes"].append((vote, f"art-{fy}"))
            if (leg, fy) in r.fail:
                raise r.fail[(leg, fy)]
        return pipeline

    monkeypatch.setattr(flow, "_run_cloud_pipeline", make("cloud"))
    monkeypatch.setattr(flow, "_run_lan_pipeline", make("lan"))
    monkeypatch.setattr(flow, "lan_shutdown_task",
                        lambda config, artefacts_dir=None: r.shutdowns.append(artefacts_dir) or {"outcome": "VERIFIED"})
    monkeypatch.setattr(flow, "_record_run", lambda config, run_id, leg, started, status, *a, **k:
                        r.recorded.append((flow._effective_fy_prefix(config), run_id, leg, status)))
    monkeypatch.setattr(flow, "_send_run_alert", lambda config, leg, status, text, *a, **k:
                        r.alerts.append((flow._effective_fy_prefix(config), leg, status)))
    return r


def run(mode="all", **kw):
    return flow.backup.fn("config.yaml", mode, **kw)


# ── OV-3.1 / OV-3.2 ───────────────────────────────────────────────────────────

def test_slot_is_held_once_per_leg_around_both_folders_new_fy_first(rig):
    run("all")
    assert rig.events == ["slot+", f"cloud:{NEXT}", f"cloud:{CFG_FY}", "slot-",
                          "slot+", f"lan:{NEXT}", f"lan:{CFG_FY}", "slot-"]


def test_folder_views_run_ids_and_flags(rig):
    run("cloud")
    (_, fy1, rid1, view1, kw1), (_, fy2, rid2, view2, kw2) = rig.calls
    assert (fy1, fy2) == (NEXT, CFG_FY)
    assert rid1 == f"flow1-cloud-{NEXT}" and rid2 == f"flow1-cloud-{CFG_FY}"
    assert view1.paths.source_drive.endswith(NEXT) and view2.paths.source_drive.endswith(CFG_FY)
    assert (kw1["shared_budget"], kw2["shared_budget"]) == (False, True)
    assert kw1["budget_monotonic_start"] == kw2["budget_monotonic_start"]     # one shared leg budget
    assert kw1["late_seconds"] == kw2["late_seconds"]


def test_a_normal_day_has_one_folder_and_one_slot_hold(rig, monkeypatch):
    monkeypatch.setattr(flow, "_scheduled_time", lambda: pendulum.datetime(2026, 11, 20, 2, 1, tz="Asia/Kolkata"))
    run("cloud")
    assert rig.events == ["slot+", f"cloud:{CFG_FY}", "slot-"]
    assert rig.calls[0][2] == f"flow1-cloud-{CFG_FY}"


def test_one_folders_failure_never_stops_the_other_and_the_flow_still_raises(rig):
    rig.fail[("cloud", NEXT)] = RuntimeError("early folder broke")
    with pytest.raises(ExceptionGroup) as ei:
        run("cloud")
    assert [c[1] for c in rig.calls] == [NEXT, CFG_FY]                    # the configured FY still ran
    assert [str(e) for e in ei.value.exceptions] == ["early folder broke"]


def test_cloud_failure_does_not_stop_the_lan_leg(rig):
    rig.fail[("cloud", CFG_FY)] = RuntimeError("cloud broke")
    with pytest.raises(ExceptionGroup):
        run("all")
    assert [c[:2] for c in rig.calls] == [("cloud", NEXT), ("cloud", CFG_FY), ("lan", NEXT), ("lan", CFG_FY)]


def test_manual_fy_must_be_active_and_narrows_the_run(rig):
    run("cloud", fy=NEXT)
    assert [c[1] for c in rig.calls] == [NEXT]
    with pytest.raises(ValueError, match="not being backed up"):
        run("cloud", fy="FY25-26")
    assert [c[1] for c in rig.calls] == [NEXT]


def test_disabled_legs_do_nothing_and_do_not_work_out_the_folders(rig):
    rig.cfg.cloud.enabled = False
    rig.cfg.lan.enabled = False
    run("all")
    assert rig.events == []


# ── OV-3.3 shared budget + MISSED ─────────────────────────────────────────────

def test_second_folder_with_no_budget_left_is_missed_for_its_fy(monkeypatch, tmp_path):
    recorded, alerts = [], []
    monkeypatch.setattr(flow, "_record_run", lambda config, run_id, leg, started, status, *a, **k:
                        recorded.append((flow._effective_fy_prefix(config), status)))
    monkeypatch.setattr(flow, "notify", lambda config, event, db=None: alerts.append(event))
    cfg = _cfg(tmp_path)
    budget = SimpleNamespace(remaining=lambda: 10.0, describe=lambda: "6 h")
    # first folder / on time: no check (nothing to compare against)
    flow._missed_if_no_budget(cfg, "cloud", "r1", "t", budget, 600, 0.0, None, shared_budget=False)
    assert recorded == []
    # second folder of the same leg run: the budget is used up -> MISSED for ITS fy
    with pytest.raises(flow.PartialRun, match="CLOUD_MISSED"):
        flow._missed_if_no_budget(cfg, "cloud", "r2", "t", budget, 600, 0.0, None, shared_budget=True)
    assert recorded == [(CFG_FY, "CLOUD_MISSED")]
    assert alerts[0].fy == CFG_FY and alerts[0].fault_code == "AAM-SYS-07"
    assert "used up by the folder before it" in alerts[0].message


def test_second_folder_with_budget_left_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(flow, "_record_run", lambda *a, **k: pytest.fail("must not be recorded"))
    budget = SimpleNamespace(remaining=lambda: 5000.0, describe=lambda: "6 h")
    flow._missed_if_no_budget(_cfg(tmp_path), "cloud", "r2", "t", budget, 600, 0.0, None, shared_budget=True)


# ── OV-3.4 one NAS power cycle ────────────────────────────────────────────────

def test_one_shutdown_after_both_folders_complete(rig):
    run("lan")
    assert rig.shutdowns == [f"art-{CFG_FY}"]


def test_no_shutdown_when_a_folder_says_stay_on(rig):
    rig.votes[("lan", NEXT)] = False
    run("lan")
    assert rig.shutdowns == []


def test_no_shutdown_when_the_second_folder_never_ran_or_voted(rig):
    rig.votes[("lan", CFG_FY)] = None          # MISSED / cut off before it could vote
    run("lan")
    assert rig.shutdowns == []


def test_crash_before_the_second_folder_means_no_shutdown_and_the_flow_raises(rig):
    rig.fail[("lan", NEXT)] = RuntimeError("boom")
    rig.votes[("lan", NEXT)] = False            # a crashed folder: today's failure rule says stay on
    with pytest.raises(ExceptionGroup):
        run("lan")
    assert rig.shutdowns == []


def test_failure_rule_shutdown_option_still_counts_once(rig):
    # every folder failed but both say "shutdown_on_all_retries_exhausted": still ONE shutdown
    rig.fail[("lan", NEXT)] = RuntimeError("a")
    rig.fail[("lan", CFG_FY)] = RuntimeError("b")
    with pytest.raises(ExceptionGroup):
        run("lan")
    assert len(rig.shutdowns) == 1


def test_cloud_leg_never_touches_the_nas(rig):
    run("cloud")
    assert rig.shutdowns == []


def test_slot_timeout_records_each_folder_and_runs_nothing(rig):
    rig.slot_error = TimeoutError("waited 3600 s")
    with pytest.raises(ExceptionGroup):
        run("lan")
    assert rig.calls == [] and rig.shutdowns == []
    assert sorted(r[0] for r in rig.recorded) == [CFG_FY, NEXT]
    assert {r[3] for r in rig.recorded} == {"LAN_SLOT_TIMEOUT"}
    assert sorted(a[0] for a in rig.alerts) == [CFG_FY, NEXT]


def test_cancel_in_the_first_folder_releases_the_slot_and_does_not_shut_down(rig):
    rig.fail[("lan", NEXT)] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run("lan")
    assert rig.events[-1] == "slot-" and rig.shutdowns == []


# ── helpers ───────────────────────────────────────────────────────────────────

def test_merge_run_metrics_keeps_existing_keys(tmp_path):
    db = ManifestDB(tmp_path / "m.db")
    db.insert_run({"run_id": "r", "mode": "lan", "fy": CFG_FY, "started_at": "t", "status": "LAN_COMPLETE",
                   "extended_metrics": '{"a": 1}'})
    assert db.merge_run_metrics("r", {"nas_shutdown": {"outcome": "VERIFIED"}}) is True
    import json
    assert json.loads(db.last_run("lan", fy=CFG_FY)["extended_metrics"]) == {
        "a": 1, "nas_shutdown": {"outcome": "VERIFIED"}}
    assert db.merge_run_metrics("missing", {"x": 1}) is False
    db.close()


def test_run_id_and_artifact_keys_carry_the_fy(monkeypatch):
    monkeypatch.setattr(flow, "_flow_run_prefix", lambda: "abc")
    assert flow._stable_run_id("cloud") == "abc-cloud"
    assert flow._stable_run_id("cloud", "FY27-28") == "abc-cloud-FY27-28"
    assert flow._artifact_key("cloud-backup-summary", "FY27-28") == "cloud-backup-summary-fy27-28"
    assert flow._artifact_key("lan-backup-summary", None) == "lan-backup-summary"


# ── Opus review of WP3 ────────────────────────────────────────────────────────

def test_an_approval_run_only_touches_the_configured_fy(rig):
    """I-3: one approval belongs to one blocked folder; it must not be applied to the early FY."""
    run("lan", approval_id="a1")
    assert [c[1] for c in rig.calls] == [CFG_FY]
    assert rig.calls[0][4]["approval_id"] == "a1"


def test_the_first_folders_running_row_is_written_before_the_slot_wait(rig, monkeypatch):
    """I-2: a run that dies while waiting for the slot still leaves a RUNNING row to sweep."""
    seen = []
    monkeypatch.setattr(flow, "_record_running", lambda config, leg, run_id, started_at:
                        seen.append((flow._effective_fy_prefix(config), leg, list(rig.events))))
    run("cloud")
    assert seen == [(NEXT, "cloud", [])]          # before "slot+"


def test_a_cancel_makes_no_nas_decision(rig):
    """N-6: nothing is powered off while the run is being terminated."""
    rig.fail[("lan", CFG_FY)] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run("lan")
    assert rig.shutdowns == []


def test_failure_alert_text_is_truthful_when_the_loop_decides(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.lan.shutdown_on_all_retries_exhausted = True
    assert "only if every folder's backup allows it" in flow._nas_alert_note(False, True, cfg)
    assert "NOT shut down" not in flow._nas_alert_note(False, True, cfg)
    cfg.lan.shutdown_on_all_retries_exhausted = False
    assert "NOT shut down" in flow._nas_alert_note(False, True, cfg)
    # the rollover's final backup (no loop): unchanged text
    assert "scheduled for shutdown" in flow._nas_alert_note(True, False, cfg)
    assert "NOT shut down" in flow._nas_alert_note(False, False, cfg)


def test_active_set_failure_falls_back_and_warns(rig, monkeypatch):
    sent = []
    monkeypatch.setattr(flow, "notify", lambda config, event, db=None: sent.append(event.key))
    monkeypatch.setattr(flow.fy_overlap, "active_folders", MagicMock(side_effect=RuntimeError("db locked")))
    run("cloud")
    assert [c[1] for c in rig.calls] == [CFG_FY]
    assert sent == ["overlap:active_set"]
