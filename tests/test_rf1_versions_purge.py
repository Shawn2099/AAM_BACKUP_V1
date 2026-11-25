"""Tests for RF-1: versions purge by run-folder date (`rclone purge`)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pydantic import ValidationError

from core import lan_versions
from models.config import LanConfig


def _ok_res():
    return SimpleNamespace(exit_code=0, spawn_error=None, timed_out=False)


def _fail_res(code=1):
    return SimpleNamespace(exit_code=code, spawn_error=None, timed_out=False)


def test_rf1_1_and_2_purges_only_due_run_folders_with_rclone_purge(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    mirror = nas / "FY26-27"
    mirror.mkdir(parents=True)
    vroot = nas / "AAM_versions"
    vroot.mkdir()
    f1 = vroot / "2026-08-01_2101_FY26-27"
    f2 = vroot / "2026-09-02_2101_FY26-27"
    f3 = vroot / "2026-11-20_2101_FY26-27"
    f4 = vroot / "not_a_run_folder"
    for d in (f1, f2, f3, f4):
        d.mkdir()
    (vroot / "notes.txt").write_text("keep", encoding="utf-8")

    calls = []
    monkeypatch.setattr(
        lan_versions, "run_tool",
        lambda step, cmd, **kw: calls.append((step, cmd)) or _ok_res(),
    )
    res = lan_versions.purge_old_versions(
        str(mirror), "AAM_versions", retention_days=91, today=dt.date(2026, 12, 2),
    )
    assert res["ok"] is True
    assert res["purged"] == ["2026-08-01_2101_FY26-27", "2026-09-02_2101_FY26-27"]
    assert res["failed"] == []
    assert [c[0] for c in calls] == ["rclone-versions-purge", "rclone-versions-purge"]
    for _, cmd in calls:
        assert cmd[1] == "purge"
        assert "delete" not in cmd and "--min-age" not in cmd and "rmdirs" not in cmd


def test_rf1_3_one_failure_does_not_stop_other_due_folders(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    mirror = nas / "FY26-27"
    mirror.mkdir(parents=True)
    vroot = nas / "AAM_versions"
    vroot.mkdir()
    (vroot / "2026-08-01_2101_FY26-27").mkdir()
    (vroot / "2026-08-02_2101_FY26-27").mkdir()

    calls = []

    def _fake(step, cmd, **kw):
        calls.append(cmd)
        return _fail_res(1) if len(calls) == 1 else _ok_res()

    monkeypatch.setattr(lan_versions, "run_tool", _fake)
    res = lan_versions.purge_old_versions(
        str(mirror), "AAM_versions", retention_days=91, today=dt.date(2026, 12, 2),
    )
    assert res["ok"] is False
    assert res["purged"] == ["2026-08-02_2101_FY26-27"]
    assert len(res["failed"]) == 1
    assert res["failed"][0]["folder"] == "2026-08-01_2101_FY26-27"


def test_rf1_4_boundary_90_days_not_purged(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    mirror = nas / "FY26-27"
    mirror.mkdir(parents=True)
    vroot = nas / "AAM_versions"
    vroot.mkdir()
    (vroot / "2026-09-03_2101_FY26-27").mkdir()

    calls = []
    monkeypatch.setattr(lan_versions, "run_tool", lambda *a, **kw: calls.append(a) or _ok_res())
    res = lan_versions.purge_old_versions(
        str(mirror), "AAM_versions", retention_days=91, today=dt.date(2026, 12, 2),
    )
    assert res["ok"] is True
    assert res["purged"] == []
    assert calls == []


def test_rf1_5_absent_versions_root_skips(tmp_path, monkeypatch):
    mirror = tmp_path / "nas" / "FY26-27"
    mirror.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(lan_versions, "run_tool", lambda *a, **kw: calls.append(a) or _ok_res())
    res = lan_versions.purge_old_versions(str(mirror), "AAM_versions", retention_days=91)
    assert res["ok"] is True
    assert res["skipped"] == "versions folder absent"
    assert res["purged"] == [] and res["failed"] == []
    assert calls == []


def test_rf1_6_outside_versions_root_is_skipped_and_recorded_failed(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    mirror = nas / "FY26-27"
    mirror.mkdir(parents=True)
    vroot = nas / "AAM_versions"
    vroot.mkdir()
    outside = tmp_path / "outside_dir"
    outside.mkdir()

    monkeypatch.setattr(lan_versions, "folders_to_purge", lambda *a, **kw: [outside])
    calls = []
    monkeypatch.setattr(lan_versions, "run_tool", lambda *a, **kw: calls.append(a) or _ok_res())
    res = lan_versions.purge_old_versions(
        str(mirror), "AAM_versions", retention_days=91, today=dt.date(2026, 12, 2),
    )
    assert res["ok"] is False
    assert res["purged"] == []
    assert res["failed"] == [{"folder": "outside_dir", "error": "outside versions root"}]
    assert calls == []


def test_rf1_7_config_key_bounds_help_and_example():
    from core.config_help import HELP

    assert LanConfig().versions_purge_timeout_seconds == 1800
    assert LanConfig(versions_purge_timeout_seconds=60).versions_purge_timeout_seconds == 60
    assert LanConfig(versions_purge_timeout_seconds=14400).versions_purge_timeout_seconds == 14400
    for bad in (59, 14401):
        with pytest.raises(ValidationError):
            LanConfig(versions_purge_timeout_seconds=bad)
    assert "lan.versions_purge_timeout_seconds" in HELP
    ex = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    assert ex["lan"]["versions_purge_timeout_seconds"] == 1800


def test_rf1_8_flow_passes_versions_purge_timeout_and_swallows_errors(tmp_path, monkeypatch):
    import flow
    from core import statuses

    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    cfg = MagicMock()
    cfg.paths.source_drive = str(tmp_path / "FY26-27")
    cfg.paths.lan_destination = r"\\nas\share\FY26-27"
    cfg.paths.database_path = str(tmp_path / "m.db")
    cfg.paths.backup_lock_path = tmp_path / "backup.lock"
    cfg.paths.run_logs_dir = str(tmp_path / "run_logs")
    cfg.lan.enabled = True
    cfg.lan.versions_enabled = True
    cfg.lan.versions_folder = "AAM_versions"
    cfg.lan.versions_retention_days = 91
    cfg.lan.versions_purge_timeout_seconds = 1234
    cfg.lan.max_attempts = 1
    cfg.lan.retry_delay_seconds = 60
    cfg.lan.error_tail_lines = 10
    cfg.lan.shutdown_after_backup = False
    cfg.budget.lan_max_hours = 4
    cfg.budget.lan_post_reserve_minutes = 30
    cfg.maintenance.sqlite_synchronous = "normal"

    with patch("flow.notify"), \
            patch("flow._send_run_alert"), \
            patch("flow._record_run") as rec, \
            patch("flow._send_recovery_if_needed"), \
            patch("flow.health_check_task"), \
            patch("flow.wol_check_task"), \
            patch("flow.lan_preflight_task", return_value={"ok": True, "source_files": 1, "dest_files": 1, "planned_deletes": 0, "canary_present": True}), \
            patch("flow.lan_guards_task", return_value={"max_delete": 10}), \
            patch("flow.lan_access_task"), \
            patch("flow.lan_sync_task") as sync, \
            patch("flow.lan_verify_task", return_value={"verified": True, "matching": 1, "missing": 0, "differ": 0, "changed_during_backup": 0}), \
            patch("flow._hash_plan", return_value=({"ran": True, "verified": True}, [])), \
            patch("core.lan_versions.purge_old_versions", side_effect=RuntimeError("boom")) as purge_mock:
        sync.with_options.return_value.return_value = {
            "status": "LAN_COMPLETE", "exit_code": 0, "files_failed": 0,
            "stats": {"transfers": 1, "bytes": 10, "deletes": 0, "renames": 0, "errors": 0, "checks": 1},
        }
        res = flow._run_lan_pipeline(cfg, "run-rf1", "2026-11-25T21:00:00", allow_shutdown=False)
    assert res["status"] == "LAN_COMPLETE"
    assert statuses.is_success(rec.call_args.args[4])
    assert purge_mock.call_args.kwargs["timeout_s"] == 1234
