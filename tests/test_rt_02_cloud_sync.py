import os
import shutil
import time
import pytest
from pathlib import Path

from core.cloud_sync import run_cloud_sync
from core.cloud_verify import verify_cloud_integrity
from core.cloud_preflight import run_cloud_dry_run
from core.cloud_reporter import get_cloud_size, get_cloud_manifest
from core.rclone_config import temp_rclone_config

from tests.e2e_helpers import (
    cfg,
    source_test_dir,
    make_file,
    assert_log_contains,
    capture_logs,
)


@pytest.fixture(scope="module", autouse=True)
def setup_teardown_cloud():
    """Setup and teardown for cloud tests."""
    config = cfg()
    source = source_test_dir()
    
    # Cleanup before
    shutil.rmtree(source, ignore_errors=True)
    source.mkdir(parents=True, exist_ok=True)
    
    yield
    
    # Cleanup after
    shutil.rmtree(source, ignore_errors=True)
    
    # Purge from cloud
    import subprocess
    from core.process import resolve_binary
    
    with temp_rclone_config(
        config.paths.gcs_key_path,
        config.cloud.location,
        config.cloud.project_number,
        config.cloud.storage_class,
    ) as config_path:
        rclone_exe = resolve_binary("rclone") or "rclone"
        subprocess.run([
            rclone_exe, "purge",
            f"aam_gcs:{config.cloud.bucket}/E2E_TEST_FY",
            "--config", config_path
        ], check=False)


def get_cloud_files(config_path, bucket, prefix):
    """Helper to get files via rclone lsjson."""
    import subprocess
    import json
    from core.process import resolve_binary
    
    rclone_exe = resolve_binary("rclone") or "rclone"
    result = subprocess.run([
        rclone_exe, "lsjson",
        f"aam_gcs:{bucket}/{prefix}",
        "--config", config_path,
        "--recursive",
        "--files-only",
    ], capture_output=True, text=True, check=True)
    
    if not result.stdout.strip():
        return []
        
    return json.loads(result.stdout)


def test_cloud_01_golden_path_new_files(capture_logs):
    """CLOUD-01: Golden Path — Files Appear in GCS."""
    source = source_test_dir()
    config = cfg()
    
    # Create 3 files
    make_file(source / "cloud_small.txt", 1024)
    make_file(source / "cloud_medium.bin", 2 * 1024 * 1024)
    make_file(source / "cloud_nested" / "file.txt", 512)
    
    result = run_cloud_sync(
        source=str(source),
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
        timeout=config.cloud.subprocess_timeout_seconds,
    )
    
    assert result["status"] == "CLOUD_COMPLETE"
    assert result["exit_code"] == 0
    assert result["error"] is None
    
    with temp_rclone_config(
        config.paths.gcs_key_path,
        config.cloud.location,
        config.cloud.project_number,
        config.cloud.storage_class,
    ) as config_path:
        files = get_cloud_files(config_path, config.cloud.bucket, "E2E_TEST_FY")
        
    assert len(files) == 3
    
    sizes = {f.get("Path", f.get("path")): f.get("Size", f.get("size")) for f in files}
    assert sizes.get("cloud_small.txt") == 1024
    assert sizes.get("cloud_medium.bin") == 2 * 1024 * 1024
    assert sizes.get("cloud_nested/file.txt") == 512
    
    assert_log_contains(capture_logs, "CLOUD_COMPLETE")


def test_cloud_02_idempotency_zero_bytes():
    """CLOUD-02: Idempotency — Second Run Transfers Zero New Bytes."""
    source = source_test_dir()
    config = cfg()
    
    start_time = time.time()
    
    result = run_cloud_sync(
        source=str(source),
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
        timeout=config.cloud.subprocess_timeout_seconds,
    )
    
    elapsed = time.time() - start_time
    
    assert result["status"] in ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE")
    assert result["exit_code"] in (0, 9)
    assert elapsed < 15.0  # Should be very fast


def test_cloud_03_modified_file_reuploaded():
    """CLOUD-03: Modified File Is Re-Uploaded."""
    source = source_test_dir()
    config = cfg()
    
    # Overwrite
    time.sleep(2.1) # modify-window 2s
    make_file(source / "cloud_small.txt", 2048)
    
    result = run_cloud_sync(
        source=str(source),
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
        timeout=config.cloud.subprocess_timeout_seconds,
    )
    
    assert result["status"] == "CLOUD_COMPLETE"
    
    with temp_rclone_config(
        config.paths.gcs_key_path,
        config.cloud.location,
        config.cloud.project_number,
        config.cloud.storage_class,
    ) as config_path:
        files = get_cloud_files(config_path, config.cloud.bucket, "E2E_TEST_FY")
        
    sizes = {f.get("Path", f.get("path")): f.get("Size", f.get("size")) for f in files}
    assert sizes.get("cloud_small.txt") == 2048


