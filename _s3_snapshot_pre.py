"""S3 pre-rt snapshot: hash the live E:\\FY26-27 dataset, list NAS FY folder,
and list the bucket prefixes — so the post-rt run can prove none of these
were touched by the real-hardware suite."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

OUT = Path(r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\_s3_pre_state")
OUT.mkdir(exist_ok=True)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 1. E:\FY26-27 hash manifest ─────────────────────────────────────────
fy = Path(r"E:\FY26-27")
manifest = {}
for p in sorted(fy.rglob("*")):
    if p.is_file():
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        manifest[str(p.relative_to(fy))] = {"size": p.stat().st_size, "sha256": h}
(OUT / "e_fy26_27_manifest.json").write_text(
    json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8"
)
print(f"E:\\FY26-27: {len(manifest)} files hashed")

# ── 2. NAS FY folder listing ────────────────────────────────────────────
nas_fy = Path(r"\\10.10.186.231\lan_backup\FY26-27")
nas_listing = {}
if nas_fy.exists():
    for p in sorted(nas_fy.rglob("*")):
        if p.is_file():
            nas_listing[str(p.relative_to(nas_fy))] = p.stat().st_size
(OUT / "nas_fy26_27_listing.json").write_text(json.dumps(nas_listing, indent=1), encoding="utf-8")
print(f"NAS FY26-27: {len(nas_listing)} files")

# ── 3. Bucket state via rclone (demo key) ────────────────────────────────
rclone = r"C:\AAMBackup\deploy\bin\rclone.exe"
rcfile = Path(OUT / "rt_probe.conf")
rcfile.write_text(
    "[aam_gcs]\ntype = google\nservice_account_file = "
    r"C:\AAMBackup\deploy\keys\aam-demo-gcs-d9427ae2cacc.json" + "\n",
    encoding="utf-8",
)
bucket = "aam-backup-demo-innovizta"


def rclone(*args, timeout=120):
    r = subprocess.run(
        [rclone, "--config", str(rcfile), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


for prefix in ("FY26-27/", "E2E_TEST_FY/", "FY23-24/", "NONEXISTENT_BUCKET/", "docs/"):
    code, out, err = rclone("lsf", f"aam_gcs:{bucket}/{prefix}")
    n = len([l for l in out.splitlines() if l.strip()]) if code == 0 else -1
    print(f"bucket {prefix}: {'EXISTS' if code == 0 else 'absent'} ({n} objects)"
          + (f" — rclone exit {code}: {err.strip()[:100]}" if code != 0 else ""))

# storage class of a live object (sample)
code, out, err = rclone("lsl", f"aam_gcs:{bucket}/FY26-27/", "--limit", "3")
print("lsl FY26-27/ sample:")
print(out or err)

# FY26-27 count exact
code, out, err = rclone("size", f"aam_gcs:{bucket}/FY26-27/")
print("size FY26-27/:")
print(out or err)

print("SNAPSHOT DONE")
