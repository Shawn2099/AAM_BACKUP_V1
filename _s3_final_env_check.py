"""Final environment verification: bucket prefixes, services, orphans, NAS."""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(r"C:\AAMBackup")

print("=== bucket prefixes (expect FY26-27: 572/54.343 MiB; E2E_TEST_FY: 0; junk untouched) ===")
from core.rclone_config import temp_rclone_config

RC = r"C:\AAMBackup\deploy\bin\rclone.exe"
with temp_rclone_config(r"C:\AAMBackup\deploy\keys\aam-gcs-key.json", "asia-south1", "920173882190", "STANDARD") as rc:
    for pfx in ("FY26-27/", "E2E_TEST_FY/", "NONEXISTENT_BUCKET/", "docs/"):
        r = subprocess.run([RC, "--config", rc, "size", f"aam_gcs:aam-backup-demo-innovizta/{pfx}"],
                           capture_output=True, text=True, timeout=300)
        lines = (r.stdout or r.stderr).strip().splitlines()
        print(f"  bucket {pfx:22s} {lines[0] if lines else '?'}")

print("\n=== NAS state ===")
nas = Path(r"\\10.10.186.231\lan_backup\FY26-27")
try:
    n = sum(1 for p in nas.rglob("*") if p.is_file())
    print(f"  NAS reachable: True — FY26-27 has {n} files (expect 572)")
except OSError as e:
    print(f"  NAS reachable: False — {e}")

print("\n=== orphans ===")
for exe in ("robocopy.exe", "rclone.exe", "shutdown.exe"):
    p = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}"], capture_output=True, text=True)
    print(f"  {exe}: {'PRESENT' if exe in p.stdout else 'NONE'}")

print("\n=== python processes (AAMBackup tree only) ===")
out = subprocess.run(["powershell", "-NoProfile", "-Command",
                      "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                      "Where-Object { $_.CommandLine -like '*AAMBackup*' } | "
                      "ForEach-Object { '{0} :: {1}' -f $_.ProcessId, $_.CommandLine }"],
                     capture_output=True, text=True)
print(out.stdout.strip() or "  (none)")
