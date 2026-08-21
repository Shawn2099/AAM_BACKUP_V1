"""Post-LAN-canary verification: DB row, NAS mirror integrity, logs, NAS power state."""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(r"C:\AAMBackup")
T0 = time.time()


def line(s=""):
    print(f"[+{time.time()-T0:6.1f}s] {s}", flush=True)


line("=== 1) production DB: latest run row ===")
con = sqlite3.connect("file:C:\\BackupAgent\\manifest.db?mode=ro", uri=True, timeout=10)
cur = con.cursor()
row = cur.execute(
    "SELECT id, run_id, mode, started_at, ended_at, status, exit_code, files_copied, bytes_copied, "
    "files_failed, duration_seconds, error_message, extended_metrics FROM run_history ORDER BY id DESC LIMIT 1"
).fetchone()
con.close()
line(f"latest run: {row}")

line("=== 2) NAS mirror: file count + canary + sample ===")
nas = Path(r"\\10.10.186.231\lan_backup\FY26-27")
try:
    n = 0
    sz = 0
    for p in nas.rglob("*"):
        if p.is_file():
            n += 1
            sz += p.stat().st_size
    canary = (nas / ".AAM_TARGET_MOUNTED").exists()
    line(f"NAS FY26-27: {n} files, {sz/1048576:.3f} MiB; canary present: {canary}")
    samples = sorted(str(p.relative_to(nas)) for p in nas.rglob("*") if p.is_file())
    line(f"first: {samples[:3]}")
    line(f"last:  {samples[-3:]}")
except OSError as e:
    line(f"NAS UNREACHABLE (shut down?): {e}")

line("=== 3) local source count (expect 572 / 54.343 MiB) ===")
src = Path(r"E:\FY26-27")
ns = 0
szs = 0
for p in src.rglob("*"):
    if p.is_file():
        ns += 1
        szs += p.stat().st_size
line(f"local E:\\FY26-27: {ns} files, {szs/1048576:.3f} MiB")

line("=== 4) shutdown evidence in app log ===")
lp = Path(r"C:\BackupAgent\logs\backup_2026-08-21.log")
lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
keep = [l for l in lines if "lan" in l.lower() and any(
    k in l.lower() for k in ("shutdown", "robocopy", "lan sync", "mirror", "preflight", "canary",
                             "recorded", "shutdown", "skipped", "complete", "error", "warn"))]
for l in keep[-30:]:
    print("   ", l[:280], flush=True)

line("=== 5) orphans ===")
for exe in ("robocopy.exe", "rclone.exe"):
    p = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}"], capture_output=True, text=True)
    line(f"{exe}: {'NONE' if exe not in p.stdout else 'PRESENT'}")
