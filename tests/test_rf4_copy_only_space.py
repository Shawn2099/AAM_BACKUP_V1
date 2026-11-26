"""Tests for RF-4: copy-only free-space check counts planned move bytes."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_a2_interrupted_run_status import _lan_pipeline, _make_config

import flow
from core import cloud_sync, guards, lan_sync
from models.config import LanConfig


def _jline(**kw) -> str:
    return json.dumps(kw)


def test_rf4_1_planned_move_bytes_sums_move_entries():
    log = "\n".join([
        _jline(level="notice", skipped="move to new/a.txt", size=1000),
        _jline(level="notice", skipped="move to new/b.txt", size=2500),
    ])
    assert cloud_sync.planned_move_bytes(log) == 3500


def test_rf4_2_planned_move_bytes_ignores_non_moves_and_returns_zero():
    log = "\n".join([
        _jline(level="notice", skipped="copy", size=999),
        _jline(level="notice", skipped="delete", size=888),
        "non-json line",
    ])
    assert cloud_sync.planned_move_bytes(log) == 0
    assert cloud_sync.planned_move_bytes("") == 0


def test_rf4_3_planned_move_bytes_returns_none_if_size_missing_or_non_int():
    log_missing = _jline(level="notice", skipped="move to new/a.txt")
    log_bool = _jline(level="notice", skipped="move to new/a.txt", size=True)
    log_str = _jline(level="notice", skipped="move to new/a.txt", size="100")
    assert cloud_sync.planned_move_bytes(log_missing) is None
    assert cloud_sync.planned_move_bytes(log_bool) is None
    assert cloud_sync.planned_move_bytes(log_str) is None


def test_rf4_4_plan_lan_mirror_sets_planned_move_bytes(tmp_path):
    art_dir = tmp_path / "art"
    art_dir.mkdir()
    log_lines = "\n".join([
        _jline(level="notice", skipped="copy", size=400),
        _jline(level="notice", skipped="move to b.txt", size=600),
        _jline(level="info", stats={"transfers": 0, "deletes": 0, "errors": 0}),
    ])

    def fake_run_tool(step, cmd, timeout_s=None, artefacts=None, log_path=None):
        # Write to the actual log_path and combined_path passed in cmd
        for i, tok in enumerate(cmd):
            if tok == "--log-file" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w", encoding="utf-8") as f:
                    f.write(log_lines)
            if tok == "--combined" and i + 1 < len(cmd):
                with open(cmd[i + 1], "w", encoding="utf-8") as f:
                    f.write("+ b.txt\n- a.txt\n")
        return SimpleNamespace(
            exit_code=0,
            spawn_error=None,
            timed_out=False,
            log_text=lambda: log_lines,
            stderr_text=lambda: "",
        )

    cfg = LanConfig()
    with patch("core.lan_sync.run_tool", side_effect=fake_run_tool):
        plan = lan_sync.plan_lan_mirror(cfg, "C:/src", "//nas/dest", str(art_dir))

    assert plan["ok"] is True
    assert plan["planned_bytes"] == 400
    assert plan["planned_move_bytes"] == 600


def _fake_lan_cfg(versions_enabled: bool = False):
    return SimpleNamespace(
        paths=SimpleNamespace(source_drive="C:/src", lan_destination="//nas/dest"),
        lan=SimpleNamespace(min_free_dest_gb=1.0, versions_enabled=versions_enabled),
    )


def test_rf4_5_lan_access_task_normal_sync_ignores_move_bytes():
    cfg = _fake_lan_cfg()
    plan = {"planned_bytes": 1000, "planned_move_bytes": 5000}
    with patch("flow.permissions.probe_source_read"), \
            patch("flow.permissions.probe_destination_write"), \
            patch("flow.permissions.check_destination_free_space") as chk:
        flow.lan_access_task.fn(cfg, plan, "run-1")
    assert chk.call_args.kwargs["bytes_needed"] == 1000


def test_rf4_6_lan_access_task_copy_only_adds_move_bytes():
    cfg = _fake_lan_cfg()
    plan = {"planned_bytes": 1000, "planned_move_bytes": 5000}
    with patch("flow.permissions.probe_source_read"), \
            patch("flow.permissions.probe_destination_write"), \
            patch("flow.permissions.check_destination_free_space") as chk:
        flow.lan_access_task.fn(cfg, plan, "run-1", copy_only=True)
    assert chk.call_args.kwargs["bytes_needed"] == 6000


def test_rf4_7_lan_access_task_copy_only_fails_closed_when_either_none():
    cfg = _fake_lan_cfg()
    for plan in (
        {"planned_bytes": None, "planned_move_bytes": 500},
        {"planned_bytes": 500, "planned_move_bytes": None},
    ):
        with patch("flow.permissions.probe_source_read"), \
                patch("flow.permissions.probe_destination_write"), \
                patch("flow.permissions.check_destination_free_space") as chk:
            flow.lan_access_task.fn(cfg, plan, "run-1", copy_only=True)
        assert chk.call_args.kwargs["bytes_needed"] is None


def test_rf4_8_lan_g3_copy_only_passes_copy_only_true_to_lan_access():
    try:
        guards.enforce("lan", [guards.g3_mass_delete(900, 1000, 50, 10.0)], fy="FY26-27", max_delete=0)
    except guards.GuardBlocked as gb:
        rec_g3 = gb.record
    with _lan_pipeline("pre", None), \
            patch("flow.lan_guards_task", side_effect=guards.GuardBlocked("lan", rec_g3)), \
            patch("flow.lan_access_task") as access_mock, \
            patch("flow.lan_sync_task") as sync_mock, \
            patch("flow.notify"), \
            patch("flow.ManifestDB") as mdb:
        mdb.from_config.return_value.open_approval_request.return_value = "appr-rf4"
        sync_mock.with_options.return_value.return_value = {
            "status": "LAN_COMPLETE", "exit_code": 0, "stats": {"transfers": 1, "bytes": 100},
        }
        with pytest.raises(guards.GuardBlocked):
            flow._run_lan_pipeline(_make_config(), "run-rf4", "2026-11-25T21:00:00", allow_shutdown=False)

    assert access_mock.call_args.kwargs.get("copy_only") is True
