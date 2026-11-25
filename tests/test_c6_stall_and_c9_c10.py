"""Tests for WS-C C6 (S6 stall watchdog), C9 (F3 rstrip + --delete-after), C10 (F1 real deletes)."""
from __future__ import annotations

import sys

import pytest
from pydantic import ValidationError

from core import cloud_sync, lan_sync
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool
from models.config import LanConfig, ToolsConfig, load_config


def test_c6_tools_config_stall_timeout_bounds():
    assert ToolsConfig().stall_timeout_seconds == 1800
    assert ToolsConfig(stall_timeout_seconds=0).stall_timeout_seconds == 0
    assert ToolsConfig(stall_timeout_seconds=300).stall_timeout_seconds == 300
    assert ToolsConfig(stall_timeout_seconds=14400).stall_timeout_seconds == 14400
    for bad in (-1, 1, 299, 14401):
        with pytest.raises(ValidationError):
            ToolsConfig(stall_timeout_seconds=bad)
    cfg = load_config("config.example.yaml")
    assert cfg.tools.stall_timeout_seconds == 1800


def test_c6_run_tool_kills_real_hanging_child_on_stall(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.05)
    art = RunArtefacts.open(tmp_path / "art")
    res = run_tool(
        "hang-test",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_s=10.0,
        stall_timeout_s=0.25,
        artefacts=art,
    )
    assert res.stalled is True
    assert res.timed_out is False
    assert res.exit_code is None
    assert "stalled" in res.describe_failure().lower()


def test_c9_decide_lan_arrival_preserves_leading_and_trailing_spaces():
    stderr = "NOTICE: x: 0 differences found\nNOTICE: x: 1 matching files\n"
    res = lan_sync.decide_lan_arrival(0, stderr, "=  lead.txt\n= trail.txt \n", lambda p: False)
    assert res["verified"] is True

    # Missing file with leading space must preserve the leading space in unverified_paths
    stderr1 = "NOTICE: x: 1 differences found\nNOTICE: x: 0 matching files\n"
    res2 = lan_sync.decide_lan_arrival(1, stderr1, "+  lead.txt\n", lambda p: False)
    assert res2["verified"] is False
    assert res2["unverified_paths"] == [" lead.txt"]


def test_c9_build_lan_rclone_command_includes_delete_after_on_sync(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")
    cmd = lan_sync.build_lan_rclone_command(LanConfig(), str(src), r"\\NAS\b\FY26-27", str(tmp_path / "l.log"), mode="sync")
    assert "--delete-after" in cmd
    cmd_copy = lan_sync.build_lan_rclone_command(LanConfig(), str(src), r"\\NAS\b\FY26-27", str(tmp_path / "c.log"), mode="copy")
    assert "--delete-after" not in cmd_copy


def test_c10_counted_deletes_excludes_failed_delete_attempts():
    # X3: stats.deletes reports 3 when 1 delete failed twice with --retries 2 and only 2 files were deleted
    log = "\n".join([
        '{"level":"info","msg":"Deleted","object":"a.txt"}',
        '{"level":"info","msg":"Moved into backup dir","object":"b.txt"}',
        '{"level":"error","msg":"Couldn\'t delete: sharing violation","object":"c.txt"}',
        '{"level":"notice","stats":{"deletes":3,"transfers":0,"renames":0,"errors":2}}',
    ])
    assert cloud_sync.counted_deletes(log) == 2
