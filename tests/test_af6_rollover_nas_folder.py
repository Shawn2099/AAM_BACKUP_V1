"""AF-6: create_nas_folder must not auto-mark an existing non-empty unmarked NAS folder."""
from __future__ import annotations

import pytest

from core import fy_rollover
from core.fy_rollover import MOUNT_MARKER


def test_af6_create_nas_folder_refuses_existing_non_empty_unmarked_folder(tmp_path):
    lan_root = tmp_path / "nas_share"
    lan_root.mkdir()
    existing = lan_root / "FY27-28"
    existing.mkdir()
    (existing / "unexpected_data.txt").write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(OSError, match="non-empty|approval|marker|AAM_TARGET_MOUNTED"):
        fy_rollover.create_nas_folder(str(lan_root), "FY27-28")

    assert not (existing / MOUNT_MARKER).exists()


def test_af6_create_nas_folder_marks_empty_and_new_and_accepts_already_marked(tmp_path):
    lan_root = tmp_path / "nas_share"
    lan_root.mkdir()

    # 1. Brand new folder -> created & marked
    p1 = fy_rollover.create_nas_folder(str(lan_root), "FY27-28")
    assert (p1 / MOUNT_MARKER).is_file()

    # 2. Already marked (even with files) -> ok
    (p1 / "backup.txt").write_text("ok", encoding="utf-8")
    p2 = fy_rollover.create_nas_folder(str(lan_root), "FY27-28")
    assert p2 == p1 and (p2 / MOUNT_MARKER).is_file()

    # 3. Existing empty unmarked -> marked
    empty_dir = lan_root / "FY28-29"
    empty_dir.mkdir()
    p3 = fy_rollover.create_nas_folder(str(lan_root), "FY28-29")
    assert (p3 / MOUNT_MARKER).is_file()
