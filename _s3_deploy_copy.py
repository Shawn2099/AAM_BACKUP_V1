"""Deploy step: copy the 8 eef781f runtime files into C:\\AAMBackup, then verify:
1) each copied file (line-ending normalized) == git blob at eef781f
2) the ENTIRE C:\\AAMBackup tree is unchanged except exactly those 8 files
   (compared against the pre-deploy manifest)
3) config.yaml raw bytes unchanged
"""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROD = Path(r"C:\AAMBackup")
REPO = Path(r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1")
MANIFEST = REPO / "_s3_pre_state" / "prod_backup_manifest.json"

CHANGED = [
    "flow.py",
    "core/cloud_preflight.py",
    "core/cloud_reporter.py",
    "core/cloud_sync.py",
    "core/cloud_verify.py",
    "core/lan_preflight.py",
    "core/report.py",
    "models/config.py",
]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def norm(b: bytes) -> bytes:
    return b.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def git_blob(commit: str, rel: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(REPO), "show", f"{commit}:{rel}"],
        capture_output=True, check=True,
    ).stdout


# --- copy ---
for rel in CHANGED:
    src = REPO / rel
    dst = PROD / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"copied: {rel}")

# --- verify 1: normalized content == eef781f blob ---
print("\n=== post-copy: normalized == eef781f blob ===")
ok = True
for rel in CHANGED:
    same = norm((PROD / rel).read_bytes()) == norm(git_blob("eef781f", rel))
    ok = ok and same
    print(f"  {rel:28s} {'MATCH' if same else 'DIFF!'}")

# --- verify 2: tree diff vs pre-deploy manifest ===
print("\n=== tree diff vs pre-deploy manifest ===")
pre = {k: v["sha256"] for k, v in json.loads(MANIFEST.read_text(encoding="utf-8")).items()}
skip = {".pytest_cache", "__pycache__"}
diffs = {}
now = {}
for p in sorted(PROD.rglob("*")):
    if any(part in skip for part in p.parts):
        continue
    if p.is_file():
        rel = p.relative_to(PROD).as_posix()
        now[rel] = sha(p.read_bytes())

added = sorted(set(now) - set(pre))
removed = sorted(set(pre) - set(now))
modified = sorted(r for r in set(now) & set(pre) if now[r] != pre[r])
print(f"  added:    {added}")
print(f"  removed:  {removed}")
print(f"  modified: {modified}")
expected = sorted(CHANGED)
ok = ok and added == [] and removed == [] and modified == expected

# --- verify 3: config.yaml raw unchanged ===
cfg = (PROD / "config.yaml").read_bytes()
print(f"\nconfig.yaml sha256: {sha(cfg)}  (pre-deploy: {pre.get('config.yaml', 'n/a')})")
ok = ok and sha(cfg) == pre.get("config.yaml")

print("\nDEPLOY VERIFICATION:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
