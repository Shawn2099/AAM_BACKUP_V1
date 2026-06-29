import msvcrt
import os
import time
from pathlib import Path

import pytest

from core.lan_manifest import diff_snapshots, snapshot_to_dict, walk_lan_destination
from core.lan_preflight import HealthError as LanHealthError
from core.lan_preflight import run_lan_dry_run
from core.lan_sync import run_lan_sync
from core.wol import ensure_server_online
from tests.e2e_helpers import (
    assert_log_contains,
    cfg,
    clean_test_dirs,
    make_file,
    nas_test_dir,
    source_test_dir,
)


@pytest.fixture(scope="module", autouse=True)
def setup_teardown_lan():
    """Wake NAS, setup dirs, and clean up after all LAN tests."""
    config = cfg()
    if config.wol.enabled:
        ensure_server_online(config)

    clean_test_dirs()
    source_test_dir().mkdir(parents=True, exist_ok=True)
    nas_test_dir().mkdir(parents=True, exist_ok=True)
    (nas_test_dir() / ".AAM_TARGET_MOUNTED").touch()

    yield

    clean_test_dirs()


@pytest.fixture(autouse=True)
def ensure_canary():
    """Ensure canary file exists before each test."""
    nas_test_dir().mkdir(parents=True, exist_ok=True)
    (nas_test_dir() / ".AAM_TARGET_MOUNTED").touch()


def test_lan_01_golden_path_new_files(capture_logs):
    """LAN-01: Golden Path — New Files Arrive on NAS."""
    source = source_test_dir()
    dest = nas_test_dir()

    # Create 3 files
    make_file(source / "small.txt", 1024)
    make_file(source / "medium.bin", 2 * 1024 * 1024)
    make_file(source / "nested" / "file.txt", 512)

    config = cfg()

    # Preflight
    dry_run = run_lan_dry_run(str(source), str(dest))
    assert dry_run["ok"] is True

    # Sync
    sync = run_lan_sync(str(source), str(dest), config.lan)
    assert sync["status"] == "LAN_COMPLETE"
    assert sync["exit_code"] in (1, 3)  # Robocopy exit codes for successful copy

    # Assertions
    assert (dest / "small.txt").exists()
    assert (dest / "medium.bin").exists()
    assert (dest / "nested" / "file.txt").exists()

    assert os.path.getsize(dest / "small.txt") == 1024
    assert os.path.getsize(dest / "medium.bin") == 2 * 1024 * 1024
    assert os.path.getsize(dest / "nested" / "file.txt") == 512

    assert_log_contains(capture_logs, "LAN sync exit")


def test_lan_02_mirror_delete():
    """LAN-02: Mirror Delete — Files Removed on Source Are Deleted on NAS."""
    source = source_test_dir()
    dest = nas_test_dir()

    # Ensure LAN-01 state
    assert (dest / "small.txt").exists()

    # Delete one file
    (source / "small.txt").unlink()

    config = cfg()
    sync = run_lan_sync(str(source), str(dest), config.lan)

    assert sync["status"] in ("LAN_COMPLETE", "LAN_PARTIAL")
    assert not (dest / "small.txt").exists()
    assert (dest / "medium.bin").exists()


def test_lan_03_canary_missing_abort(capture_logs):
    """LAN-03: Canary Missing → Hard Abort Before Any Transfer."""
    source = source_test_dir()
    dest = nas_test_dir()

    # Delete canary
    canary = dest / ".AAM_TARGET_MOUNTED"
    if canary.exists():
        canary.unlink()

    with pytest.raises(LanHealthError) as exc_info:
        run_lan_dry_run(str(source), str(dest))

    assert str(dest) in str(exc_info.value)
    assert "Canary file" in str(exc_info.value)

    assert_log_contains(capture_logs, "Canary file")


def test_lan_04_os_locked_file_robocopy_survives(capture_logs):
    """LAN-04: OS-Level Locked File — Robocopy Survives."""
    source = source_test_dir()
    dest = nas_test_dir()
    config = cfg()

    locked_file = source / "locked_doc.txt"
    make_file(locked_file, 1024)

    fd = os.open(str(locked_file), os.O_RDWR)
    try:
        # Place exclusive OS lock
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1024)

        sync = run_lan_sync(str(source), str(dest), config.lan)

        # Robocopy exit code 8 indicates some files failed to copy
        # Our classify_exit_code should map it to LAN_PARTIAL or LAN_FAILED
        assert sync["status"] in ("LAN_PARTIAL", "LAN_FAILED")
        assert sync["exit_code"] >= 8
        assert sync["error"] is not None
        assert len(sync["error"]) > 0

        assert_log_contains(capture_logs, "exit")

    finally:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1024)
        os.close(fd)


def test_lan_05_large_file_no_corruption():
    """LAN-05: Large File Transfer — Verify No Data Corruption."""
    source = source_test_dir()
    dest = nas_test_dir()
    config = cfg()

    large_file = source / "large.bin"
    import hashlib
    
    # 50 MB
    large_file.parent.mkdir(parents=True, exist_ok=True)
    h_source = hashlib.sha256()
    with open(large_file, "wb") as f:
        for _ in range(50):
            chunk = os.urandom(1024 * 1024)
            f.write(chunk)
            h_source.update(chunk)
            
    sync = run_lan_sync(str(source), str(dest), config.lan)
    assert sync["status"] in ("LAN_COMPLETE", "LAN_PARTIAL")

    h_dest = hashlib.sha256()
    with open(dest / "large.bin", "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h_dest.update(chunk)
            
    assert h_source.hexdigest() == h_dest.hexdigest()


def test_lan_06_dest_not_reachable(capture_logs):
    """LAN-06: Destination Not Reachable — Graceful Error and Useful Log."""
    source = source_test_dir()
    dest = nas_test_dir()
    
    # Move canary to fake directory to simulate unreachable path but with a logic trap
    bad_dest = Path(r"\\10.10.186.231\NONEXISTENT_SHARE\E2E_TEST_DEST")

    with pytest.raises((LanHealthError, RuntimeError, FileNotFoundError)) as exc_info:
        run_lan_dry_run(str(source), str(bad_dest))

    assert str(bad_dest) in str(exc_info.value)
    assert_log_contains(capture_logs, str(bad_dest))


def test_lan_07_snapshot_diff_logic():
    """LAN-07: Snapshot Diff Logic — Before vs After."""
    source = source_test_dir()
    dest = nas_test_dir()
    config = cfg()

    # Initial sync
    run_lan_sync(str(source), str(dest), config.lan)
    
    before = snapshot_to_dict(walk_lan_destination(str(dest)))

    # Add 1 new file
    make_file(source / "diff_new.txt", 1024)
    # Modify 1 existing file (Wait to ensure mtime changes)
    time.sleep(2.1)
    make_file(source / "small.txt", 2048)

    run_lan_sync(str(source), str(dest), config.lan)

    after = snapshot_to_dict(walk_lan_destination(str(dest)))
    diff = diff_snapshots(before, after)

    added_paths = [Path(p).name for p in diff["added"]]
    modified_paths = [Path(p).name for p in diff["modified"]]
    
    assert "diff_new.txt" in added_paths
    assert "small.txt" in modified_paths or "small.txt" in [Path(p).name for p in diff["added"]] # Depending on if it sees it as modified or a new creation
    assert len(diff["removed"]) == 0
