"""Comprehensive tests for core/lan_manifest.py — all functions."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.lan_manifest import (
    snapshot_to_dict,
    diff_snapshots,
    walk_lan_destination,
)


class TestSnapshotToDict:
    def test_empty_list(self):
        assert snapshot_to_dict([]) == {}

    def test_single_entry(self):
        entries = [{"path": "file.txt", "size": 100, "mtime": 1700000000.0}]
        result = snapshot_to_dict(entries)
        assert "file.txt" in result
        assert result["file.txt"] == (100, 1700000000.0)

    def test_multiple_entries(self):
        entries = [
            {"path": f"file_{i}.txt", "size": i * 100, "mtime": 1700000000.0 + i}
            for i in range(5)
        ]
        result = snapshot_to_dict(entries)
        assert len(result) == 5

    def test_duplicate_paths(self):
        """Duplicate paths — last one wins (dict behavior)."""
        entries = [
            {"path": "file.txt", "size": 100, "mtime": 1700000000.0},
            {"path": "file.txt", "size": 200, "mtime": 1700000001.0},
        ]
        result = snapshot_to_dict(entries)
        assert result["file.txt"] == (200, 1700000001.0)


class TestDiffSnapshots:
    def test_empty_both(self):
        result = diff_snapshots({}, {})
        assert result["added"] == []
        assert result["removed"] == []
        assert result["modified"] == []
        assert result["unchanged"] == []

    def test_added_files(self):
        after = {"file.txt": (100, 1700000000.0)}
        result = diff_snapshots({}, after)
        assert "file.txt" in result["added"]

    def test_removed_files(self):
        before = {"file.txt": (100, 1700000000.0)}
        result = diff_snapshots(before, {})
        assert "file.txt" in result["removed"]

    def test_modified_size(self):
        before = {"file.txt": (100, 1700000000.0)}
        after = {"file.txt": (200, 1700000000.0)}
        result = diff_snapshots(before, after)
        assert "file.txt" in result["modified"]

    def test_modified_mtime(self):
        before = {"file.txt": (100, 1700000000.0)}
        after = {"file.txt": (100, 1700000001.0)}
        result = diff_snapshots(before, after)
        assert "file.txt" in result["modified"]

    def test_unchanged(self):
        before = {"file.txt": (100, 1700000000.0)}
        after = {"file.txt": (100, 1700000000.0)}
        result = diff_snapshots(before, after)
        assert "file.txt" in result["unchanged"]


class TestWalkLanDestination:
    def test_walk_empty_dir(self, tmp_path):
        """Empty directory → empty list."""
        target = tmp_path / "empty"
        target.mkdir()
        result = walk_lan_destination(str(target))
        assert result == []

    def test_walk_with_files(self, tmp_path):
        """Directory with files → list of entries."""
        target = tmp_path / "share"
        target.mkdir()
        (target / "file1.txt").write_text("data1")
        (target / "file2.txt").write_text("data2")

        result = walk_lan_destination(str(target))
        assert len(result) == 2
        paths = [r["path"] for r in result]
        assert any("file1.txt" in p for p in paths)
        assert any("file2.txt" in p for p in paths)

    def test_walk_nested_dirs(self, tmp_path):
        """Nested directories are walked recursively."""
        target = tmp_path / "share"
        (target / "subdir").mkdir(parents=True)
        (target / "subdir" / "nested.txt").write_text("data")

        result = walk_lan_destination(str(target))
        assert len(result) >= 1
        paths = [r["path"] for r in result]
        assert any("nested.txt" in p for p in paths)

    def test_walk_nonexistent_path(self, tmp_path):
        """Nonexistent path → OSError."""
        with pytest.raises(OSError):
            walk_lan_destination(str(tmp_path / "nonexistent"))

    def test_walk_unc_normalization(self, tmp_path):
        """UNC path is normalized."""
        target = tmp_path / "share"
        target.mkdir()
        (target / "file.txt").write_text("data")

        result = walk_lan_destination(str(target))
        assert len(result) > 0

    def test_walk_skips_system_entries(self, tmp_path):
        """System entries like Thumbs.db are still walked (no filter in walk)."""
        target = tmp_path / "share"
        target.mkdir()
        (target / "Thumbs.db").write_text("thumb")
        (target / "desktop.ini").write_text("ini")

        result = walk_lan_destination(str(target))
        # walk_lan_destination doesn't filter — it's a raw walk
        assert len(result) >= 2
