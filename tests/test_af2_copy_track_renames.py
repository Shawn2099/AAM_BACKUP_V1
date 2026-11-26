"""Tests for AF-2: cloud copy mode never passes --track-renames to rclone copy."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_a2_interrupted_run_status import _cloud_pipeline

import flow
from core import cloud_sync, guards
from models.config import AppConfig, CloudConfig, PathsConfig, WolConfig


def test_af2_builder_copy_mode_omits_track_renames(tmp_path):
    src = tmp_path / "FY26-27"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")
    conf = str(tmp_path / "rclone.conf")

    copy_cmd = cloud_sync.build_rclone_sync_command(
        str(src), "bucket", "FY26-27", conf, "STANDARD",
        track_renames=True, mode="copy",
    )
    assert copy_cmd[1] == "copy"
    assert not any(arg.startswith("--track-renames") for arg in copy_cmd)

    sync_on = cloud_sync.build_rclone_sync_command(
        str(src), "bucket", "FY26-27", conf, "STANDARD",
        track_renames=True, mode="sync",
    )
    assert sync_on[1] == "sync"
    assert "--track-renames" in sync_on

    sync_off = cloud_sync.build_rclone_sync_command(
        str(src), "bucket", "FY26-27", conf, "STANDARD",
        track_renames=False, mode="sync",
    )
    assert sync_off[1] == "sync"
    assert not any(arg.startswith("--track-renames") for arg in sync_off)


def test_af2_copy_only_run_clean_log_populates_copied_and_no_error(tmp_path):
    src = tmp_path / "FY26-27"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")
    key = tmp_path / "key.json"
    key.write_text("{}", encoding="utf-8")

    cfg = AppConfig(
        paths=PathsConfig(
            source_drive=str(src),
            lan_destination=r"\\nas\share\FY26-27",
            gcs_key_path=str(key),
            database_path=str(tmp_path / "test.db"),
        ),
        wol=WolConfig(enabled=False),
        cloud=CloudConfig(bucket="test-bucket", project_number="123456", track_renames=True),
    )

    with pytest.raises(guards.GuardBlocked) as exc_info:
        guards.enforce("cloud", [
            guards.g1_source_has_files("a.txt", str(src)),
            guards.g3_mass_delete(900, 1000, 50, 10.0),
        ])
    real_record = exc_info.value.record

    seen_cmds: list[list[str]] = []

    def fake_run_tool(step, cmd, timeout_s=None, artefacts=None, log_path=None, **_kw):
        seen_cmds.append(list(cmd))
        return SimpleNamespace(
            completed=True, exit_code=0, spawn_error=None, timed_out=False, stalled=False,
            log_text=lambda: '{"level":"info","msg":"Transferred","stats":{"transfers":3,"bytes":2048,"deletes":0,"errors":0,"checks":1}}\n',
            stderr_text=lambda: "",
            stdout_text=lambda: "",
            describe_failure=lambda: "",
        )

    orig_cloud_sync_fn = flow.cloud_sync_task.fn
    with _cloud_pipeline("pre", None) as (record_run, _rec, _alert), \
            patch("flow.cloud_guards_task", side_effect=guards.GuardBlocked("cloud", real_record)), \
            patch("flow.cloud_access_task"), \
            patch("flow.cloud_sync_task") as sync_task, \
            patch("core.cloud_sync.run_tool", side_effect=fake_run_tool), \
            patch("flow.notify"), \
            patch("flow.ManifestDB") as mdb:
        sync_task.with_options.return_value.side_effect = orig_cloud_sync_fn
        mdb.from_config.return_value.open_approval_request.return_value = "appr-af2"
        with pytest.raises(guards.GuardBlocked):
            flow._run_cloud_pipeline(cfg, "run-af2", "2026-11-25T02:00:00")

    assert len(seen_cmds) == 1
    assert not any(arg.startswith("--track-renames") for arg in seen_cmds[0])
    ext = json.loads(record_run.call_args.kwargs["extended_metrics"])
    assert ext["guards"]["copy_only"]["error"] is None
    assert ext["guards"]["copy_only"]["copied"] == 3


def test_af2_real_rclone_copy_with_builder_flags_has_no_errors(tmp_path):
    rclone_exe = Path("deploy/bin/rclone.exe")
    if not rclone_exe.is_file():
        pytest.skip("deploy/bin/rclone.exe not present")

    import subprocess

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    log_path = tmp_path / "rclone.log"

    # Build real copy command and adapt GCS-only flags to local dirs so we test
    # the exact flag set our builder emits for mode="copy".
    cmd = cloud_sync.build_rclone_sync_command(
        str(src), "bucket", "FY26-27", str(tmp_path / "empty.conf"), "STANDARD",
        track_renames=True, mode="copy",
    )
    assert not any(arg.startswith("--track-renames") for arg in cmd)
    local_cmd = [str(rclone_exe), "copy", str(src), str(dst), "--log-file", str(log_path)]
    skip_next = False
    for i, arg in enumerate(cmd[4:]):
        if skip_next:
            skip_next = False
            continue
        if arg in ("--config", "--gcs-storage-class"):
            skip_next = True
            continue
        if arg in ("--fast-list", "--gcs-no-check-bucket"):
            continue
        local_cmd.append(arg)

    proc = subprocess.run(local_cmd, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    log_text = log_path.read_text(encoding="utf-8")
    has_error, tail = cloud_sync.scan_rclone_log_for_errors(log_text)
    assert not has_error, tail
    assert (dst / "a.txt").read_text(encoding="utf-8") == "hello"
