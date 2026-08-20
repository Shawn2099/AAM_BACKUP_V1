"""S3 post-rt verification: re-hash E:\\FY26-27, list NAS FY folder, snapshot
bucket — then diff against _s3_pre_state and PROVE the live data was untouched."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1")
PRE = ROOT / "_s3_pre_state"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT))
from core.rclone_config import temp_rclone_config  # noqa: E402

ok = True

# ── 1. E:\FY26-27 ─────────────────────────────────────────────────────────
fy = Path(r"E:\FY26-27")
post = {}
for p in sorted(fy.rglob("*")):
    if p.is_file():
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        post[str(p.relative_to(fy))] = {"size": p.stat().st_size, "sha256": h}
pre = json.loads((PRE / "e_fy26_27_manifest.json").read_text(encoding="utf-8"))
if post == pre:
    print(f"E:\\FY26-27: IDENTICAL ({len(post)} files, SHA-256 verified)")
else:
    ok = False
    added = set(post) - set(pre)
    removed = set(pre) - set(post)
    changed = {k for k in set(pre) & set(post) if pre[k] != post[k]}
    print(f"E:\\FY26-27: CHANGED! added={len(added)} removed={len(removed)} changed={len(changed)}")
    for k in list(added)[:10]:
        print("  +", k)
    for k in list(removed)[:10]:
        print("  -", k)
    for k in list(changed)[:10]:
        print("  ~", k)

# ── 2. NAS FY folder ──────────────────────────────────────────────────────
nas_fy = Path(r"\\10.10.186.231\lan_backup\FY26-27")
nas_listing = {}
if nas_fy.exists():
    for p in sorted(nas_fy.rglob("*")):
        if p.is_file():
            nas_listing[str(p.relative_to(nas_fy))] = p.stat().st_size
pre_nas = json.loads((PRE / "nas_fy26_27_listing.json").read_text(encoding="utf-8"))
if nas_listing == pre_nas:
    print(f"NAS FY26-27: IDENTICAL ({len(nas_listing)} files)")
else:
    ok = False
    print(f"NAS FY26-27: CHANGED! pre={pre_nas} post={nas_listing}")

# ── 3. Suite-owned NAS namespaces (must be cleaned) ───────────────────────
for name in ("E2E_TEST_DEST", "E2E_TEST_FY"):
    p = Path(r"\\10.10.186.231\lan_backup") / name
    if p.exists():
        n = sum(1 for x in p.rglob("*") if x.is_file())
        print(f"NAS suite-namespace {name}: STILL PRESENT ({n} files)")
    else:
        print(f"NAS suite-namespace {name}: purged")
src_leftover = Path(r"E:\E2E_TEST_SOURCE")
if src_leftover.exists():
    print(f"E:\\E2E_TEST_SOURCE: STILL PRESENT")
else:
    print("E:\\E2E_TEST_SOURCE: purged")
src_e2e = Path(r"E:\test_backup\E2E_TEST_FY")
if src_e2e.exists():
    print(f"E:\\test_backup\\E2E_TEST_FY: STILL PRESENT")
else:
    print("E:\\test_backup\\E2E_TEST_FY: purged")

# ── 4. Bucket ──────────────────────────────────────────────────────────────
RC = r"C:\AAMBackup\deploy\bin\rclone.exe"
KEY = r"C:\AAMBackup\deploy\keys\aam-gcs-key.json"
bucket = "aam-backup-demo-innovizta"


def run(*args, cfg):
    r = subprocess.run([RC, "--config", cfg, *args],
                       capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout, r.stderr


with temp_rclone_config(KEY, "asia-south1", "920173882190", "STANDARD") as rcfile:
    pre_bucket = json.loads((PRE / "bucket_state.json").read_text(encoding="utf-8"))
    for prefix in ("FY26-27/", "NONEXISTENT_BUCKET/", "docs/"):
        code, out, err = run("lsf", f"aam_gcs:{bucket}/{prefix}", cfg=rcfile)
        n = len([l for l in out.splitlines() if l.strip()]) if code == 0 else -1
        pre_n = pre_bucket.get(prefix, {}).get("count")
        status = "UNCHANGED" if n == pre_n else "CHANGED"
        if n != pre_n:
            ok = False
        print(f"bucket {prefix}: {n} entries (pre {pre_n}) — {status}")
    code, out, err = run("size", f"aam_gcs:{bucket}/FY26-27/", cfg=rcfile)
    print("--- size FY26-27/ (must be 572 objects / 54.343 MiB) ---")
    print((out or err).strip())
    # root E2E_TEST_FY (suite-owned — should be empty after purge)
    code, out, err = run("lsf", f"aam_gcs:{bucket}/E2E_TEST_FY/", cfg=rcfile)
    n = len([l for l in out.splitlines() if l.strip()]) if code == 0 else -1
    print(f"bucket E2E_TEST_FY/ (root, suite-owned): {n} entries (expect 0 after purge)")
    if n not in (0,):
        ok = False

print()
print("POST-STATE VERIFICATION:", "PASS — all live data untouched" if ok
      else "FAIL — see differences above")
sys.exit(0 if ok else 1)
