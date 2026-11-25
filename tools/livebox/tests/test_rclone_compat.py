"""Compatibility tests for rclone LAN engine in tools/livebox (WS-D D3, D5, D6)."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tools.livebox import calendar as cal
from tools.livebox import oracles, safety
from tools.livebox import run_lane as rl


def _make_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE run_history ("
        "id INTEGER PRIMARY KEY, run_id TEXT, mode TEXT, started_at TEXT, "
        "status TEXT, files_copied INTEGER, files_deleted INTEGER, fault_code TEXT, extended_metrics TEXT)"
    )
    return conn


def test_check_tree_detects_missing_and_extra_empty_dirs(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "empty_a").mkdir(parents=True)
    (src / "sub" / "file.txt").parent.mkdir(parents=True)
    (src / "sub" / "file.txt").write_text("hi", encoding="utf-8")

    (dst / "empty_a").mkdir(parents=True)
    (dst / "sub").mkdir(parents=True)
    (dst / "sub" / "file.txt").write_text("hi", encoding="utf-8")
    assert oracles.check_tree(src, dst) == []

    (dst / "empty_a").rmdir()
    v = oracles.check_tree(src, dst)
    assert len(v) == 1 and "empty_dirs" in v[0].detail and "empty_a" in v[0].detail

    (dst / "empty_a").mkdir()
    (dst / "extra_empty").mkdir()
    v2 = oracles.check_tree(src, dst)
    assert len(v2) == 1 and "extra_empty" in v2[0].detail


def test_check_rclone_lan_metrics_validates_complete_partial_suspect_blocked(tmp_path):
    db = tmp_path / "manifest.db"
    day = dt.date(2027, 1, 5)
    ts = "2027-01-05T21:05:00+05:30"
    conn = _make_db(db)
    try:
        conn.execute(
            "INSERT INTO run_history (run_id, mode, started_at, status, files_copied, files_deleted, fault_code, extended_metrics) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "lan", ts, "LAN_COMPLETE", 3, 0, None,
             json.dumps({"counts_source": "rclone", "arrival": {"verified": True, "hash": {"ran": True, "verified": True}}})),
        )
        conn.execute(
            "INSERT INTO run_history (run_id, mode, started_at, status, files_copied, files_deleted, fault_code, extended_metrics) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r2", "lan", ts, "LAN_PARTIAL", 1, 0, "AAM-LAN-02",
             json.dumps({"counts_source": "rclone", "deletes_held": True})),
        )
        conn.execute(
            "INSERT INTO run_history (run_id, mode, started_at, status, files_copied, files_deleted, fault_code, extended_metrics) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r3", "lan", ts, "LAN_SUSPECT", 1, 0, "AAM-LAN-04",
             json.dumps({"counts_source": "rclone", "arrival": {"verified": False}})),
        )
        conn.execute(
            "INSERT INTO run_history (run_id, mode, started_at, status, files_copied, files_deleted, fault_code, extended_metrics) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r4", "lan", ts, "LAN_BLOCKED", 0, 0, "AAM-DEL-03",
             json.dumps({"max_delete_reached": True})),
        )
        conn.commit()
    finally:
        conn.close()

    ro = oracles.open_ro(db)
    try:
        assert oracles.check_rclone_lan_metrics(ro, day) == []
    finally:
        ro.close()


def test_check_rclone_lan_metrics_flags_missing_hash_on_copied_complete(tmp_path):
    db = tmp_path / "manifest.db"
    day = dt.date(2027, 1, 5)
    conn = _make_db(db)
    conn.execute(
        "INSERT INTO run_history (run_id, mode, started_at, status, files_copied, files_deleted, fault_code, extended_metrics) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("r1", "lan", "2027-01-05T21:05:00+05:30", "LAN_COMPLETE", 2, 0, None,
         json.dumps({"counts_source": "rclone", "arrival": {"verified": True, "hash": {}}})),
    )
    conn.commit()
    conn.close()

    ro = oracles.open_ro(db)
    try:
        v = oracles.check_rclone_lan_metrics(ro, day)
        assert len(v) == 1 and "arrival.hash" in v[0].detail
    finally:
        ro.close()


def test_check_rclone_lan_metrics_validates_killed_after_cutoff_and_partial_suffix(tmp_path):
    db = tmp_path / "manifest.db"
    day = dt.date(2027, 1, 5)
    ts = "2027-01-05T21:05:00+05:30"
    conn = _make_db(db)
    conn.execute(
        "INSERT INTO run_history (run_id, mode, started_at, status, files_copied, files_deleted, fault_code, extended_metrics) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("r_ok", "lan", ts, "LAN_PARTIAL", 0, 0, "AAM-LAN-04",
         json.dumps({"counts_source": "rclone", "killed_after_cutoff": True, "budget_stopped": False, "failed_paths": ["ro.txt"]})),
    )
    conn.execute(
        "INSERT INTO run_history (run_id, mode, started_at, status, files_copied, files_deleted, fault_code, extended_metrics) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("r_bad", "lan", ts, "LAN_PARTIAL", 0, 0, "AAM-LAN-03",
         json.dumps({"counts_source": "rclone", "killed_after_cutoff": True, "budget_stopped": True,
                     "failed_paths": ["ro.txt.7514dbf0.partial"]})),
    )
    conn.commit()
    conn.close()

    ro = oracles.open_ro(db)
    try:
        v = oracles.check_rclone_lan_metrics(ro, day)
        details = [x.detail for x in v]
        assert len(v) == 3
        assert any("AAM-LAN-04" in d for d in details)
        assert any("budget_stopped" in d for d in details)
        assert any("unstripped partial suffix" in d for d in details)
    finally:
        ro.close()


def test_s15_checks_rclone_lan_hash_files_from_raw_path(tmp_path):
    lane = str(tmp_path / "stamp1" / "A")
    safety.check_argv(
        ["rclone.exe", "check", lane + r"\src", lane + r"\nas", "--one-way", "--files-from-raw", lane + r"\list.txt"],
        lane,
    )
    with pytest.raises(safety.SafetyRefusal):
        safety.check_argv(
            ["rclone.exe", "check", lane + r"\src", lane + r"\nas", "--files-from-raw", r"C:\BackupAgent\list.txt"],
            lane,
        )


def test_spy_rollover_nas_allows_wake_on_switch_day_only(tmp_path):
    lane = tmp_path / "stamp1" / "A"
    lane.mkdir(parents=True)
    h = rl.LaneHooks(lane, lane="A", stamp="stamp1", days=[], agent_runs=tmp_path / "ar", allow_dev_path=True)
    h.rig = SimpleNamespace(
        config={"lan": {"shutdown_after_backup": True}},
        runtime_dir=lane / "runtime",
    )
    ev = cal.Event(dt.datetime(2091, 4, 1, 6, 0, tzinfo=cal.IST), cal.ROLLOVER_CHECK, "FY90-91")

    switch_day = SimpleNamespace(date=dt.date(2091, 4, 1), is_switch_day=True, rollover_result=None, rows=[], expect_raise=())
    out = SimpleNamespace(new_rows=[], flow=SimpleNamespace(value=None, exc=None), value=None, exc=None, checks=[])
    h._event_checks(switch_day, ev, out, wake=2, shut=0, arch=1)
    assert any(c["check_id"] == "SPY_ROLLOVER_NAS" and c["verdict"] == "PASS" for c in out.checks)

    non_switch_day = SimpleNamespace(date=dt.date(2091, 4, 2), is_switch_day=False, rollover_result=None, rows=[], expect_raise=())
    out2 = SimpleNamespace(new_rows=[], flow=SimpleNamespace(value=None, exc=None), value=None, exc=None, checks=[])
    h._event_checks(non_switch_day, ev, out2, wake=1, shut=0, arch=0)
    assert any(c["check_id"] == "SPY_ROLLOVER_NAS" and c["verdict"] == "FINDING" for c in out2.checks)
