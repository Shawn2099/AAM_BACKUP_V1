"""Tests for lan_manifest — filesystem walk, snapshot, and diff logic."""

import os
import tempfile
from pathlib import Path

from core.lan_manifest import diff_snapshots, snapshot_to_dict, walk_lan_destination


class TestWalkLanDestination:
    def test_walk_returns_file_list(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world")
        files = walk_lan_destination(str(tmp_path))
        paths = sorted(f["path"] for f in files)
        assert paths == ["a.txt", "b.txt"]

    def test_walk_includes_size_and_mtime(self, tmp_path):
        (tmp_path / "f.txt").write_text("data")
        files = walk_lan_destination(str(tmp_path))
        assert len(files) == 1
        assert files[0]["size"] == 4
        assert files[0]["mtime"] > 0

    def test_walk_recursive(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.txt").write_text("nested")
        files = walk_lan_destination(str(tmp_path))
        assert len(files) == 1
        assert "sub" in files[0]["path"]

    def test_walk_empty_directory(self, tmp_path):
        files = walk_lan_destination(str(tmp_path))
        assert files == []

    def test_walk_skips_locked_files(self, tmp_path):
        (tmp_path / "ok.txt").write_text("fine")
        files = walk_lan_destination(str(tmp_path))
        assert len(files) == 1


class TestSnapshotToDict:
    def test_converts_to_path_tuple_map(self):
        files = [
            {"path": "a.txt", "size": 100, "mtime": 1.0},
            {"path": "b.txt", "size": 200, "mtime": 2.0},
        ]
        result = snapshot_to_dict(files)
        assert result == {"a.txt": (100, 1.0), "b.txt": (200, 2.0)}

    def test_empty_list(self):
        assert snapshot_to_dict([]) == {}


class TestDiffSnapshots:
    def test_added_files(self):
        before = {}
        after = {"new.txt": (100, 1.0)}
        diff = diff_snapshots(before, after)
        assert diff["added"] == ["new.txt"]
        assert diff["removed"] == []
        assert diff["modified"] == []
        assert diff["unchanged"] == []

    def test_removed_files(self):
        before = {"old.txt": (100, 1.0)}
        after = {}
        diff = diff_snapshots(before, after)
        assert diff["removed"] == ["old.txt"]
        assert diff["added"] == []

    def test_modified_files(self):
        before = {"f.txt": (100, 1.0)}
        after = {"f.txt": (999, 2.0)}
        diff = diff_snapshots(before, after)
        assert diff["modified"] == ["f.txt"]
        assert diff["unchanged"] == []

    def test_unchanged_files(self):
        snapshot = {"f.txt": (100, 1.0)}
        diff = diff_snapshots(snapshot, snapshot)
        assert diff["unchanged"] == ["f.txt"]
        assert diff["added"] == []
        assert diff["removed"] == []
        assert diff["modified"] == []

    def test_mixed_changes(self):
        before = {"keep.txt": (1, 1.0), "modify.txt": (2, 2.0), "remove.txt": (3, 3.0)}
        after = {"keep.txt": (1, 1.0), "modify.txt": (99, 99.0), "add.txt": (4, 4.0)}
        diff = diff_snapshots(before, after)
        assert diff["added"] == ["add.txt"]
        assert diff["removed"] == ["remove.txt"]
        assert diff["modified"] == ["modify.txt"]
        assert diff["unchanged"] == ["keep.txt"]

    def test_empty_snapshots(self):
        diff = diff_snapshots({}, {})
        assert diff == {"added": [], "removed": [], "modified": [], "unchanged": []}

    def test_results_are_sorted(self):
        before = {"c.txt": (1, 1.0), "a.txt": (2, 2.0), "b.txt": (3, 3.0)}
        after = {"c.txt": (1, 1.0), "a.txt": (2, 2.0), "b.txt": (3, 3.0)}
        diff = diff_snapshots(before, after)
        assert diff["unchanged"] == ["a.txt", "b.txt", "c.txt"]
