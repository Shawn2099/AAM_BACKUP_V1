"""Tests for WS-B B4: copy-only on G3-only block (V1, V2, V5) on LAN and cloud."""
from __future__ import annotations

from core import cloud_sync, guards


def test_b4_is_g3_only_block_distinguishes_g3_from_other_guards():
    g3_only = {
        "blocked": True,
        "approved_guards": [],
        "results": [
            {"guard": "G1_SOURCE_HAS_FILES", "passed": True},
            {"guard": "G3_MASS_DELETE", "passed": False, "metrics": {"planned_deletes": 25}},
            {"guard": "G5_CANARY", "passed": True},
        ],
    }
    assert guards.is_g3_only_block(g3_only) is True

    g3_plus_g5 = {
        "blocked": True,
        "approved_guards": [],
        "results": [
            {"guard": "G3_MASS_DELETE", "passed": False},
            {"guard": "G5_CANARY", "passed": False},
        ],
    }
    assert guards.is_g3_only_block(g3_plus_g5) is False

    g1_only = {
        "blocked": True,
        "approved_guards": [],
        "results": [{"guard": "G1_SOURCE_HAS_FILES", "passed": False}],
    }
    assert guards.is_g3_only_block(g1_only) is False

    not_blocked = {"blocked": False, "approved_guards": ["G3_MASS_DELETE"], "results": [
        {"guard": "G3_MASS_DELETE", "passed": False}
    ]}
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
