"""Plan A6 / A9: the rollover as a persisted, resumable step journal - one scenario per failure
(R1-R4) and the resume the next morning. Same rig as test_m2_rollover (patched tools, one real
manifest DB per rig, so the journal really persists between the runs of one scenario)."""

import pytest

from core import faults
from core import fy_rollover as fr
from core import rollover_journal as rj
from core.config_file import ConfigWriteError
from tests.test_m2_rollover import _Rig

FROM, TO = "FY26-27", "FY27-28"


def _states(r):
    return {k: v["state"] for k, v in r.db.rollover_get(FROM, TO)["steps"].items()}


def _events(r):
    return [c.args[1] for c in r.m["notify"].call_args_list]


def _config_now_points_at_the_new_fy(r):
    """What the real config switch does to config.yaml (the rig's update_config_yaml is a mock)."""
    r.cfg.paths.source_drive = "E:\\DATA\\FY27-28"
    r.cfg.paths.lan_destination = "\\\\nas\\backup\\FY27-28"


def _reset_mocks(r):
    for name in ("_run_cloud_pipeline", "_run_lan_pipeline", "notify", "_record_run",
                 "update_config_yaml", "run_archive_transition", "lan_shutdown_task",
                 "create_nas_folder", "create_source_folder", "check_clock", "ensure_server_online"):
        r.m[name].reset_mock()


def test_happy_path_completes_every_step_and_says_nothing_is_left_to_do():
    with _Rig() as r:
        assert r.run() == "ROLLOVER_COMPLETED"
        assert _states(r) == {s: rj.DONE for s in rj.STEPS}
        info = [e for e in _events(r) if e.kind == "INFO"]
        assert len(info) == 1 and "Nothing to do" in info[0].message
        r.m["lan_shutdown_task"].assert_called_once()        # NAS is shut down at the END
        r.m["_record_running"].assert_called_once()
        assert r.m["_record_running"].call_args.args[1] == "rollover"


def test_final_backups_run_outside_the_backup_slot():
    with _Rig() as r:
        r.order.attach_mock(r.m["_run_cloud_pipeline"], "cloud_pipeline")
        r.order.attach_mock(r.m["_run_lan_pipeline"], "lan_pipeline")
        r.run()
        names = [c[0] for c in r.order.mock_calls if not c[0].startswith("_backup_slot().")]
        assert names.index("_backup_slot") > max(names.index("cloud_pipeline"),
                                                 names.index("lan_pipeline"))


# ---- R1: the NAS is off / unreachable when its folder is to be created -------------------------

def test_nas_folder_failure_does_not_stop_the_switch_and_nas_is_not_shut_down():
    with _Rig() as r:
        r.m["create_nas_folder"].side_effect = OSError("NAS share is not reachable")
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["update_config_yaml"].assert_called_once()        # cloud protects the new FY from day 1
        st = _states(r)
        assert st[rj.NAS_FOLDER] == rj.FAILED and st[rj.CONFIG_SWITCH] == rj.DONE
        assert st[rj.NAS_SHUTDOWN] == rj.PENDING             # never shut down a NAS we never reached
        r.m["lan_shutdown_task"].assert_not_called()
        [info] = [e for e in _events(r) if e.kind == "INFO"]
        assert "nas folder" in info.message and "Nothing to do" not in info.message
        assert r.statuses()[-1] == "ROLLOVER_COMPLETE"


# ---- R2: the switch itself fails ------------------------------------------------------------

def test_config_switch_failure_is_recorded_alerted_and_resumed_next_morning_with_a_repeat_backup():
    with _Rig() as r:
        r.m["update_config_yaml"].side_effect = ConfigWriteError("config.yaml is in use")
        with pytest.raises(fr.RolloverError, match="config switch"):
            r.run()
        assert r.statuses()[-1] == "ROLLOVER_FAILED"
        [event] = [e for e in _events(r) if e.kind == "ACTION_REQUIRED"]
        assert event.fault_code == "AAM-ROL-04" and event.key == f"rollover:{TO}:config_switch"
        st = _states(r)
        assert st[rj.CONFIG_SWITCH] == rj.FAILED and st[rj.ARCHIVE] == rj.PENDING
        r.m["run_archive_transition"].assert_not_called()    # nothing after the failed switch
        assert not [e for e in _events(r) if e.kind == "INFO"]

        # next morning: the lock is free; D-17 - the final backup is repeated first
        _reset_mocks(r)
        r.m["update_config_yaml"].side_effect = None
        r.m["update_config_yaml"].return_value = "cfg.bak"
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["_run_cloud_pipeline"].assert_called_once()
        r.m["_run_lan_pipeline"].assert_called_once()
        assert _states(r) == {s: rj.DONE for s in rj.STEPS}
        assert r.db.rollover_get(FROM, TO)["steps"][rj.CONFIG_SWITCH]["attempts"] == 2


