"""Pre-canary snapshot: production DB, NAS, bucket, local source, Prefect state."""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(r"C:\AAMBackup")
os.environ.setdefault("PREFECT_API_URL", "http://127.0.0.1:4200/api")

OUT = Path(r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\_s3_pre_state\pre_canary")
OUT.mkdir(parents=True, exist_ok=True)
snap = {"ts": time.strftime("%Y-%m-%d %H:%M:%S %Z")}

# --- production DB (read-only) ---
con = sqlite3.connect("file:C:\\BackupAgent\\manifest.db?mode=ro", uri=True, timeout=10)
cur = con.cursor()
tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("tables:", tables)
for t in tables:
    n = cur.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
    print(f"  {t}: {n} rows")
    if n:
        cols = [c[1] for c in cur.execute(f"PRAGMA table_info([{t}])")]
        print("   cols:", cols)
        rows = cur.execute(f"SELECT * FROM [{t}] ORDER BY rowid DESC LIMIT 5").fetchall()
        for r in rows:
            print("   ", r)
con.close()

# --- local source ---
src = Path(r"E:\FY26-27")
n = 0
sz = 0
for p in src.rglob("*"):
    if p.is_file():
        n += 1
        sz += p.stat().st_size
snap["local_source"] = {"files": n, "bytes": sz}
print(f"local E:\\FY26-27: {n} files, {sz/1048576:.3f} MiB")

# --- NAS ---
nas = Path(r"\\10.10.186.231\lan_backup\FY26-27")
try:
    nn = 0
    nsz = 0
    canary = nas / ".AAM_TARGET_MOUNTED"
    for p in nas.rglob("*"):
        if p.is_file():
            nn += 1
            nsz += p.stat().st_size
    print(f"NAS FY26-27: {nn} files, {nsz} bytes; canary present: {canary.exists()}")
    snap["nas"] = {"files": nn, "bytes": nsz, "canary": canary.exists()}
except OSError as e:
    print("NAS unreachable:", e)
    snap["nas"] = {"error": str(e)}

# --- bucket (via app's rclone config) ---
RC = r"C:\AAMBackup\deploy\bin\rclone.exe"
from core.rclone_config import temp_rclone_config

with temp_rclone_config(r"C:\AAMBackup\deploy\keys\aam-gcs-key.json", "asia-south1", "920173882190", "STANDARD") as rc:
    r = subprocess.run([RC, "--config", rc, "size", "aam_gcs:aam-backup-demo-innovizta/FY26-27/"],
                       capture_output=True, text=True, timeout=300)
    out = (r.stdout or r.stderr).strip()
    print("bucket FY26-27/:", out)
    snap["bucket_fy26_27"] = out.splitlines()

with open(OUT / "pre_canary_snapshot.json", "w", encoding="utf-8") as f:
    json.dump(snap, f, indent=1, default=str)
print("snapshot saved ->", OUT / "pre_canary_snapshot.json")
