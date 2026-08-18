import json
import shutil
import time

import pytest

from core.health import (
    HealthError,
    check_binary_exists,
    check_clock_skew,
    check_gcs_key,
    check_source_drive,
    pre_backup_health,
)
from tests.e2e_helpers import (
    cfg,
    make_file,
    source_test_dir,
)


import os as _gate_os
import sys as _gate_sys

# F4/F5: real-hardware acceptance suite — skipped on dev/CI machines:
#   * Windows-only (robocopy, NSSM, sc, msvcrt, production Windows paths)
#   * requires the production deployment (source drive, NAS, GCS key)
# Run it on the production server with:  set AAM_RUN_REAL_HARDWARE=1
pytestmark = [
    pytest.mark.skipif(_gate_sys.platform != "win32",
                       reason="F5: Windows-only real-hardware acceptance test"),
    pytest.mark.skipif(_gate_os.environ.get("AAM_RUN_REAL_HARDWARE") != "1",
                       reason="F4: real-hardware test — set AAM_RUN_REAL_HARDWARE=1 on the production server"),
]


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
    with open(key_path, encoding="utf-8") as f:
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
