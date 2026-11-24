r"""Tests for tools.livebox.safety (plan 1.8, rows S1-S15; work package L3b). Written before the module exists.

ASSUMED API (all pure checks on injected inputs; nothing reads real production state, the network or the system clock):

    class SafetyRefusal(Exception): .rule -> "S1".."S15"   # abort the lane
    class SafetyPause(Exception):   .rule -> "S10" / "S11"  # pause, not abort

    check_box_head(head, expected_commit, porcelain)                 S1  (porcelain = text of `git status --porcelain`)
    check_sandbox_paths(paths: dict[str, str], lane_root)            S2  (every value must be under lane_root)
    check_not_production_paths(paths: dict[str, str], production)    S3  (production = iterable of forbidden roots)
    check_fy_leaves(leaves, production_leaves, century=False)        S4
    check_config_binding(config_path, lane_config, flow_file, box_code_root)   S5
    check_prefect_api_url(url, lane_url)                             S6
    check_notifications(smtp_host, smtp_auth, recipients)            S7
    check_rclone_config(config_path, lane_rclone_conf, remote_type, endpoint, smoke=False)   S8
    check_shutdown_raisers(shutdown_fn, wol_fn, lan_shutdown_task)   S9  (raisers carry attr `livebox_raiser = True`,
                                                                       the spy carries `livebox_spy = True`)
    check_production_lock(lock_path)                                 S10 (SafetyPause if the file exists)
    check_production_schedule(now, crons, window_minutes=30)         S11 (now = aware datetime from an injected clock,
                                                                       crons = {job_name: cron string}; SafetyPause)
    check_resources(free_bytes_c, lane_root_bytes)                   S12 (GB = 1024**3)
    check_lane_process_count(count)                                  S13 (max 3)
    check_production_slot(slot_state)                                S14 (slot_state = {"active": int, "limit": int};
                                                                       taken when active >= limit)
    check_argv(argv, lane_root)                                      S15 (list of str; any absolute path outside the lane
                                                                       root is refused)

Each check returns None when fine and raises SafetyRefusal (or SafetyPause) with the right `.rule` otherwise.
"""
from __future__ import annotations

import datetime as dt
import importlib

import pytest

GB = 1024**3
ROOT = r"C:\AAMLivebox\20261201_0900\laneA"
LANE_CFG = ROOT + r"\config.yaml"
BOX_CODE = r"C:\AAMBackup"
PROD_ROOTS = [r"C:\AAMBackup", r"C:\BackupAgent", "E:\\", r"\\nas-prod\backup", r"D:\prod_logs"]


def safety():
    return importlib.import_module("tools.livebox.safety")


def refused(rule, fn, *args, **kwargs):
    s = safety()
    with pytest.raises(s.SafetyRefusal) as ei:
        fn(*args, **kwargs)
    assert ei.value.rule == rule


def good_paths():
    return {
        "source": ROOT + r"\source",
        "nas": ROOT + r"\nas",
        "bucket": ROOT + r"\bucket",
        "runtime": ROOT + r"\runtime",
        "database": ROOT + r"\runtime\manifest.db",
        "log_dir": ROOT + r"\logs",
        "run_logs": ROOT + r"\runtime\run_logs",
        "lock": ROOT + r"\runtime\backup.lock",
        "prefect_home": ROOT + r"\prefect",
        "temp": ROOT + r"\tmp",
    }


# S1
def test_s1_head_mismatch_or_dirty_box_refused():
    s = safety()
    assert s.check_box_head("abc123", "abc123", "") is None
    refused("S1", s.check_box_head, "def456", "abc123", "")
    refused("S1", s.check_box_head, "abc123", "abc123", " M flow.py\n")


# S2
def test_s2_every_sandbox_path_must_be_under_lane_root(tmp_path):
    s = safety()
    assert s.check_sandbox_paths(good_paths(), ROOT) is None
    for key in good_paths():
        bad = good_paths()
        bad[key] = str(tmp_path / "elsewhere")
        refused("S2", s.check_sandbox_paths, bad, ROOT)
    sneaky = good_paths()
    sneaky["source"] = ROOT + r"\..\other\source"
    refused("S2", s.check_sandbox_paths, sneaky, ROOT)
    sibling = good_paths()
    sibling["nas"] = ROOT + "X" + r"\nas"  # prefix match must not count
    refused("S2", s.check_sandbox_paths, sibling, ROOT)


