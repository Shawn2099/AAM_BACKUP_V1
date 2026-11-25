"""Tests for WS-B B3: --backup-dir and mode='sync'/'copy' in core.lan_sync."""
from __future__ import annotations

import pytest

from core import cloud_sync, lan_sync
from models.config import LanConfig


def test_b3_build_lan_rclone_command_copy_mode_and_backup_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")
    dst = r"\\NAS\backup\FY26-27"
    bdir = r"\\NAS\backup\AAM_versions\2026-11-25_2101_FY26-27"
    cfg = LanConfig()

    cmd_sync = lan_sync.build_lan_rclone_command(
        cfg, str(src), dst, str(tmp_path / "s.log"),
        max_delete=10, mode="sync", backup_dir=bdir,
    )
    assert cmd_sync[1] == "sync"
    assert "--backup-dir" in cmd_sync and cmd_sync[cmd_sync.index("--backup-dir") + 1] == bdir
    assert "--max-delete" in cmd_sync

    cmd_copy = lan_sync.build_lan_rclone_command(
        cfg, str(src), dst, str(tmp_path / "c.log"),
        max_delete=10, mode="copy", backup_dir=bdir,
    )
    assert cmd_copy[1] == "copy"
    assert "--backup-dir" in cmd_copy and cmd_copy[cmd_copy.index("--backup-dir") + 1] == bdir
    assert "--max-delete" not in cmd_copy

    cmd_dry = lan_sync.build_lan_rclone_command(
        cfg, str(src), dst, str(tmp_path / "d.log"),
        dry_run=True, combined_path=str(tmp_path / "comb.txt"), backup_dir=bdir,
    )
    assert cmd_dry[1] == "sync" and "--dry-run" in cmd_dry
    assert "--backup-dir" in cmd_dry and cmd_dry[cmd_dry.index("--backup-dir") + 1] == bdir

    with pytest.raises(ValueError):
        lan_sync.build_lan_rclone_command(cfg, str(src), dst, str(tmp_path / "x.log"), mode="move")


def test_b3_backup_dir_for_run_distinct_per_run_and_fy():
    dst = r"\\NAS\backup\FY26-27"
    b1 = lan_sync.backup_dir_for_run(dst, "AAM_versions", "FY26-27", "2026-11-25T21:01:00+05:30", run_tag="a1")
    b2 = lan_sync.backup_dir_for_run(dst, "AAM_versions", "FY26-27", "2026-11-25T21:01:00+05:30", run_tag="a2")
    b3 = lan_sync.backup_dir_for_run(dst, "AAM_versions", "FY27-28", "2026-11-25T21:01:00+05:30", run_tag="a1")
    assert b1 != b2
    assert b1 != b3
    assert b1.startswith(r"\\NAS\backup\AAM_versions\2026-11-25_2101_FY26-27")
    assert b3.startswith(r"\\NAS\backup\AAM_versions\2026-11-25_2101_FY27-28")


def test_b3_planned_versions_bytes_sums_replaced_and_deleted():
    log = "\n".join([
        '{"level":"notice","skipped":"move","object":"change.txt","size":120}',
        '{"level":"notice","skipped":"move into backup dir","object":"gone.txt","size":300}',
        '{"level":"notice","skipped":"copy","object":"change.txt","size":150}',
        '{"level":"notice","skipped":"move to new.txt","object":"old.txt","size":999}',
    ])
    assert cloud_sync.planned_versions_bytes(log) == 420
