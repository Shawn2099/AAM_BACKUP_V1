"""Comprehensive tests for core/lan_manifest.py — walk destination, diff snapshots, path normalization."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from core.lan_manifest import walk_lan_destination, snapshot_to_dict, diff_snapshots


# ═══════════════════════════════════════════════════════════════
# 1. walk_lan_destination
# ═══════════════════════════════════════════════════════════════

class TestWalkLanDestination:
    """Walk LAN share recursively."""

    def test_returns_file_list(self, tmp_path):
        (tmp_path / "file1.txt").write_text("hello")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "file2.txt").write_text("world")

        result = walk_lan_destination(str(tmp_path))

        assert isinstance(result, list)
        assert len(result) == 2
        paths = {f["path"] for f in result}
        assert "file1.txt" in paths
        assert os.path.join("subdir", "file2.txt") in paths or "subdir/file2.txt" in paths

    def test_file_structure(self, tmp_path):
        (tmp_path / "test.txt").write_text("data")

        result = walk_lan_destination(str(tmp_path))

        assert len(result) == 1
        file_entry = result[0]
        assert "path" in file_entry
        assert "size" in file_entry
        assert "mtime" in file_entry

    def test_empty_directory(self, tmp_path):
        result = walk_lan_destination(str(tmp_path))

        assert result == []

    def test_oserror_on_file_skipped(self, tmp_path, monkeypatch):
        (tmp_path / "good.txt").write_text("ok")
        (tmp_path / "bad.txt").write_text("bad")

        original_stat = os.stat

        def mock_stat(path, *args, **kwargs):
            if "bad.txt" in str(path):
                raise OSError("access denied")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", mock_stat)

        result = walk_lan_destination(str(tmp_path))

        assert len(result) == 1
        assert result[0]["path"] == "good.txt"

    def test_size_populated(self, tmp_path):
        (tmp_path / "data.bin").write_bytes(b"x" * 1024)

        result = walk_lan_destination(str(tmp_path))

        assert result[0]["size"] == 1024

    def test_mtime_populated(self, tmp_path):
        (tmp_path / "data.bin").write_bytes(b"y")

        result = walk_lan_destination(str(tmp_path))

        assert isinstance(result[0]["mtime"], float)

    def test_nested_directories(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b").mkdir()
        (tmp_path / "a" / "b" / "c.txt").write_text("deep")

        result = walk_lan_destination(str(tmp_path))

        assert len(result) == 1
        assert "c.txt" in result[0]["path"]


# ═══════════════════════════════════════════════════════════════
# 2. snapshot_to_dict
# ═══════════════════════════════════════════════════════════════

class TestSnapshotToDict:
    """Convert walk result to dict."""

    def test_conversion(self):
        files = [
            {"path": "a.txt", "size": 100, "mtime": 1.0},
            {"path": "b.txt", "size": 200, "mtime": 2.0},
        ]
        result = snapshot_to_dict(files)

        assert result == {
            "a.txt": (100, 1.0),
            "b.txt": (200, 2.0),
        }

    def test_empty_list(self):
        result = snapshot_to_dict([])
        assert result == {}

    def test_single_file(self):
        files = [{"path": "x.txt", "size": 50, "mtime": 3.0}]
        result = snapshot_to_dict(files)
        assert result == {"x.txt": (50, 3.0)}

    def test_overwrites_duplicate_paths(self):
        files = [
            {"path": "x.txt", "size": 10, "mtime": 1.0},
            {"path": "x.txt", "size": 20, "mtime": 2.0},
        ]
        result = snapshot_to_dict(files)
        assert result["x.txt"] == (20, 2.0)


# ═══════════════════════════════════════════════════════════════
# 3. diff_snapshots
# ═══════════════════════════════════════════════════════════════

class TestDiffSnapshots:
    """Compare two snapshots."""

    def test_new_files_detected(self):
        before = {}
        after = {"new.txt": (100, 1.0)}

        diff = diff_snapshots(before, after)

        assert diff["added"] == ["new.txt"]
        assert diff["removed"] == []
        assert diff["modified"] == []

    def test_deleted_files_detected(self):
        before = {"gone.txt": (100, 1.0)}
        after = {}

        diff = diff_snapshots(before, after)

        assert diff["removed"] == ["gone.txt"]
        assert diff["added"] == []

    def test_modified_files_detected(self):
        before = {"file.txt": (100, 1.0)}
        after = {"file.txt": (200, 2.0)}

        diff = diff_snapshots(before, after)

        assert diff["modified"] == ["file.txt"]
        assert diff["added"] == []
        assert diff["removed"] == []

    def test_unchanged_files_empty_diff(self):
        before = {"file.txt": (100, 1.0)}
        after = {"file.txt": (100, 1.0)}

        diff = diff_snapshots(before, after)

        assert diff["unchanged"] == ["file.txt"]
        assert diff["added"] == []
        assert diff["removed"] == []
        assert diff["modified"] == []

    def test_mixed_changes(self):
        before = {
            "keep.txt": (100, 1.0),
            "modify.txt": (50, 5.0),
            "delete.txt": (200, 3.0),
        }
        after = {
            "keep.txt": (100, 1.0),
            "modify.txt": (999, 9.0),
            "add.txt": (300, 7.0),
        }

        diff = diff_snapshots(before, after)

        assert sorted(diff["added"]) == ["add.txt"]
        assert sorted(diff["removed"]) == ["delete.txt"]
        assert sorted(diff["modified"]) == ["modify.txt"]
        assert sorted(diff["unchanged"]) == ["keep.txt"]

    def test_results_are_sorted(self):
        before = {"z.txt": (1, 1.0), "a.txt": (2, 2.0), "m.txt": (3, 3.0)}
        after = {"z.txt": (1, 1.0), "a.txt": (2, 2.0), "n.txt": (4, 4.0)}

        diff = diff_snapshots(before, after)

        assert diff["removed"] == sorted(diff["removed"])
        assert diff["added"] == sorted(diff["added"])

    def test_empty_both(self):
        diff = diff_snapshots({}, {})

        assert diff["added"] == []
        assert diff["removed"] == []
        assert diff["modified"] == []
        assert diff["unchanged"] == []

    def test_all_new(self):
        before = {}
        after = {"a.txt": (1, 1.0), "b.txt": (2, 2.0)}

        diff = diff_snapshots(before, after)

        assert sorted(diff["added"]) == ["a.txt", "b.txt"]
        assert diff["removed"] == []

    def test_all_removed(self):
        before = {"a.txt": (1, 1.0), "b.txt": (2, 2.0)}
        after = {}

        diff = diff_snapshots(before, after)

        assert sorted(diff["removed"]) == ["a.txt", "b.txt"]
        assert diff["added"] == []

    def test_size_changed_only(self):
        """Size changed → modified."""
        before = {"f.txt": (100, 1.0)}
        after = {"f.txt": (101, 1.0)}

        diff = diff_snapshots(before, after)

        assert diff["modified"] == ["f.txt"]

    def test_mtime_changed_only(self):
        """Mtime changed → modified."""
        before = {"f.txt": (100, 1.0)}
        after = {"f.txt": (100, 2.0)}

        diff = diff_snapshots(before, after)

        assert diff["modified"] == ["f.txt"]


# ═══════════════════════════════════════════════════════════════
# 4. Path normalization
# ═══════════════════════════════════════════════════════════════

class TestPathNormalization:
    """OS separator paths should normalize consistently."""

    def test_forward_slash_paths(self):
        before = {"dir/file.txt": (100, 1.0)}
        after = {"dir/file.txt": (100, 1.0)}

        diff = diff_snapshots(before, after)

        assert diff["unchanged"] == ["dir/file.txt"]

    def test_mixed_separators_not_equal(self):
        """Different separators are different paths (no auto-normalization in diff)."""
        before = {"dir\\file.txt": (100, 1.0)}
        after = {"dir/file.txt": (100, 1.0)}

        diff = diff_snapshots(before, after)

        assert diff["removed"] == ["dir\\file.txt"]
        assert diff["added"] == ["dir/file.txt"]

    def test_walk_normalizes_via_relpath(self, tmp_path):
        """walk_lan_destination uses os.path.relpath, so paths are OS-native."""
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "file.txt").write_text("data")

        result = walk_lan_destination(str(tmp_path))

        assert len(result) == 1
        # Path should be relative, not absolute
        assert not os.path.isabs(result[0]["path"])
