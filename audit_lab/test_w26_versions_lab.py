"""WAVE 26 - Phase 1b B8: real-rclone --backup-dir & copy-only lab (V12, V13)."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from core.lan_sync import backup_dir_for_run, plan_lan_mirror, run_lan_mirror
from models.config import LanConfig

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="rclone LAN lab is Windows-only")


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def test_v12_real_rclone_backup_dir_saves_deletes_and_replaces_to_unc(sandbox, unc_nas):
    src = sandbox / "src" / "FY26-27"
    src.mkdir(parents=True, exist_ok=True)
    dst_local, dst_unc_p = unc_nas
    dst_unc = str(dst_unc_p).replace("/", "\\")

    (src / "keep.txt").write_bytes(b"keep-v1")
    (src / "sub").mkdir()
    (src / "sub" / "replace.txt").write_bytes(b"old-content-123")
    (src / "sub" / "gone.txt").write_bytes(b"deleted-content-456")

    old_replace_md5 = _md5(src / "sub" / "replace.txt")
    old_gone_md5 = _md5(src / "sub" / "gone.txt")

    cfg = LanConfig(retry_count=1, retry_wait_seconds=1)
    art1 = sandbox / "runs" / "r1"
    art1.mkdir(parents=True)
    r1 = run_lan_mirror(cfg, str(src), dst_unc, str(art1), max_delete=10)
    assert r1["status"] == "LAN_COMPLETE"

    # Modify replace.txt and delete gone.txt
    (src / "sub" / "replace.txt").write_bytes(b"new-content-longer-7890")
    (src / "sub" / "gone.txt").unlink()

    bdir = backup_dir_for_run(dst_unc, "AAM_versions", "FY26-27", "2026-11-25T21:01:00+05:30", run_tag="r2")
    art2 = sandbox / "runs" / "r2"
    art2.mkdir(parents=True)
    plan = plan_lan_mirror(cfg, str(src), dst_unc, str(art2), backup_dir=bdir)
    assert plan["ok"] is True
    assert plan["planned_deletes"] == 1

    r2 = run_lan_mirror(cfg, str(src), dst_unc, str(art2), max_delete=10, mode="sync", backup_dir=bdir)
    assert r2["status"] == "LAN_COMPLETE"

    v_local = dst_local.parent / "AAM_versions" / "2026-11-25_2101_FY26-27_r2"
    assert _md5(v_local / "sub" / "replace.txt") == old_replace_md5
    assert _md5(v_local / "sub" / "gone.txt") == old_gone_md5
    assert (dst_local / "sub" / "replace.txt").read_bytes() == b"new-content-longer-7890"
    assert not (dst_local / "sub" / "gone.txt").exists()


def test_v12_copy_only_mode_preserves_deletes_and_saves_replaced(sandbox, unc_nas):
    src = sandbox / "src2" / "FY26-27"
    src.mkdir(parents=True, exist_ok=True)
    dst_local, dst_unc_p = unc_nas
    dst_unc = str(dst_unc_p).replace("/", "\\")

    (src / "stay.txt").write_bytes(b"stay")
    (src / "del.txt").write_bytes(b"still-here")
    cfg = LanConfig(retry_count=1, retry_wait_seconds=1)
    art1 = sandbox / "runs2" / "r1"
    art1.mkdir(parents=True)
    assert run_lan_mirror(cfg, str(src), dst_unc, str(art1), max_delete=10)["status"] == "LAN_COMPLETE"

    (src / "del.txt").unlink()
    (src / "new.txt").write_bytes(b"arrived")
    bdir = backup_dir_for_run(dst_unc, "AAM_versions", "FY26-27", "2026-11-25T21:02:00+05:30", run_tag="c1")
    art2 = sandbox / "runs2" / "r2"
    art2.mkdir(parents=True)
    r2 = run_lan_mirror(cfg, str(src), dst_unc, str(art2), max_delete=0, mode="copy", backup_dir=bdir)
    assert r2["status"] == "LAN_COMPLETE"
    assert (dst_local / "new.txt").read_bytes() == b"arrived"
    assert (dst_local / "del.txt").read_bytes() == b"still-here"
