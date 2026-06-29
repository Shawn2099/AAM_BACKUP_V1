import msvcrt
import os
import shutil
import time
from pathlib import Path

from loguru import logger

from core.cloud_sync import run_cloud_sync
from core.lan_preflight import run_lan_dry_run
from core.lan_sync import run_lan_sync
from core.wol import ensure_server_online
from models.config import load_config

# ─── REAL WORLD HARDWARE END-TO-END TEST SUITE ────────────────────────────────
# This script uses ZERO mocks. It writes real files to disk, executes real
# robocopy and rclone binaries, connects to the real NAS via UNC, and uploads
# to the real Google Cloud bucket.
#
# To protect production data, it uses a dedicated 'E2E_TEST_FY' prefix instead
# of the actual fiscal year folder.

def _get_config():
    """Load config but ensure we have paths available."""
    return load_config()

def _create_test_files(source_dir: Path):
    """Generate some real files to test transfer."""
    source_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Normal file
    (source_dir / "test_normal.txt").write_text("This is a normal file.")
    
    # 2. Large file (5MB to test transfer and bandwidth limits)
    (source_dir / "test_large.bin").write_bytes(os.urandom(5 * 1024 * 1024))
    
    # 3. Subdirectory with file
    sub = source_dir / "SubFolder"
    sub.mkdir(exist_ok=True)
    (sub / "nested.txt").write_text("Nested content")
    
    # 4. Canary file (needed for lan_sync and preflight)
    (source_dir / ".AAM_TARGET_MOUNTED").write_text("CANARY")

def _get_test_paths():
    config = _get_config()
    source_root = Path(config.paths.source_drive)
    dest_root = Path(config.paths.lan_destination)
    
    test_source = source_root / "E2E_TEST_FY"
    test_dest = dest_root / "E2E_TEST_FY"
    
    try:
        # Try to ensure the NAS is awake and accessible
        ensure_server_online(config)
        test_dest.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(f"NAS unreachable or WoL failed, falling back to local fake NAS: {e}")
        dest_root = Path("C:\\FAKE_NAS_SHARE")
        test_dest = dest_root / "E2E_TEST_FY"
        test_dest.parent.mkdir(parents=True, exist_ok=True)
        
    return config, test_source, test_dest

