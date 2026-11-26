"""Logical fixes from Appendix A (A5/A7): copy-only deletes_held, blocked_guard_code, rollover shutdown, watchdog lock path."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import flow
import watchdog
from core import cloud_sync, faults, guards
from models.config import load_config


def test_l1_cloud_exit_5_sets_retryable_true(tmp_path):
    cfg = load_config().cloud
    stats_line = json.dumps({"level": "info", "msg": "stats", "stats": {"deletes": 0, "transfers": 1}})
    log_p = tmp_path / "c.jsonl"
    log_p.write_text(stats_line + "\n", encoding="utf-8")
    art = tmp_path / "art"
    art.mkdir()
    tr = SimpleNamespace(
        spawn_error=None, stalled=False, timed_out=False, exit_code=5,
        log_path=log_p, log_text=lambda: stats_line + "\n", stderr_text=lambda: "",
        describe_failure=lambda: "exit 5",
    )
    key_file = tmp_path / "key.json"
    key_file.write_text("{}", encoding="utf-8")
    with patch.object(cloud_sync, "run_tool", return_value=tr):
        res = cloud_sync.run_cloud_sync(
            str(tmp_path), "b", "FY26-27", str(key_file), "proj", "STANDARD",
            max_delete=10,
        )
    assert res["status"] == "CLOUD_PARTIAL"
    assert res["retryable"] is True


def test_l2_cloud_copy_only_records_planned_deletes_from_guard_results():
    cfg = load_config()
    cfg.cloud.enabled = True
    r = guards.GuardResult(
        guard="G3_MASS_DELETE",
        verdict=guards.BLOCK,
        reason="too many deletes",
        metrics={"planned_deletes": 321},
    )
    record = {"verdict": guards.BLOCK, "results": [r.__dict__], "max_delete": 10}
    gb = guards.GuardBlocked("G3 blocked", record=record)

    with (
        patch.object(flow, "_backup_slot") as slot,
        patch.object(flow, "_record_run"),
        patch.object(flow, "_send_run_alert"),
        patch.object(flow, "_finalize_run_artefacts"),
        patch.object(flow, "_open_run_artefacts", return_value=None),
        patch.object(flow, "health_check_task", return_value={"verified": True}),
        patch.object(flow, "cloud_preflight_task"),
        patch.object(flow, "cloud_guards_task", side_effect=gb),
        patch.object(flow, "cloud_access_task"),
        patch.object(flow, "cloud_sync_task", return_value={"status": "CLOUD_COMPLETE", "stats": {"transfers": 1, "bytes": 10}}),
    ):
        slot.return_value.__enter__ = MagicMock()
        slot.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises(guards.GuardBlocked):
            flow._run_cloud_pipeline(cfg, "r-l2", "2026-11-26T02:01:00+05:30", time.monotonic())
    assert gb.record["copy_only"]["deletes_held"] == 321


def test_l3_blocked_guard_code_skips_approved_guards():
    record = {
        "approved_guards": ["G1_SOURCE_HAS_FILES"],
        "results": [
            {"guard": "G1_SOURCE_HAS_FILES", "verdict": "BLOCK", "fault_code": "AAM-SRC-03"},
            {"guard": "G3_MASS_DELETE", "verdict": "BLOCK", "fault_code": "AAM-DEL-01"},
        ],
    }
    assert faults.blocked_guard_code(record) == "AAM-DEL-01"


def test_l5_watchdog_reload_tunables_updates_backup_lock_path(tmp_path, monkeypatch):
    cfg = load_config()
    new_db = tmp_path / "new_runtime" / "manifest.db"
    cfg.paths.database_path = str(new_db)
    cfg.paths.runtime_dir = str(tmp_path / "new_runtime")

    old_lock = watchdog.BACKUP_LOCK_PATH
    try:
        monkeypatch.setattr(watchdog, "_config_mtime", lambda: 999999.0)
        with patch("models.config.load_config", return_value=cfg):
            watchdog._reload_tunables(1.0)
        assert watchdog.BACKUP_LOCK_PATH == cfg.paths.backup_lock_path
    finally:
        watchdog.BACKUP_LOCK_PATH = old_lock
