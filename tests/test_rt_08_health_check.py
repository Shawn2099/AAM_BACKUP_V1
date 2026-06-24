import os
import shutil
import time
import json
import pytest
from pathlib import Path

from core.health import (
    check_source_drive,
    check_binary_exists,
    check_gcs_key,
    check_clock_skew,
    pre_backup_health,
    HealthError,
)

from tests.e2e_helpers import (
    cfg,
    source_test_dir,
    make_file,
)


def test_hc_01_source_drive_exists():
    """HC-01: Source Drive Check — Drive Exists and Has Files."""
    config = cfg()
    source = source_test_dir()
    
    source.mkdir(parents=True, exist_ok=True)
    make_file(source / "health_test.txt", 1024)
    
    try:
        ok, reason = check_source_drive(str(source))
        assert ok is True
        assert reason == ""
    finally:
        shutil.rmtree(source, ignore_errors=True)


def test_hc_02_source_drive_empty_fails(tmp_path):
    """HC-02: Source Drive Check — Empty Drive Fails."""
    empty_dir = tmp_path / "empty_source"
    empty_dir.mkdir()
    
    ok, reason = check_source_drive(str(empty_dir))
    
    assert ok is False
    assert "empty" in reason.lower()


def test_hc_03_binary_check():
    """HC-03: Binary Check — Robocopy and Rclone Found."""
    assert check_binary_exists("robocopy") is True
    assert check_binary_exists("rclone") is True


def test_hc_04_gcs_key_check():
    """HC-04: GCS Key Check — Real Key Exists and Is Valid JSON."""
    config = cfg()
    key_path = config.paths.gcs_key_path
    
    ok, reason = check_gcs_key(key_path)
    
    assert ok is True
    assert reason == ""
    
    # Verify it's valid JSON
    with open(key_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert "type" in data
        assert data["type"] == "service_account"


def test_hc_05_clock_skew_check():
    """HC-05: Clock Skew Check — System Clock Is Sane."""
    start = time.time()
    
    ok, reason = check_clock_skew(max_skew_seconds=600)
    
    elapsed = time.time() - start
    
    assert ok is True
    assert reason == ""
    assert elapsed < 5.0 # Should be very fast (just an HTTP HEAD request to Google)


def test_hc_06_pre_backup_health_raises():
    """HC-06: pre_backup_health() Raises on Source Missing."""
    config = cfg()
    source = source_test_dir()
    
    # Make sure it doesn't exist
    shutil.rmtree(source, ignore_errors=True)
    
    with pytest.raises(HealthError) as exc_info:
        pre_backup_health(
            source_path=str(source),
            mode="cloud",
            gcs_key_path=config.paths.gcs_key_path
        )
        
    assert str(source) in str(exc_info.value)
