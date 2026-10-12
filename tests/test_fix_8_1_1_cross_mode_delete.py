# tests/test_fix_8_1_1_cross_mode_delete.py
import pytest

from core.backup_repository import record_sync_results


class TestClearModeStatusPreservesPeerStatus:
    def test_lan_prune_preserves_cloud_status(self, populated_db):
        record_sync_results(populated_db, "lan", [], removed=["docs/client_report.pdf"])
        entry = populated_db.get_entry("docs/client_report.pdf")
        assert entry is not None, "Row must not be deleted while cloud still has it"
        assert entry["cloud_status"] == "synced"
        assert entry["lan_status"] is None

    def test_cloud_prune_preserves_lan_status(self, populated_db):
        record_sync_results(populated_db, "cloud", [], removed=["docs/client_report.pdf"])
        entry = populated_db.get_entry("docs/client_report.pdf")
        assert entry is not None, "Row must not be deleted while LAN still has it"
        assert entry["lan_status"] == "synced"
        assert entry["cloud_status"] is None

    def test_row_deleted_only_when_both_modes_cleared(self, populated_db):
        record_sync_results(populated_db, "lan", [], removed=["docs/client_report.pdf"])
        record_sync_results(populated_db, "cloud", [], removed=["docs/client_report.pdf"])
        assert populated_db.get_entry("docs/client_report.pdf") is None

    def test_clear_mode_status_empty_list_noop(self, populated_db):
        assert populated_db.clear_mode_status("lan", []) == 0
        assert populated_db.get_entry("docs/client_report.pdf")["lan_status"] == "synced"

    def test_clear_mode_status_invalid_mode_raises(self, tmp_db):
        with pytest.raises(ValueError, match="mode must be"):
            tmp_db.clear_mode_status("ftp", ["file.txt"])

    def test_chunking_handles_large_path_lists(self, tmp_db):
        paths = [f"file_{i}.txt" for i in range(750)]
        tmp_db.bulk_upsert_synced([{"path": p, "size": 100, "mtime": 0.0} for p in paths], mode="lan")
        tmp_db.bulk_upsert_synced([{"path": p, "size": 100, "mtime": 0.0} for p in paths], mode="cloud")
        cleared = tmp_db.clear_mode_status("lan", paths)
        assert cleared == 750
        assert tmp_db.file_count("cloud_status") == 750
        assert tmp_db.file_count("lan_status") == 0