# S3
@pytest.mark.parametrize(
    "bad",
    [
        r"C:\AAMBackup",
        r"C:\AAMBackup\data\x",
        r"C:\BackupAgent\run_logs",
        r"E:\Clients",
        r"\\nas-prod\backup\FY25-26",
        r"D:\prod_logs\today",
    ],
)
def test_s3_production_paths_refused(bad):
    s = safety()
    ok = good_paths()
    assert s.check_not_production_paths(ok, PROD_ROOTS) is None
    ok["source"] = bad
    refused("S3", s.check_not_production_paths, ok, PROD_ROOTS)


# S4
def test_s4_fy_leaf_outside_sandbox_range_or_production_on_sandbox_fy():
    s = safety()
    assert s.check_fy_leaves(["FY90-91", "FY91-92", "FY92-93", "FY93-94"], ["FY26-27"]) is None
    assert s.check_fy_leaves(["FY99-00", "FY00-01"], ["FY26-27"], century=True) is None
    refused("S4", s.check_fy_leaves, ["FY25-26"], ["FY26-27"])
    refused("S4", s.check_fy_leaves, ["FY99-00"], ["FY26-27"])  # century leaf only for the century lane
    refused("S4", s.check_fy_leaves, ["FY94-95"], ["FY26-27"])
    refused("S4", s.check_fy_leaves, ["FY90-91"], ["FY91-92"])  # production configured on a sandbox FY


# S5
def test_s5_config_path_and_flow_file_binding():
    s = safety()
    flow_ok = BOX_CODE + r"\flow.py"
    assert s.check_config_binding(LANE_CFG, LANE_CFG, flow_ok, BOX_CODE) is None
    refused("S5", s.check_config_binding, BOX_CODE + r"\config.yaml", LANE_CFG, flow_ok, BOX_CODE)
    refused("S5", s.check_config_binding, LANE_CFG, LANE_CFG, r"C:\Users\x\dev\flow.py", BOX_CODE)
    refused("S5", s.check_config_binding, LANE_CFG, LANE_CFG, r"C:\AAMBackupEvil\flow.py", BOX_CODE)


# S6
def test_s6_prefect_api_url_must_be_lane_ephemeral_never_4200():
    s = safety()
    lane = "http://127.0.0.1:51234/api"
    assert s.check_prefect_api_url(lane, lane) is None
    refused("S6", s.check_prefect_api_url, "http://127.0.0.1:4200/api", lane)
    refused("S6", s.check_prefect_api_url, "http://localhost:4200/api", lane)
    refused("S6", s.check_prefect_api_url, "https://api.prefect.cloud/api", lane)
    refused("S6", s.check_prefect_api_url, None, lane)
    refused("S6", s.check_prefect_api_url, "http://127.0.0.1:51235/api", lane)


# S7
def test_s7_smtp_must_be_local_unauthenticated_invalid_recipients():
    s = safety()
    good = ["ops@lab.invalid", "owner@x.invalid"]
    assert s.check_notifications("127.0.0.1", False, good) is None
    refused("S7", s.check_notifications, "smtp.gmail.com", False, good)
    refused("S7", s.check_notifications, "127.0.0.1", True, good)
    refused("S7", s.check_notifications, "127.0.0.1", False, ["ops@lab.invalid", "real@firm.com"])
    refused("S7", s.check_notifications, "127.0.0.1", False, ["a@invalid.example.com"])


