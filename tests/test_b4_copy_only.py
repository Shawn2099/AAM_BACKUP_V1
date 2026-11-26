"""Tests for WS-B B4: copy-only on G3-only block (V1, V2, V5) on LAN and cloud."""
from __future__ import annotations

import pytest

from core import cloud_sync, guards


def _enforce_record(results: list[guards.GuardResult],
                    approved: frozenset[str] = frozenset()) -> dict:
    try:
        return guards.enforce("cloud", results, approved_guards=approved)
    except guards.GuardBlocked as gb:
        return gb.record


def test_b4_is_g3_only_block_distinguishes_g3_from_other_guards():
    g1_ok = guards.g1_source_has_files("a.txt", "D:\\src")
    g1_bad = guards.g1_source_has_files(None, "D:\\src")
    g3_bad = guards.g3_mass_delete(25, 100, 5, 10.0)
    g5_ok = guards.g5_canary(True, True, "\\\\nas\\share")
    g5_bad = guards.g5_canary(False, True, "\\\\nas\\share")

    g3_only = _enforce_record([g1_ok, g3_bad, g5_ok])
    assert guards.is_g3_only_block(g3_only) is True

    g3_plus_g5 = _enforce_record([g3_bad, g5_bad])
    assert guards.is_g3_only_block(g3_plus_g5) is False

    g1_only = _enforce_record([g1_bad])
    assert guards.is_g3_only_block(g1_only) is False

    not_blocked = _enforce_record([g3_bad], approved=frozenset({"G3_MASS_DELETE"}))
    assert guards.is_g3_only_block(not_blocked) is False


def test_b4_cloud_build_rclone_command_supports_copy_mode(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")
    cmd = cloud_sync.build_rclone_sync_command(
        str(src), "bucket", "FY26-27", str(tmp_path / "rclone.conf"), "STANDARD",
        max_delete=5, mode="copy",
    )
    assert cmd[1] == "copy"
    assert "--max-delete" not in cmd
