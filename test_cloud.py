"""Test script: full cloud backup pipeline on Windows Server 2016."""

from models.config import load_config
from core.fy_router import get_fy_prefix
from core.cloud_preflight import run_cloud_dry_run, _write_temp_config
from core.cloud_sync import run_cloud_sync
from core.cloud_verify import verify_cloud_integrity
from core.cloud_reporter import get_cloud_size
from core.logging import configure as configure_logging
from pathlib import Path

config = load_config("config.yaml")
configure_logging(config.paths.log_directory)

fy = get_fy_prefix()
print(f"FY prefix: {fy}")

# Step 1: Dry-run preflight
print("\n[1] Cloud preflight...")
dry = run_cloud_dry_run(
    config.paths.source_drive,
    config.cloud.bucket,
    fy,
    config.paths.gcs_key_path,
    config.cloud.location,
)
print(f"    ok={dry['ok']}, matched={dry['matched']}, exit={dry['exit_code']}")
if not dry["ok"]:
    print(f"PREFLIGHT FAILED: {dry['error']}")
    exit(1)

# Step 2: Sync
print("\n[2] Cloud sync...")
sync = run_cloud_sync(
    source=config.paths.source_drive,
    bucket=config.cloud.bucket,
    fy_prefix=fy,
    gcs_key_path=config.paths.gcs_key_path,
    location=config.cloud.location,
    project_number=config.cloud.project_number,
    bwlimit=config.cloud.bandwidth_limit,
    retries=config.cloud.retry_count,
    timeout=config.cloud.subprocess_timeout_seconds,
)
print(f"    status={sync['status']}, exit={sync['exit_code']}")
if sync["status"] == "CLOUD_FAILED":
    print(f"SYNC FAILED: {sync.get('error')}")
    exit(1)

# Step 3: Verify
print("\n[3] Cloud verify...")
verify_cfg = _write_temp_config(
    config.paths.gcs_key_path,
    config.cloud.location,
    config.cloud.project_number,
)
try:
    verify = verify_cloud_integrity(
        config.paths.source_drive,
        config.cloud.bucket,
        fy,
        verify_cfg,
    )
    print(f"    verified={verify['verified']}, exit={verify['exit_code']}")
finally:
    Path(verify_cfg).unlink(missing_ok=True)

# Step 4: Report size
print("\n[4] Cloud report...")
report_cfg = _write_temp_config(
    config.paths.gcs_key_path,
    config.cloud.location,
    config.cloud.project_number,
)
try:
    size = get_cloud_size(config.cloud.bucket, fy, report_cfg)
    print(f"    {size['count']} files, {size['bytes']} bytes")
finally:
    Path(report_cfg).unlink(missing_ok=True)

print("\n=== CLOUD PIPELINE COMPLETE ===")