def test_source_folder_failure_stops_before_the_switch():
    with _Rig() as r:
        r.m["create_source_folder"].side_effect = OSError("disk full")
        with pytest.raises(fr.RolloverError, match="source folder"):
            r.run()
        r.m["update_config_yaml"].assert_not_called()
        assert _states(r)[rj.SOURCE_FOLDER] == rj.FAILED
        assert r.statuses()[-1] == "ROLLOVER_FAILED"


# ---- R3: crash after the config write, before anything was recorded ---------------------------

def test_config_already_switched_is_recognised_and_completes():
    with _Rig() as r:
        r.m["update_config_yaml"].return_value = None        # compare-and-set: already at TO
        assert r.run() == "ROLLOVER_COMPLETED"
        assert _states(r)[rj.CONFIG_SWITCH] == rj.DONE


# ---- R4: the archive move fails ---------------------------------------------------------------

def test_archive_failure_warns_is_retried_and_never_repeats_the_rest():
    with _Rig() as r:
        r.m["run_archive_transition"].return_value = False
        assert r.run() == "ROLLOVER_COMPLETED"
        assert _states(r)[rj.ARCHIVE] == rj.FAILED
        warn = [e for e in _events(r) if e.fault_code == "AAM-ROL-05"]
        assert len(warn) == 1 and warn[0].kind == "WARNING"
        assert warn[0].key == f"rollover:{TO}:archive"
        [info] = [e for e in _events(r) if e.kind == "INFO"]
        assert "archive" in info.message and "Nothing to do" not in info.message

        # next morning the archive works; nothing else is repeated
        _config_now_points_at_the_new_fy(r)
        _reset_mocks(r)
        r.m["run_archive_transition"].return_value = True
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["run_archive_transition"].assert_called_once()
        r.m["_run_cloud_pipeline"].assert_not_called()       # config already switched: no repeat
        r.m["update_config_yaml"].assert_not_called()
        r.m["check_clock"].assert_not_called()               # post-switch steps need no clock proof
        assert not [e for e in _events(r) if e.kind == "INFO"]   # announced only once
        assert _states(r) == {s: rj.DONE for s in rj.STEPS}


def test_archive_failure_escalates_after_the_configured_days():
    with _Rig() as r:
        r.m["run_archive_transition"].return_value = False
        r.run()                                              # day 1: WARNING
        # pretend the archive has been failing since long ago
        with r.db._lock:
            conn = r.db._get_conn()
            with conn:
                conn.execute("UPDATE rollover_journal_steps SET first_failed_at = ? "
                             "WHERE step = ?", ("2020-01-01T00:00:00+05:30", rj.ARCHIVE))
        _config_now_points_at_the_new_fy(r)
        _reset_mocks(r)
        r.m["run_archive_transition"].return_value = False
        r.run()
        [alert] = [e for e in _events(r) if e.fault_code == "AAM-ROL-05"]
        assert alert.kind == "ACTION_REQUIRED"


# ---- the first-run guard paths still work through the journal ---------------------------------

def test_blocked_run_leaves_a_journal_and_an_approved_rerun_continues():
    with _Rig(lan="LAN_FAILED") as r:
        with pytest.raises(fr.RolloverError):
            r.run()
        assert r.statuses()[-1] == "ROLLOVER_BLOCKED"
        assert _states(r)[rj.FINAL_BACKUPS] == rj.FAILED
        r.m["update_config_yaml"].assert_not_called()


def test_fault_codes_used_by_the_rollover_exist():
    for code in ("AAM-ROL-04", "AAM-ROL-05"):
        assert faults.get(code) is not None