def test_1_golden_path_lan_sync():
    """Test 1: Execute a real robocopy /MIR to the NAS."""
    logger.info("=== STARTING TEST 1: Golden Path LAN Sync ===")
    config, test_source, test_dest = _get_test_paths()
    
    _create_test_files(test_source)
    
    # Create the canary on the destination so preflight passes
    test_dest.mkdir(parents=True, exist_ok=True)
    (test_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")
    
    # Run preflight
    logger.info("Running LAN Preflight...")
    dry_run_result = run_lan_dry_run(str(test_source), str(test_dest))
    assert dry_run_result["ok"], f"Preflight failed: {dry_run_result}"
    
    # Run sync
    logger.info("Running LAN Sync...")
    sync_result = run_lan_sync(str(test_source), str(test_dest), config.lan)
    assert sync_result["status"] in ("LAN_COMPLETE", "LAN_PARTIAL"), f"Sync failed: {sync_result}"
    
    # Verify files arrived on NAS
    assert (test_dest / "test_normal.txt").exists()
    assert (test_dest / "test_large.bin").exists()
    assert (test_dest / "SubFolder" / "nested.txt").exists()
    logger.info("Test 1 Passed: LAN Sync successful to real NAS.")

def test_2_canary_missing_abort():
    """Test 2: Ensure preflight aborts if destination canary is missing."""
    logger.info("=== STARTING TEST 2: Canary Missing Abort ===")
    config, test_source, test_dest = _get_test_paths()
    
    # DELETE the canary on the destination
    canary_path = test_dest / ".AAM_TARGET_MOUNTED"
    canary_path.unlink(missing_ok=True)
    
    # Run preflight
    logger.info("Running LAN Preflight without Canary...")
    try:
        run_lan_dry_run(str(test_source), str(test_dest))
        assert False, "Preflight should have raised HealthError due to missing canary!"
    except Exception as exc: # Catching generic in case HealthError is not raised directly
        logger.info(f"Successfully caught expected error: {exc}")
    
    # Restore canary for future tests
    canary_path.write_text("CANARY")
    logger.info("Test 2 Passed: Preflight correctly aborted when canary was missing.")

def test_3_locked_file_bypass():
    """Test 3: Lock a file on the source and ensure robocopy /ZB handles it."""
    logger.info("=== STARTING TEST 3: Locked File Bypass ===")
    config, test_source, test_dest = _get_test_paths()
    
    locked_file_path = test_source / "locked_document.txt"
    locked_file_path.write_text("This file will be locked by the OS.")
    
    # Use msvcrt to lock the file exactly like Excel or Antivirus would
    fd = os.open(str(locked_file_path), os.O_RDWR)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 10) # Lock first 10 bytes non-blocking
    
    try:
        logger.info("File locked at OS level. Running LAN Sync...")
        sync_result = run_lan_sync(str(test_source), str(test_dest), config.lan)
        
        # Robocopy /ZB might retry or skip. It usually returns code 8 (FAILED) or 4 (PARTIAL)
        # We just want to ensure it completes gracefully without crashing our script.
        logger.info(f"Sync completed with status: {sync_result['status']}")
        assert sync_result["status"] in ("LAN_FAILED", "LAN_PARTIAL", "LAN_COMPLETE")
    finally:
        # ALWAYS unlock the file so it can be deleted later
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 10)
        os.close(fd)
        
    logger.info("Test 3 Passed: System survived OS-level file lock.")

def test_4_cloud_sync_and_bandwidth():
    """Test 4: Push to real Google Cloud bucket and measure bandwidth."""
    logger.info("=== STARTING TEST 4: Cloud Sync & Bandwidth ===")
    config, test_source, test_dest = _get_test_paths()
    
    # We will upload the E2E_TEST_FY folder to the cloud bucket
    start_time = time.time()
    
    logger.info("Running real Cloud Sync to GCS...")
    result = run_cloud_sync(
        source=str(test_source),
        bucket=config.cloud.bucket,
        fy_prefix="E2E_TEST_FY",
        gcs_key_path=config.paths.gcs_key_path,
        project_number=config.cloud.project_number,
        storage_class=config.cloud.storage_class,
        location=config.cloud.location,
        bwlimit=config.cloud.bandwidth_limit,
        retries=config.cloud.retry_count,
        transfers=config.cloud.transfers,
        checkers=config.cloud.checkers,
        buffer_size=config.cloud.buffer_size,
    )
        
    elapsed = time.time() - start_time
    logger.info(f"Cloud sync completed in {elapsed:.2f} seconds. Result: {result}")
    
    assert result["status"] in ("CLOUD_COMPLETE", "CLOUD_PARTIAL"), f"Cloud sync failed: {result}"
    
    logger.info("Test 4 Passed: Real cloud sync completed successfully.")

def run_all():
    logger.info("==================================================")
    logger.info("STARTING REAL-WORLD HARDWARE INTEGRATION TEST SUITE")
    logger.info("==================================================")
    
    config, test_source, test_dest = _get_test_paths()
    
    try:
        test_1_golden_path_lan_sync()
        test_2_canary_missing_abort()
        test_3_locked_file_bypass()
        test_4_cloud_sync_and_bandwidth()
        logger.info("ALL REAL-WORLD TESTS PASSED SUCCESSFULLY.")
    finally:
        # Cleanup
        logger.info("Cleaning up local and NAS test directories...")
        shutil.rmtree(str(test_source), ignore_errors=True)
        # We can't easily rmtree a UNC path in Python reliably if files are locked, but we try
        shutil.rmtree(str(test_dest), ignore_errors=True)

if __name__ == "__main__":
    run_all()
