# tests/test_regression_backup_repository.py
from core.backup_repository import record_sync_results


def test_normal_cloud_upsert(tmp_db):
    entries = [{"path": "file1.txt", "size": 100, "mtime": 1.0}]
    record_sync_results(tmp_db, "cloud", entries)
    assert tmp_db.file_count("cloud_status") == 1


def test_normal_lan_upsert(tmp_db):
    entries = [{"path": "file1.txt", "size": 100, "mtime": 1.0}]
    record_sync_results(tmp_db, "lan", entries)
    assert tmp_db.file_count("lan_status") == 1


def test_prune_stale_synced_isolation(tmp_db):
    entries = [{"path": f"f{i}.txt", "size": 10, "mtime": 1.0} for i in range(5)]
    record_sync_results(tmp_db, "cloud", entries)
    record_sync_results(tmp_db, "lan", entries)

    # Cloud sync next day with only 3 files
    active_cloud = {f"f{i}.txt" for i in range(3)}
    pruned = tmp_db.prune_stale_synced("cloud", active_cloud)
    assert pruned == 2
    assert tmp_db.file_count("cloud_status") == 3
    assert tmp_db.file_count("lan_status") == 5


def test_full_cycle_cross_mode_delete_regression(tmp_db):
    entries = [{"path": "shared/data.csv", "size": 500, "mtime": 1.0}]
    record_sync_results(tmp_db, "lan", entries)
    record_sync_results(tmp_db, "cloud", entries)

    assert tmp_db.get_entry("shared/data.csv")["lan_status"] == "synced"
    assert tmp_db.get_entry("shared/data.csv")["cloud_status"] == "synced"

    # Remove from LAN
    record_sync_results(tmp_db, "lan", [], removed=["shared/data.csv"])
    entry = tmp_db.get_entry("shared/data.csv")
    assert entry is not None, "Must not be deleted while cloud still has it"
    assert entry["lan_status"] is None
    assert entry["cloud_status"] == "synced"

    # Remove from Cloud as well
    record_sync_results(tmp_db, "cloud", [], removed=["shared/data.csv"])
    assert tmp_db.get_entry("shared/data.csv") is None