def test_cloud_04_bandwidth_limiting():
    """CLOUD-04: Bandwidth Limiting Is Actually Enforced."""
    source = source_test_dir()
    config = cfg()
    
    make_file(source / "bw_test.bin", 15 * 1024 * 1024)  # 15 MB
    
    start_time = time.time()
    
    result = run_cloud_sync(
        source=str(source),
        bucket=config.cloud.bucket,
        fy_prefix="E2E_TEST_FY",
        gcs_key_path=config.paths.gcs_key_path,
        project_number=config.cloud.project_number,
        storage_class=config.cloud.storage_class,
        location=config.cloud.location,
        bwlimit="1M", # 1 MB/s
        retries=config.cloud.retry_count,
        transfers=config.cloud.transfers,
        checkers=config.cloud.checkers,
        buffer_size=config.cloud.buffer_size,
        timeout=config.cloud.subprocess_timeout_seconds,
    )
    
    elapsed = time.time() - start_time
    
    assert result["status"] == "CLOUD_COMPLETE"
    # Allow 25% tolerance (11.25s) + some overhead
    assert elapsed >= 10.0


def test_cloud_05_verify_integrity():
    """CLOUD-05: Verify Cloud Integrity — Post-Sync Check."""
    source = source_test_dir()
    config = cfg()
    
    with temp_rclone_config(
        config.paths.gcs_key_path,
        config.cloud.location,
        config.cloud.project_number,
        config.cloud.storage_class,
    ) as config_path:
        result = verify_cloud_integrity(
            source=str(source),
            bucket=config.cloud.bucket,
            fy_prefix="E2E_TEST_FY",
            config_path=config_path,
        )
        
    assert result["verified"] is True
    assert result["exit_code"] == 0


def test_cloud_06_verify_fails_on_tampered_gcs(capture_logs):
    """CLOUD-06: Verify Fails on Tampered GCS File (Deliberate Mismatch)."""
    source = source_test_dir()
    config = cfg()
    
    # Add new file without syncing
    make_file(source / "unsynced.txt", 1024)
    
    with temp_rclone_config(
        config.paths.gcs_key_path,
        config.cloud.location,
        config.cloud.project_number,
        config.cloud.storage_class,
    ) as config_path:
        result = verify_cloud_integrity(
            source=str(source),
            bucket=config.cloud.bucket,
            fy_prefix="E2E_TEST_FY",
            config_path=config_path,
        )
        
    assert result["verified"] is False
    assert result["exit_code"] == 1
    assert "Integrity mismatch" in str(result["error"])
    
    assert_log_contains(capture_logs, "mismatch")


def test_cloud_07_gcs_reporter_size():
    """CLOUD-07: GCS Reporter — get_cloud_size Returns Accurate Count."""
    source = source_test_dir()
    config = cfg()
    
    # Run sync to ensure unsynced.txt is up there
    run_cloud_sync(
        source=str(source),
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
        timeout=config.cloud.subprocess_timeout_seconds,
    )
    
    with temp_rclone_config(
        config.paths.gcs_key_path,
        config.cloud.location,
        config.cloud.project_number,
        config.cloud.storage_class,
    ) as config_path:
        size = get_cloud_size(config.cloud.bucket, "E2E_TEST_FY", config_path)
        
    # small (2048) + medium (2MB) + nested (512) + bw (15MB) + unsynced (1024)
    expected_bytes = 2048 + 2 * 1024 * 1024 + 512 + 15 * 1024 * 1024 + 1024
    assert size["count"] == 5
    assert abs(size["bytes"] - expected_bytes) < (expected_bytes * 0.01)


def test_cloud_08_gcs_reporter_manifest():
    """CLOUD-08: GCS Reporter — get_cloud_manifest Returns File List."""
    config = cfg()
    
    with temp_rclone_config(
        config.paths.gcs_key_path,
        config.cloud.location,
        config.cloud.project_number,
        config.cloud.storage_class,
    ) as config_path:
        manifest = get_cloud_manifest(config.cloud.bucket, "E2E_TEST_FY", config_path)
        
    assert isinstance(manifest, list)
    assert len(manifest) == 5
    
    paths = [f.get("Path", f.get("path")) for f in manifest]
    assert "cloud_small.txt" in paths
    assert "cloud_medium.bin" in paths


def test_cloud_09_preflight_fails_bad_creds(capture_logs):
    """CLOUD-09: Preflight Fails on Bad GCS Credentials."""
    source = source_test_dir()
    config = cfg()
    
    fake_key = Path("fake_gcs_key.json")
    fake_key.write_text('{"type": "service_account", "project_id": "fake"}')
    
    try:
        result = run_cloud_dry_run(
            source=str(source),
            bucket=config.cloud.bucket,
            fy_prefix="E2E_TEST_FY",
            gcs_key_path=str(fake_key),
            project_number=config.cloud.project_number,
            storage_class=config.cloud.storage_class,
            location=config.cloud.location,
            timeout=30,
        )
        
        assert result["ok"] is False
        err = result["error"].lower()
        assert "fake_gcs_key.json" in err or "auth" in err or "credential" in err or "private key" in err
    finally:
        fake_key.unlink(missing_ok=True)


def test_cloud_10_preflight_succeeds_valid_config():
    """CLOUD-10: Preflight Succeeds on Valid Config."""
    source = source_test_dir()
    config = cfg()
    
    result = run_cloud_dry_run(
        source=str(source),
        bucket=config.cloud.bucket,
        fy_prefix="E2E_TEST_FY",
        gcs_key_path=config.paths.gcs_key_path,
        project_number=config.cloud.project_number,
        storage_class=config.cloud.storage_class,
        location=config.cloud.location,
        timeout=30,
    )
    
    assert result["ok"] is True
