"""S3 recovery verification: bucket FY26-27/ must equal local E:\\FY26-27."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1")
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from core.rclone_config import temp_rclone_config  # noqa: E402

RC = r"C:\AAMBackup\deploy\bin\rclone.exe"
KEY = r"C:\AAMBackup\deploy\keys\aam-gcs-key.json"
BUCKET = "aam-backup-demo-innovizta"

with temp_rclone_config(KEY, "asia-south1", "920173882190", "STANDARD") as rc:
    def run(*a, t=300):
        r = subprocess.run([RC, "--config", rc, *a], capture_output=True, text=True, timeout=t)
        return r.returncode, r.stdout, r.stderr

    code, out, err = run("size", f"aam_gcs:{BUCKET}/FY26-27/")
    print("--- size FY26-27/ ---")
    print((out or err).strip())

    code, out, err = run("check", r"E:\FY26-27", f"aam_gcs:{BUCKET}/FY26-27/",
                         "--size-only", "--one-way", "--fast-list")
    print(f"--- rclone check (size-only) exit={code} ---")
    print((out or "").strip() or "(no differences)")
    print((err or "").strip())