# S8
def test_s8_rclone_config_and_remote_endpoint(tmp_path):
    s = safety()
    lane_conf = ROOT + r"\rclone.conf"
    assert s.check_rclone_config(lane_conf, lane_conf, "local", None) is None
    assert s.check_rclone_config(lane_conf, lane_conf, "google cloud storage", "http://127.0.0.1:9") is None
    assert s.check_rclone_config(lane_conf, lane_conf, "google cloud storage", "https://aam-livebox.invalid") is None
    refused("S8", s.check_rclone_config, r"C:\BackupAgent\rclone.conf", lane_conf, "local", None)
    refused("S8", s.check_rclone_config, lane_conf, lane_conf, "google cloud storage", None)
    refused("S8", s.check_rclone_config, lane_conf, lane_conf, "google cloud storage", "https://storage.googleapis.com")
    # gated smoke may lift only the endpoint rule
    assert s.check_rclone_config(lane_conf, lane_conf, "google cloud storage", None, smoke=True) is None


# S9
def test_s9_shutdown_and_wol_must_be_raisers_and_task_the_spy():
    s = safety()

    def raiser():
        raise RuntimeError("blocked")

    raiser.livebox_raiser = True

    def real():
        return None

    def spy():
        return None

    spy.livebox_spy = True
    assert s.check_shutdown_raisers(raiser, raiser, spy) is None
    refused("S9", s.check_shutdown_raisers, real, raiser, spy)
    refused("S9", s.check_shutdown_raisers, raiser, real, spy)
    refused("S9", s.check_shutdown_raisers, raiser, raiser, real)


# S10
def test_s10_production_lock_present_pauses_not_aborts(tmp_path):
    s = safety()
    lock = tmp_path / "backup.lock"
    assert s.check_production_lock(lock) is None
    lock.write_text("1234")
    with pytest.raises(s.SafetyPause) as ei:
        s.check_production_lock(lock)
    assert ei.value.rule == "S10"
    assert not isinstance(ei.value, s.SafetyRefusal)
    assert lock.read_text() == "1234"  # read only: never touched


# S11
def test_s11_production_job_within_30_minutes_pauses():
    s = safety()
    crons = {"cloud": "1 2 * * *", "lan": "1 21 * * *", "audit": "0 8 * * 0", "rollover": "0 6 * * *"}
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30))

    def at(h, m):
        return dt.datetime(2026, 11, 24, h, m, tzinfo=tz)  # a fake clock value, never the real clock

    assert s.check_production_schedule(at(12, 0), crons) is None
    for now in (at(20, 45), at(21, 1), at(1, 40)):
        with pytest.raises(s.SafetyPause) as ei:
            s.check_production_schedule(now, crons)
        assert ei.value.rule == "S11"
    assert s.check_production_schedule(at(20, 20), crons) is None  # 41 min before the 21:01 job
    with pytest.raises(s.SafetyPause):
        s.check_production_schedule(at(20, 20), crons, window_minutes=60)


# S12
def test_s12_free_space_and_lane_root_size_abort():
    s = safety()
    assert s.check_resources(200 * GB, 5 * GB) is None
    refused("S12", s.check_resources, 49 * GB, 5 * GB)
    refused("S12", s.check_resources, 200 * GB, 21 * GB)


# S13
def test_s13_more_than_three_lane_processes_refused():
    s = safety()
    assert s.check_lane_process_count(3) is None
    refused("S13", s.check_lane_process_count, 4)


# S14
def test_s14_production_slot_taken_refused():
    s = safety()
    assert s.check_production_slot({"active": 0, "limit": 1}) is None
    refused("S14", s.check_production_slot, {"active": 1, "limit": 1})


# S15
def test_s15_argv_path_outside_lane_root_refused():
    s = safety()
    ok = ["robocopy", ROOT + r"\source", ROOT + r"\nas", "/MIR", "/R:1", "/UNILOG:" + ROOT + r"\logs\a.log"]
    assert s.check_argv(ok, ROOT) is None
    assert s.check_argv(["rclone", "sync", ROOT + r"\source", ROOT + r"\bucket", "--config", ROOT + r"\rclone.conf"], ROOT) is None
    refused("S15", s.check_argv, ["robocopy", ROOT + r"\source", r"E:\Clients", "/MIR"], ROOT)
    refused("S15", s.check_argv, ["robocopy", ROOT + r"\source", ROOT + r"\nas", "/UNILOG:C:\\BackupAgent\\x.log"], ROOT)
    refused("S15", s.check_argv, ["rclone", "sync", ROOT + r"\source", r"\\nas-prod\backup\x"], ROOT)
