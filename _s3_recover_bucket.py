"""S3 recovery: re-sync the intact local E:\\FY26-27 to the bucket FY26-27/
prefix using the app's own production code path (core.cloud_sync.run_cloud_sync).

Context: test_rt_06::test_pipe_01's monkeypatch missed flow.py's own
get_fy_prefix binding, so the pipeline mirror-synced a 5-file scratch source
to the production FY26-27/ bucket prefix (uploaded 5 test files, deleted 567
production objects). The local source E:\\FY26-27 was verified byte-intact
(SHA-256) before and after the incident — this re-sync restores the prefix
exactly and removes the 5 stray test files.
"""
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1")
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.cloud_sync import run_cloud_sync  # noqa: E402

result = run_cloud_sync(
    source=r"E:\FY26-27",
    bucket="aam-backup-demo-innovizta",
    fy_prefix="FY26-27",
    gcs_key_path=r"C:\AAMBackup\deploy\keys\aam-gcs-key.json",
    project_number="920173882190",
    storage_class="STANDARD",
    location="asia-south1",
    bwlimit="50M",
    retries=3,
    transfers=4,
    checkers=16,
    buffer_size="64M",
)
print("SYNC RESULT:", result)
