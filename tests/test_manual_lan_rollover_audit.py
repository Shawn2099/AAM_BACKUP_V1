"""Manual-only LAN at the 1 April rollover and the Sunday audit (owner decision 2, 2026-11-23).

The NAS is still woken and used; an unreachable NAS is an EXPECTED, NON-BLOCKING outcome that is recorded
truthfully (run row, rollover metrics, dashboard / report line) with at most one warning mail per rollover.
Real failures (a reachable NAS that fails, guard blocks, the cloud leg) block exactly as before."""
import json
from unittest.mock import MagicMock, patch

import pytest

import flow
from core import faults, fy_overlap, manual_leg
from core import fy_rollover as fr
from core.wol import WolTimeout
from tests.test_a4_final_lan_no_shutdown import _make_lan_config
from tests.test_m2_rollover import _Rig
from tests.test_ov8_audit_per_fy import CFG_FY, rig, rows, run  # noqa: F401  (rig fixture)


def _nas_off(config, run_id, started_at, monotonic_start=None, approval_id=None, **kwargs):
    raise faults.coded(WolTimeout("Backup server unreachable after WoL"), "AAM-NAS-02")


def _manual_lan(r):
    r.cfg.lan.scheduled = False
    r.cfg.lan.manual_only = True
    r.cfg.cloud.scheduled = True
    return r


def _warnings(r):
    return [c.args[1] for c in r.m["notify"].call_args_list
            if c.args[1].kind == "WARNING" and "not_fully_copied" in c.args[1].key]


def test_manual_only_lan_nas_off_does_not_block_the_switch_and_warns_once():
    with _Rig() as r:
        _manual_lan(r)
        r.m["_run_lan_pipeline"].side_effect = _nas_off
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["update_config_yaml"].assert_called_once()          # the switch went ahead
        r.m["_open_block_request"].assert_not_called()
        assert r.m["_run_lan_pipeline"].call_args.kwargs["nas_offline_expected"] is True
        [w] = _warnings(r)
        assert w.key == "rollover:FY26-27:not_fully_copied" and w.fy == "FY26-27"
        assert "FY26-27 is not yet fully copied to LAN" in w.message
        assert "start a LAN backup from the dashboard" in w.message       # the truth, not 'it will be brought up to date'
        assert "up to date" not in w.message
        assert r.db.get_meta("lan_final_missing") is None                 # derived from run_history now
        assert fy_overlap.year_closed(r.db, "FY26-27") is False            # NOT closed: it stays tracked
        # the rollover run row says it
        recorded = [c for c in r.m["_record_run"].call_args_list if c.args[4] == "ROLLOVER_COMPLETE"]
        metrics = json.loads(recorded[-1].kwargs["extended_metrics"])
        assert metrics["lan_final_not_reached"] == "FY26-27"
        assert metrics["old_year_not_fully_copied"] == {"fy": "FY26-27", "legs": ["lan"]}


def test_the_year_closes_only_after_a_real_lan_backup_of_it_started_after_the_year_ended():
    with _Rig() as r:
        _manual_lan(r)
        r.m["_run_lan_pipeline"].side_effect = _nas_off
        r.run()
        r.cfg.paths.source_drive = "E:\\DATA\\FY27-28"                  # what the switch did to config.yaml
        r.cfg.paths.lan_destination = "\\\\nas\\backup\\FY27-28"
        for run_id, mode, status, started in (
                ("c1", "cloud", "CLOUD_COMPLETE", "2027-04-01T06:00:00+05:30"),
                ("l0", "lan", "LAN_COMPLETE", "2027-03-31T21:00:00+05:30"),      # before the year ended
                ("l1", "lan", "LAN_FAILED", "2027-04-05T10:00:00+05:30"),
                ("l2", "lan", "LAN_PARTIAL", "2027-04-06T10:00:00+05:30")):
            r.db.insert_run({"fy": "FY26-27", "run_id": run_id, "mode": mode, "status": status,
                             "started_at": started, "ended_at": started})
        flow._close_finished_old_fys(r.cfg, r.db)
        assert fy_overlap.year_closed(r.db, "FY26-27") is False
        assert [o["missing"] for o in fy_overlap.unfinished_old_years(r.cfg, r.db)] == [["lan"]]
        r.db.insert_run({"fy": "FY26-27", "run_id": "l3", "mode": "lan", "status": "LAN_COMPLETE",
                         "started_at": "2027-04-07T10:00:00+05:30", "ended_at": "2027-04-07T11:00:00+05:30"})
        flow._close_finished_old_fys(r.cfg, r.db)
        assert fy_overlap.year_closed(r.db, "FY26-27") is True
        assert fy_overlap.unfinished_old_years(r.cfg, r.db) == []


def test_a_normal_rollover_closes_the_year_at_the_switch_as_before():
    with _Rig() as r:
        assert r.run() == "ROLLOVER_COMPLETED"
        assert fy_overlap.year_closed(r.db, "FY26-27") is True
        assert _warnings(r) == []


def test_an_approved_rollover_over_a_failed_final_backup_keeps_the_year_tracked():
    with _Rig(lan="LAN_FAILED", approved=True) as r:
        assert r.run(approval_id="a1") == "ROLLOVER_COMPLETED"
        assert fy_overlap.year_closed(r.db, "FY26-27") is False
        [w] = _warnings(r)
        assert "FY26-27 is not yet fully copied to LAN" in w.message


