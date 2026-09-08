# tests/test_fix_22_2_1_snapshot_before.py
from unittest.mock import patch

from core.lan_manifest import diff_snapshots
from flow import lan_snapshot_before_task


def test_snapshot_before_returns_empty_dict_on_smb_error():
    mock_config = type("C", (), {"paths": type("P", (), {"lan_destination": r"\\NAS\share"})()})()
    with patch("flow.walk_lan_destination", side_effect=OSError("SMB timeout")):
        result = lan_snapshot_before_task.fn(mock_config)
    assert result == {}, "Must return empty dict on SMB failure, not raise"


def test_diff_snapshots_handles_empty_before_cleanly():
    """Verify that returning {} does not crash downstream diff calculations."""
    after_dict = {"sub/a.txt": (1024, 100.0), "b.txt": (2048, 200.0)}
    diff = diff_snapshots({}, after_dict)
    assert diff["added"] == ["b.txt", "sub/a.txt"]
    assert diff["removed"] == []
    assert diff["modified"] == []
