"""Deploy gate 1: compare C:\\AAMBackup runtime files against the git checkout
at d27198c (the audited commit prod supposedly runs) and eef781f (deploy target).
Also builds a full-tree hash manifest of C:\\AAMBackup for rollback verification."""
import hashlib
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROD = Path(r"C:\AAMBackup")
REPO = Path(r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1")

RUNTIME = [
    "flow.py",
    "core/cloud_preflight.py",
    "core/cloud_reporter.py",
    "core/cloud_sync.py",
    "core/cloud_verify.py",
    "core/lan_preflight.py",
    "core/lan_sync.py",
    "core/manifest.py",
    "core/process.py",
    "core/report.py",
    "core/rclone_config.py",
    "core/fy_rollover.py",
    "core/health.py",
    "core/wol.py",
    "core/time_utils.py",
    "models/config.py",
    "launch.py",
    "serve.py",
    "watchdog.py",
    "ui.py",
    "config.yaml",
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def norm(b: bytes) -> bytes:
    """Line-ending-insensitive comparison (repo stores LF; prod files are CRLF)."""
    return b.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def git_blob(commit: str, rel: str) -> bytes:
    out = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{commit}:{rel}"],
        capture_output=True, check=True,
    ).stdout
    return out


print(f"{'file':32s} {'prod(raw)':12s} {'==d27198c':10s} {'==eef781f':10s}")
prod_hashes = {}
for rel in RUNTIME:
    pp = PROD / rel
    if not pp.exists():
        print(f"{rel:32s} MISSING in prod")
        continue
    h = sha(pp)
    prod_hashes[rel] = h
    raw = pp.read_bytes()
    at_d = "?"
    at_e = "?"
    if rel != "config.yaml":  # config.yaml is untracked (contains credentials)
        at_d = "YES" if norm(raw) == norm(git_blob("d27198c", rel)) else "NO "
        at_e = "YES" if norm(raw) == norm(git_blob("eef781f", rel)) else "NO "
    else:
        at_d = "untracked"
        at_e = "untracked"
    print(f"{rel:32s} {h[:12]}  {at_d:10s} {at_e:10s}")

# full-tree manifest (exclude volatile dirs)
print()
print("=== full C:\\AAMBackup manifest ===")
manifest = {}
skip_dirs = {".pytest_cache", "__pycache__"}
for p in sorted(PROD.rglob("*")):
    if any(part in skip_dirs for part in p.parts):
        continue
    if p.is_file():
        rel = p.relative_to(PROD).as_posix()
        manifest[rel] = {"size": p.stat().st_size, "sha256": sha(p)}
out = REPO / "_s3_pre_state" / "prod_backup_manifest.json"
import json
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(manifest, indent=0), encoding="utf-8")
print(f"manifest: {len(manifest)} files -> {out}")
