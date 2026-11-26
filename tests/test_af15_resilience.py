"""AF-15: stall retryable checks, rclone_version_fault timeout/caching, dry init."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from core import cloud_sync, guards, health
import flow
from models.config import ToolsConfig
from test_support.fake_tools import fake_run_tool


def test_af15_tools_config_has_rclone_version_timeout():
    cfg = ToolsConfig()
    assert cfg.rclone_version_timeout_seconds == 30


def test_af15_stall_with_fatal_error_or_missing_stats_is_not_retryable(tmp_path):
    src = tmp_path / "FY26-27"
    src.mkdir()
    (src / "a.txt").write_text("x", encoding="utf-8")

    # Missing stats -> not retryable (fail closed)
    res_missing = MagicMock(spawn_error=None, stalled=True, timed_out=False, exit_code=-1,
                            log_text=lambda: "", stderr_text=lambda: "",
                            describe_failure=lambda: "stalled")
    with patch("core.cloud_sync.run_tool", return_value=res_missing):
        res_no_stats = cloud_sync.run_cloud_sync(
            str(src), "test-bucket", "FY26-27", str(tmp_path / "k.json"),
            "123456", "STANDARD",
        )
    assert res_no_stats["stalled"] is True
    assert res_no_stats["retryable"] is False

    # fatalError in stats -> not retryable
    fatal_log = json.dumps({"level": "info", "stats": {"deletes": 1, "fatalError": True}})
    res_fatal_tool = MagicMock(spawn_error=None, stalled=True, timed_out=False, exit_code=-1,
                               log_text=lambda: fatal_log, stderr_text=lambda: "",
                               describe_failure=lambda: "stalled")
    with patch("core.cloud_sync.run_tool", return_value=res_fatal_tool):
        res_fatal = cloud_sync.run_cloud_sync(
            str(src), "test-bucket", "FY26-27", str(tmp_path / "k.json"),
            "123456", "STANDARD",
        )
    assert res_fatal["retryable"] is False


def test_af15_rclone_version_fault_does_not_cache_transient_failure():
    health._RCLONE_VERSION_CACHE.clear()
    try:
        with patch("core.health.resolve_binary", return_value="rclone"), \
             patch("core.health.run_tool", side_effect=[
                 MagicMock(spawn_error=None, timed_out=True, exit_code=-1, stdout_text=lambda: ""),
                 MagicMock(spawn_error=None, timed_out=False, exit_code=0,
                           stdout_text=lambda: "rclone v1.74.3\n"),
             ]) as rt:
            first = health.rclone_version_fault("1.74", timeout_s=15)
            second = health.rclone_version_fault("1.74", timeout_s=15)
        assert first is not None
        assert second is None
        assert rt.call_count == 2
        assert rt.call_args_list[0].kwargs["timeout_s"] == 15
    finally:
        health._RCLONE_VERSION_CACHE.clear()


def test_af15_cloud_guards_g3_approved_with_empty_source_raises_guard_blocked(tmp_path):
    empty_src = tmp_path / "FY26-27"
    empty_src.mkdir()
    cfg = MagicMock()
    cfg.paths.source_drive = str(empty_src)
    cfg.cloud.bucket = "test-bucket"
    cfg.guards = MagicMock(max_delete_files=500, max_delete_percent=10.0,
                           min_delete_floor_files=20, max_source_shrink_percent=20.0)
    with patch("flow.ManifestDB") as mdb, \
         patch("flow._apply_approval", return_value=(frozenset({"G3_MASS_DELETE"}), {})):
        mdb.from_config.return_value.last_successful_run.return_value = None
        with pytest.raises(guards.GuardBlocked):
            flow.cloud_guards_task.fn(cfg, "FY26-27", "run-empty", approval_id="app-1")