def test_a_manual_only_cloud_leg_that_did_not_copy_the_year_keeps_it_tracked():
    with _Rig(cloud="CLOUD_FAILED", approved=True) as r:
        r.cfg.cloud.scheduled = False
        assert r.run(approval_id="a1") == "ROLLOVER_COMPLETED"
        assert fy_overlap.year_closed(r.db, "FY26-27") is False
        [w] = _warnings(r)
        assert "FY26-27 is not yet fully copied to Cloud" in w.message
        assert "start a Cloud backup from the dashboard" in w.message


def test_nightly_lan_with_the_nas_off_still_blocks_the_switch_as_today():
    with _Rig() as r:
        r.m["_run_lan_pipeline"].side_effect = _nas_off
        with pytest.raises(fr.RolloverError, match="lan"):
            r.run()
        r.m["update_config_yaml"].assert_not_called()
        assert _warnings(r) == []
        assert "nas_offline_expected" not in r.m["_run_lan_pipeline"].call_args.kwargs


def test_manual_only_lan_real_failure_with_a_reachable_nas_still_blocks():
    with _Rig(lan="LAN_FAILED") as r:
        _manual_lan(r)
        with pytest.raises(fr.RolloverError, match="lan"):
            r.run()
        r.m["update_config_yaml"].assert_not_called()
        assert _warnings(r) == []


def test_a_manual_only_cloud_leg_failing_at_the_rollover_still_blocks():
    with _Rig(cloud="CLOUD_FAILED") as r:
        r.cfg.cloud.scheduled = False
        r.cfg.cloud.manual_only = True
        with pytest.raises(fr.RolloverError, match="cloud"):
            r.run()
        r.m["update_config_yaml"].assert_not_called()


def test_a_manual_only_cloud_leg_final_backup_runs_and_the_switch_completes():
    with _Rig() as r:
        r.cfg.cloud.scheduled = False
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["_run_cloud_pipeline"].assert_called_once()


# ── the pipeline itself ────────────────────────────────────────────────────────────────────────

def _pipeline(expected, wol_raises):
    cfg = _make_lan_config()
    with patch("flow.notify"), patch("flow._send_run_alert") as alert, \
            patch("flow.lan_shutdown_task"), patch("flow._record_run") as rec, \
            patch("flow.wol_check_task", side_effect=wol_raises), patch("flow.health_check_task"):
        with pytest.raises(Exception):
            flow._run_lan_pipeline(cfg, "run-x", "2026-04-01T06:00:00", allow_shutdown=False,
                                   nas_offline_expected=expected)
    return alert, rec


def test_pipeline_sends_no_failure_mail_for_an_expected_offline_nas_but_records_it():
    alert, rec = _pipeline(True, faults.coded(WolTimeout("x"), "AAM-NAS-02"))
    alert.assert_not_called()
    metrics = json.loads(rec.call_args.kwargs["extended_metrics"])
    assert metrics["nas_offline_expected"] is True and rec.call_args.args[4] == "LAN_FAILED"


def test_pipeline_still_alerts_when_the_nas_off_is_not_expected():
    alert, _ = _pipeline(False, faults.coded(WolTimeout("x"), "AAM-NAS-02"))
    alert.assert_called_once()


def test_pipeline_still_alerts_for_other_failures_even_when_offline_is_expected():
    alert, _ = _pipeline(True, RuntimeError("robocopy exploded"))
    alert.assert_called_once()


# ── the audit ───────────────────────────────────────────────────────────────────────────────────

def test_audit_nas_off_for_a_manual_only_lan_is_recorded_without_alert_or_failure(rig):  # noqa: F811
    rig.cfg.lan.scheduled = False
    rig.wol_error = faults.coded(WolTimeout("not woken"), "AAM-NAS-02")
    run(rig, mode="lan")                                   # does NOT raise
    assert rig.alerts == []
    got = rows(rig)
    assert [s for _a, _m, _fy, s, _d in got] == ["AUDIT_INCOMPLETE", "AUDIT_INCOMPLETE"]
    assert all("manual only" in d for *_x, d in got)


def test_audit_nas_off_for_a_nightly_lan_still_alerts_and_fails(rig):  # noqa: F811
    rig.wol_error = faults.coded(WolTimeout("not woken"), "AAM-NAS-02")
    with pytest.raises(ExceptionGroup):
        run(rig, mode="lan")
    assert rig.alerts


def test_audit_other_failure_for_a_manual_only_lan_still_alerts_and_fails(rig):  # noqa: F811
    rig.cfg.lan.scheduled = False
    rig.wol_error = RuntimeError("something else broke")
    with pytest.raises(ExceptionGroup):
        run(rig, mode="lan")


def test_audit_cloud_leg_is_unchanged_for_a_manual_only_cloud(rig):  # noqa: F811
    rig.cfg.cloud.scheduled = False
    rig.results[("cloud", CFG_FY)] = RuntimeError("cloud audit boom")
    with pytest.raises(ExceptionGroup):
        run(rig, mode="cloud")
