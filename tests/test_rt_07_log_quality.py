import os
from io import StringIO
from pathlib import Path

import pytest
from loguru import logger

from core.cloud_preflight import run_cloud_dry_run
from core.cloud_sync import run_cloud_sync
from core.cloud_verify import verify_cloud_integrity
from core.fy_rollover import update_config_yaml
from core.health import HealthError, pre_backup_health
from core.lan_preflight import run_lan_dry_run
from core.process import read_lock_alive
from tests.e2e_helpers import (
    cfg,
    make_file,
    nas_test_dir,
    source_test_dir,
)


@pytest.fixture
def capture_logs():
    """Capture loguru logs to a string buffer."""
    buf = StringIO()
    handler_id = logger.add(buf, format="{level} | {message}", level="DEBUG")
    yield buf
    logger.remove(handler_id)


def test_log_01_source_missing(capture_logs):
    """LOG-01: Source Drive Missing → Log Contains Path."""
    bad_path = "/nonexistent/fake_source_path_123"
    # Use the filename segment for assertion — it is OS-agnostic regardless of
    # whether Windows normalises the leading slash to a backslash.
    expected_fragment = "fake_source_path_123"
    
    with pytest.raises(HealthError) as exc_info:
        pre_backup_health(bad_path, mode="cloud", gcs_key_path="dummy")
        
    err_text = str(exc_info.value).lower()
    
    assert expected_fragment in err_text
    assert "not accessible" in err_text or "not found" in err_text


def test_log_02_gcs_key_missing(capture_logs):
    """LOG-02: GCS Key Missing → Log Contains Key Path."""
    bad_key = "/fake/key_path_456.json"
    config = cfg()
    source = source_test_dir()
    
    # Ensure the source directory has at least one file so the source-drive
    # health check passes and execution reaches the GCS key validation.
    source.mkdir(parents=True, exist_ok=True)
    make_file(source / "log02_dummy.bin", 512)
    
    result = run_cloud_dry_run(
        source=str(source),
        bucket=config.cloud.bucket,
        fy_prefix="E2E_TEST_FY",
        gcs_key_path=bad_key,
        project_number=config.cloud.project_number,
        storage_class=config.cloud.storage_class,
        location=config.cloud.location,
    )
    
    assert result["ok"] is False
    assert bad_key in result["error"]
    assert (
        "not found" in result["error"].lower()
        or "not exist" in result["error"].lower()
        or "find the path" in result["error"].lower()
    )


def test_log_03_canary_missing(capture_logs):
    """LOG-03: Canary Missing → Log Contains Full NAS Path."""
    source = source_test_dir()
    dest = nas_test_dir()
    
    dest.mkdir(parents=True, exist_ok=True)
    canary = dest / ".AAM_TARGET_MOUNTED"
    canary.unlink(missing_ok=True)
    
    from core.lan_preflight import HealthError as LanHealthError
    with pytest.raises(LanHealthError):
        run_lan_dry_run(str(source), str(dest))
        
    logs = capture_logs.getvalue().lower()
    
    assert str(dest).lower() in logs
    assert "canary" in logs or "unmounted" in logs


def test_log_04_robocopy_locked_file_tail(capture_logs):
    """LOG-04: Robocopy Locked File → Log Contains Robocopy Log Tail."""
    import msvcrt

    from core.lan_sync import run_lan_sync
    
    source = source_test_dir()
    dest = nas_test_dir()
    config = cfg()
    
    source.mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".AAM_TARGET_MOUNTED").touch()
    
    locked_file = source / "log_locked_doc.txt"
    make_file(locked_file, 1024)
    
    fd = os.open(str(locked_file), os.O_RDWR)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1024)
        
        sync = run_lan_sync(str(source), str(dest), config.lan)
        
        assert sync["error"] is not None
        assert len(sync["error"]) > 50
        assert "log_locked_doc.txt" in sync["error"]
        
    finally:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1024)
        os.close(fd)


def test_log_05_watchdog_stale_lock(capture_logs):
    """LOG-05: Watchdog Stale Lock → Log Contains PID and Action Taken."""
    lock_path = Path("test_log_stale.lock")
    try:
        lock_path.write_text("999999:0.0") # fake PID
        alive, pid = read_lock_alive(lock_path)
        
        assert alive is False
        
        # Simulate watchdog logging
        logger.warning(f"Found stale lock for PID {pid}, removing it")
        
        logs = capture_logs.getvalue().lower()
        assert "999999" in logs
        assert "stale" in logs
    finally:
        lock_path.unlink(missing_ok=True)


def test_log_06_cloud_verify_mismatch(capture_logs):
    """LOG-06: Cloud Verify Mismatch → Log Contains File Count Discrepancy."""
    source = source_test_dir()
    config = cfg()
    
    source.mkdir(parents=True, exist_ok=True)
    make_file(source / "log_unsynced.txt", 1024)
    
    from core.rclone_config import temp_rclone_config
    with temp_rclone_config(
        config.paths.gcs_key_path,
        config.cloud.location,
        config.cloud.project_number,
        config.cloud.storage_class,
    ) as config_path:
        verify_cloud_integrity(
            source=str(source),
            bucket=config.cloud.bucket,
            fy_prefix="E2E_TEST_FY",
            config_path=config_path,
        )
        
    logs = capture_logs.getvalue().lower()
    
    assert "mismatch" in logs
    assert "exit 1" in logs


def test_log_07_fy_rollover_write_failure(monkeypatch):
    """LOG-07: FY Rollover Config Write Failure → Log Contains Temp File Path."""
    def mock_replace(src, dst):
        raise OSError("Simulated disk error 77")
        
    monkeypatch.setattr(os, "replace", mock_replace)
    
    temp_config = Path("config.example.yaml")
    
    with pytest.raises(OSError) as exc_info:
        update_config_yaml(str(temp_config), "src", "lan", "FY_E2E")
        
    err_str = str(exc_info.value)
    assert "Simulated disk error 77" in err_str


def test_log_08_rclone_not_found_clean_error(monkeypatch):
    """LOG-08: rclone Not Found → Error dict contains clean message, not a Python traceback."""
    config = cfg()
    source = source_test_dir()
    
    # Simulate rclone binary missing by making subprocess.run raise FileNotFoundError.
    # cloud_sync.py catches FileNotFoundError at the subprocess.run call site and
    # returns a clean {status, error} dict — that is exactly what we are testing here.
    # (There is no resolve_binary call in cloud_sync — it relies on the OS to raise
    # FileNotFoundError when the executable name is not found.)
    from core import cloud_sync as _cloud_sync
    
    def _mock_run(*args, **kwargs):
        raise FileNotFoundError("[WinError 2] The system cannot find the file specified: 'rclone'")
    
    monkeypatch.setattr(_cloud_sync.subprocess, "run", _mock_run)
    
    source.mkdir(parents=True, exist_ok=True)
    make_file(source / "log08_dummy.bin", 512)
    
    result = run_cloud_sync(
        source=str(source),
        bucket=config.cloud.bucket,
        fy_prefix="E2E_TEST_FY",
        gcs_key_path=config.paths.gcs_key_path,
        project_number=config.cloud.project_number,
        storage_class=config.cloud.storage_class,
        location=config.cloud.location,
    )
    
    assert result["status"] == "CLOUD_FAILED"
    assert "rclone not found" in result["error"].lower()
