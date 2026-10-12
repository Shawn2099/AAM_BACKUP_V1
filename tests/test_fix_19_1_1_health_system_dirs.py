# tests/test_fix_19_1_1_health_system_dirs.py
from core.health import check_source_drive


def test_drive_with_only_system_dirs_fails(tmp_path):
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / "$RECYCLE.BIN").mkdir()
    (tmp_path / "desktop.ini").write_text("[.ShellClassInfo]")
    (tmp_path / "Thumbs.db").write_bytes(b"\x00" * 10)

    ok, reason = check_source_drive(str(tmp_path), min_free_gb=0)
    assert not ok
    assert "empty" in reason.lower()


def test_drive_with_user_file_passes(tmp_path):
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / "Accounts").mkdir()
    (tmp_path / "Accounts" / "ledger.xlsx").write_bytes(b"data")

    ok, reason = check_source_drive(str(tmp_path), min_free_gb=0)
    assert ok, f"Expected pass but got: {reason}"


def test_completely_empty_fails(tmp_path):
    ok, reason = check_source_drive(str(tmp_path), min_free_gb=0)
    assert not ok
    assert "empty" in reason.lower()


def test_legacy_recycler_dirs_fail(tmp_path):
    (tmp_path / "Recycler").mkdir()
    (tmp_path / "Recycled").mkdir()

    ok, reason = check_source_drive(str(tmp_path), min_free_gb=0)
    assert not ok
    assert "empty" in reason.lower()
