"""Post-CLOUD-canary verification: DB row, Prefect state, bucket, logs, orphans."""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(r"C:\AAMBackup")

RUN_ID = "ae900e3c"

print("=== 1) production DB: new run_history row ===")
con = sqlite3.connect("file:C:\\BackupAgent\\manifest.db?mode=ro", uri=True, timeout=10)
cur = con.cursor()
row = cur.execute(
    "SELECT id, run_id, mode, started_at, ended_at, status, exit_code, files_copied, bytes_copied, "
    "files_failed, duration_seconds, error_message, extended_metrics FROM run_history ORDER BY id DESC LIMIT 1"
).fetchone()
print("latest run:", row)
con.close()

print("\n=== 2) Prefect run state ===")
import httpx
r = httpx.get("http://127.0.0.1:4200/api/flow_runs/ae900e3c-1e29-4013-8404-8c36a61169a1", timeout=15)
fr = r.json()
st = fr.get("state", {})
print(f"state_type={st.get('type')} name={st.get('name')} start={fr.get('start_time')} end={fr.get('end_time')}")

print("\n=== 3) bucket FY26-27/ (expect 572 / 54.343 MiB, idempotent) ===")
RC = r"C:\AAMBackup\deploy\bin\rclone.exe"
from core.rclone_config import temp_rclone_config
with temp_rclone_config(r"C:\AAMBackup\deploy\keys\aam-gcs-key.json", "asia-south1", "920173882190", "STANDARD") as rc:
    p = subprocess.run([RC, "--config", rc, "size", "aam_gcs:aam-backup-demo-innovizta/FY26-27/"],
                       capture_output=True, text=True, timeout=300)
    print((p.stdout or p.stderr).strip())

print("\n=== 4) app log tail (canary entries) ===")
logdir = Path(r"C:\BackupAgent\logs")
logs = sorted(logdir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
for lp in logs[:2]:
    print(f"--- {lp.name} (mtime {time.strftime('%H:%M:%S', time.localtime(lp.stat().st_mtime))}) ---")
    lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
    keep = [l for l in lines if "cloud" in l.lower() or "CLOUD" in l or "22:" in l or "canary" in l.lower() or "FY26" in l or "verify" in l.lower() or "rclone" in l.lower()]
    for l in keep[-25:]:
        print("  ", l[:300])

print("\n=== 5) orphan check ===")
p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq rclone.exe"], capture_output=True, text=True)
print("rclone.exe:", "NONE" if "rclone.exe" not in p.stdout else p.stdout)
p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq robocopy.exe"], capture_output=True, text=True)
print("robocopy.exe:", "NONE" if "robocopy.exe" not in p.stdout else p.stdout)
