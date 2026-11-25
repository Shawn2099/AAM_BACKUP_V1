"""Tests for WS-C C8 (F2): cloud_sync_task killed_after_cutoff per R6."""
from __future__ import annotations

import time
from types import SimpleNamespace

import flow
from core.budget import RunBudget


def _cfg(tmp_path):
    return SimpleNamespace(
        paths=SimpleNamespace(source_drive=str(tmp_path / "src"), gcs_key_path=str(tmp_path / "k.json")),
        cloud=SimpleNamespace(
            bucket="b", location="asia-south1", project_number="1", storage_class="STANDARD",
            bandwidth_limit="0", retry_count=2, transfers=2, checkers=2, buffer_size="16M",
            modify_window_seconds=2, sync_retries_sleep_seconds=5, stats_interval_seconds=60,
            subprocess_timeout_seconds=3600, max_duration_seconds=None, error_tail_lines=100,
            track_renames=True,
        ),
        budget=SimpleNamespace(cloud_verify_reserve_minutes=5),
    )


def test_c8_cloud_hang_after_cutoff_sets_killed_after_cutoff(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    budget = RunBudget("cloud", 1500, time.monotonic())  # 1500 - 300 reserve = 1200s > 300s margin -> max_duration=900
    monkeypatch.setattr(
        flow, "run_cloud_sync",
        lambda **kw: {"status": "CLOUD_FAILED", "exit_code": -1, "timed_out": True, "stats": None},
    )
    res = flow.cloud_sync_task.fn(cfg, "FY26-27", max_delete=5, budget=budget)
    assert res["status"] == "CLOUD_PARTIAL"
    assert res["killed_after_cutoff"] is True
    assert res["budget_stopped"] is False
    assert res["fault_code"] == "AAM-CLD-08"
    assert "did not stop within" in res["error"]


def test_c8_cloud_small_budget_without_cutoff_stays_budget_stopped(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    budget = RunBudget("cloud", 500, time.monotonic())  # 500 - 300 reserve = 200s <= 300s margin -> max_duration=None
    monkeypatch.setattr(
        flow, "run_cloud_sync",
        lambda **kw: {"status": "CLOUD_FAILED", "exit_code": -1, "timed_out": True, "stats": None},
    )
    res = flow.cloud_sync_task.fn(cfg, "FY26-27", max_delete=5, budget=budget)
    assert res["status"] == "CLOUD_PARTIAL"
    assert res.get("budget_stopped") is True
    assert not res.get("killed_after_cutoff")
