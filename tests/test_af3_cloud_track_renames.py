"""Tests for AF-3 (option a): cloud.track_renames defaults to False."""
from __future__ import annotations

from pathlib import Path

import pytest

from core import cloud_sync, guards
from models.config import CloudConfig


def test_af3_default_cloud_config_disables_track_renames(tmp_path):
    cfg = CloudConfig(bucket="test-bucket", project_number="123456")
    assert cfg.track_renames is False

    src = tmp_path / "FY26-27"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")
    cmd = cloud_sync.build_rclone_sync_command(
        str(src), "test-bucket", "FY26-27", str(tmp_path / "rclone.conf"), "STANDARD",
    )
    assert not any(arg.startswith("--track-renames") for arg in cmd)


def test_af3_rename_with_default_config_counts_deletes_and_blocks_over_allowance(tmp_path):
    rclone_exe = Path("deploy/bin/rclone.exe")
    if not rclone_exe.is_file():
        pytest.skip("deploy/bin/rclone.exe not present")

    import subprocess

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    for i in range(4):
        (src / f"file_{i}.txt").write_text(f"payload-{i}", encoding="utf-8")

    # Initial sync to populate destination
    p = subprocess.run([str(rclone_exe), "sync", str(src), str(dst)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr

    # Rename all 4 files in source
    for i in range(4):
        (src / f"file_{i}.txt").rename(src / f"renamed_{i}.txt")

    # With default cloud.track_renames=False, measure_planned_deletes reports
    # 4 planned deletes and 0 moves (not 0 deletes / 4 moves).
    cfg = CloudConfig(bucket="test-bucket", project_number="123456")
    dry = cloud_sync.measure_planned_deletes(str(src), str(dst), track_renames=cfg.track_renames)
    assert dry["error"] is None
    assert dry["planned_deletes"] == 4
    assert dry["moves"] == 0

    # Allowance < 4 -> G3 blocks cleanly (CLOUD_BLOCKED, not CLOUD_FAILED)
    g3 = guards.g3_mass_delete(dry["planned_deletes"], dry["dest_files"], max_delete_files=2, max_delete_percent=10.0)
    with pytest.raises(guards.GuardBlocked):
        guards.enforce("cloud", [guards.g1_source_has_files("renamed_0.txt", str(src)), g3])
    # Destination untouched
    assert sorted(f.name for f in dst.iterdir()) == [f"file_{i}.txt" for i in range(4)]

    # Allowance >= 4 -> G3 passes and sync completes with 4 deletes
    g3_ok = guards.g3_mass_delete(dry["planned_deletes"], dry["dest_files"], max_delete_files=10, max_delete_percent=100.0)
    guards.enforce("cloud", [guards.g1_source_has_files("renamed_0.txt", str(src)), g3_ok])
    p2 = subprocess.run([str(rclone_exe), "sync", str(src), str(dst), "--max-delete", "10"], capture_output=True, text=True)
    assert p2.returncode == 0, p2.stderr
    assert sorted(f.name for f in dst.iterdir()) == [f"renamed_{i}.txt" for i in range(4)]
