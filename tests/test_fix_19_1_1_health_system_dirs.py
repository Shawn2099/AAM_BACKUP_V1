# tests/test_fix_19_1_1_health_system_dirs.py
# M2 2.2: "is the source empty?" moved from health.check_source_drive to
# guard G1 (BLOCKED for a human instead of FAILED). The layouts are kept:
# OS-managed entries must never count as backup content.
from core import guards
from core.health import check_source_drive


def _g1(path):
    return guards.g1_source_has_files(
        guards.first_regular_file(str(path), guards.SOURCE_IGNORE_NAMES), str(path))


def test_drive_with_only_system_dirs_is_blocked_by_g1(tmp_path):
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / "$RECYCLE.BIN").mkdir()
    (tmp_path / "desktop.ini").write_text("[.ShellClassInfo]")
    (tmp_path / "Thumbs.db").write_bytes(b"\x00" * 10)

    assert not _g1(tmp_path).passed
    # health no longer judges emptiness; it only checks the drive is usable
    assert check_source_drive(str(tmp_path), min_free_gb=0) == (True, "")


def test_drive_with_user_file_passes(tmp_path):
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / "Accounts").mkdir()
    (tmp_path / "Accounts" / "ledger.xlsx").write_bytes(b"data")

    ok, reason = check_source_drive(str(tmp_path), min_free_gb=0)
    assert ok, f"Expected pass but got: {reason}"
    assert _g1(tmp_path).passed


def test_completely_empty_is_blocked_by_g1(tmp_path):
    result = _g1(tmp_path)
    assert not result.passed
    assert "contains no files" in result.reason


def test_legacy_recycler_dirs_are_blocked_by_g1(tmp_path):
    (tmp_path / "Recycler").mkdir()
    (tmp_path / "Recycled").mkdir()

    assert not _g1(tmp_path).passed
