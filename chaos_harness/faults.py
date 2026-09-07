"""Fault-injection adapters. Process kills and locks delegate to the
proven chaos tools via subprocess. Same-size corruption is the minimal
parameterized adaptation of the campaign scripts
(`inject_t4.py`, `plant_fault*.py`), which hardcode a single file and a
single evidence dir — logic unchanged, paths parameterized.

Every function enforces chaos-only targets via safety.py first.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from chaos_harness import safety

TOOLS = Path(r"C:\ChaosTest\tools")
PYTHON = sys.executable


def _tool(name: str) -> str:
    p = TOOLS / name
    safety.assert_chaos_path(str(p))
    if not p.exists():
        raise FileNotFoundError(f"chaos tool missing: {p}")
    return str(p)


def kill_rclone(mechanism: str, kill_after_s: float, marker: str,
                verb: str = "sync", log_path: str | None = None) -> subprocess.Popen:
    """Arm killrcl.py (proven T3B mechanism). Returns the killer process."""
    cmd = [PYTHON, _tool("killrcl.py"), mechanism, "0", str(kill_after_s),
           marker, "--verb", verb]
    if log_path:
        safety.assert_chaos_path(log_path)
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


def kill_robocopy(mechanism: str, log_path: str | None = None,
                  extra_args: list[str] | None = None) -> subprocess.Popen:
    """Arm killrob.py (proven T04A mechanism). Returns the killer process."""
    cmd = [PYTHON, _tool("killrob.py"), mechanism] + list(extra_args or [])
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


def hold_share_none_lock(target_file: str, duration_s: float,
                         logfile: str) -> subprocess.Popen:
    """Hold a share-none lock (proven T1/T2 mechanism via holdlock.py)."""
    safety.assert_chaos_path(target_file)
    safety.assert_chaos_path(logfile)
    cmd = [PYTHON, _tool("holdlock.py"), target_file,
           "--duration", str(duration_s), "--logfile", logfile]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def same_size_corrupt(source_file: str, dest_file: str, record_path: str,
                      mutate_len: int = 4096) -> dict:
    """Overwrite dest with same-length/different bytes (inject_t4.py logic,
    parameterized). Preserves original bytes alongside the record for
    guaranteed restore. Source is only read, never written."""
    for p in (source_file, dest_file, record_path):
        safety.assert_chaos_path(p)
    rec = Path(record_path)
    out: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    out["src_size"] = Path(source_file).stat().st_size
    out["dst_size_pre"] = Path(dest_file).stat().st_size
    out["src_sha256"] = sha256_file(source_file)
    out["dst_sha256_pre"] = sha256_file(dest_file)
    out["pre_match"] = out["src_sha256"] == out["dst_sha256_pre"]
    if not out["pre_match"]:
        rec.write_text(json.dumps(out, indent=1), encoding="utf-8")
        raise RuntimeError("pre-condition failed: source and dest differ before injection")
    orig = rec.parent / (Path(dest_file).name + ".ORIGINAL.bin")
    safety.assert_chaos_path(str(orig))
    shutil.copy2(dest_file, orig)
    data = bytearray(Path(source_file).read_bytes())
    mid = len(data) // 2
    half = mutate_len // 2
    for i in range(mid - half, mid + half):
        data[i] = (data[i] + 1) % 256
    Path(dest_file).write_bytes(bytes(data))
    out["dst_size_post"] = Path(dest_file).stat().st_size
    out["dst_sha256_post"] = sha256_file(dest_file)
    out["src_sha256_post"] = sha256_file(source_file)
    out["size_unchanged"] = out["dst_size_pre"] == out["dst_size_post"]
    out["content_diverged"] = out["dst_sha256_post"] != out["src_sha256_post"]
    out["original_preserved"] = str(orig)
    out["dest_file"] = str(dest_file)
    rec.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def restore(record_path: str) -> dict:
    """Restore dest bytes preserved by same_size_corrupt; verify hash."""
    safety.assert_chaos_path(record_path)
    rec = json.loads(Path(record_path).read_text(encoding="utf-8"))
    dest, orig = rec["dest_file"], rec["original_preserved"]
    for p in (dest, orig):
        safety.assert_chaos_path(p)
    shutil.copy2(orig, dest)
    now = sha256_file(dest)
    ok = now == rec["dst_sha256_pre"]
    return {"dest": dest, "restored_sha256": now,
            "expected_sha256": rec["dst_sha256_pre"], "restored_ok": ok}


def dest_variant(dest_file: str, kind: str, record_path: str) -> dict:
    """B2-3 divergence classes (missing/extra/size-diff/mtime-only).

    Same-size content diffs use same_size_corrupt instead. The original
    state is preserved alongside the record for guaranteed restore via
    restore_variant. All targets safety-gated; source never touched
    (this helper only mutates the destination side).
    """
    safety.assert_chaos_path(dest_file)
    safety.assert_chaos_path(record_path)
    if kind not in ("missing", "extra", "size-diff", "mtime-only"):
        raise ValueError(f"unknown variant {kind!r}")
    rec: dict = {"kind": kind, "dest_file": str(dest_file),
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    backup = str(Path(record_path).parent / (Path(dest_file).name + ".ORIGINAL.bin"))
    safety.assert_chaos_path(backup)
    if kind in ("missing", "size-diff", "mtime-only"):
        if not Path(dest_file).exists():
            raise RuntimeError(f"variant {kind} needs an existing dest file")
        shutil.copy2(dest_file, backup)
        rec["pre_size"] = Path(dest_file).stat().st_size
        rec["pre_sha256"] = sha256_file(dest_file)
        if kind == "missing":
            Path(dest_file).unlink()
            rec["engaged"] = not Path(dest_file).exists()
        elif kind == "size-diff":
            with open(dest_file, "r+b") as f:
                f.truncate(rec["pre_size"] // 2)
            rec["engaged"] = Path(dest_file).stat().st_size != rec["pre_size"]
        else:  # mtime-only: same bytes, shifted mtime
            old = Path(dest_file).stat().st_mtime
            os.utime(dest_file, (old - 100000, old - 100000))
            rec["engaged"] = (sha256_file(dest_file) == rec["pre_sha256"]
                              and Path(dest_file).stat().st_mtime != old)
    else:  # extra: brand-new dest-side file (never in source)
        extra = Path(dest_file)
        if extra.exists():
            raise RuntimeError("extra variant needs an absent dest path")
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(b"chaos-extra")
        rec["engaged"] = extra.exists()
    rec["original_preserved"] = backup if kind != "extra" else None
    Path(record_path).write_text(json.dumps(rec, indent=1), encoding="utf-8")
    if not rec["engaged"]:
        raise RuntimeError(f"variant {kind} failed to engage")
    return rec


def restore_variant(record_path: str) -> dict:
    """Undo dest_variant; verify byte-exact restoration where applicable."""
    safety.assert_chaos_path(record_path)
    rec = json.loads(Path(record_path).read_text(encoding="utf-8"))
    dest = rec["dest_file"]
    safety.assert_chaos_path(dest)
    if rec["kind"] == "extra":
        Path(dest).unlink(missing_ok=True)
        return {"dest": dest, "restored_ok": not Path(dest).exists()}
    orig = rec["original_preserved"]
    safety.assert_chaos_path(orig)
    shutil.copy2(orig, dest)
    ok = sha256_file(dest) == rec["pre_sha256"]
    return {"dest": dest, "restored_ok": ok}


def _rclone(args: list[str], rclone_exe: str | None,
            rclone_conf: str, timeout: int = 300) -> subprocess.CompletedProcess:
    safety.assert_chaos_path(rclone_conf)
    exe = rclone_exe or shutil.which("rclone") or "rclone"
    cmd = [exe, *args, "--config", rclone_conf]
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def cloud_plant(local_crafted: str, bucket: str, prefix: str, relpath: str,
                rclone_conf: str, rclone_exe: str | None = None) -> dict:
    """B3-1: upload a crafted object over a chaos-bucket path (T3/P10
    plant procedure, parameterized). Used for same-size GCS plants and
    extra-object plants. Refuses any non-chaos bucket."""
    safety.assert_chaos_bucket(bucket)
    safety.assert_chaos_path(local_crafted)
    remote = f"aam_gcs:{bucket}/{prefix}/{relpath}"
    r = _rclone(["copyto", local_crafted, remote], rclone_exe, rclone_conf)
    if r.returncode != 0:
        raise RuntimeError(f"cloud plant failed rc={r.returncode}: {r.stderr.strip()[:500]}")
    return {"remote": remote, "planted_ok": True, "log": r.stderr.strip()[-500:]}


def cloud_remove(bucket: str, prefix: str, relpath: str,
                 rclone_conf: str, rclone_exe: str | None = None) -> dict:
    """B3-1: delete a chaos-bucket object (plant cleanup / missing-case
    setup). Refuses any non-chaos bucket."""
    safety.assert_chaos_bucket(bucket)
    remote = f"aam_gcs:{bucket}/{prefix}/{relpath}"
    r = _rclone(["deletefile", remote], rclone_exe, rclone_conf)
    if r.returncode != 0:
        raise RuntimeError(f"cloud remove failed rc={r.returncode}: {r.stderr.strip()[:500]}")
    return {"remote": remote, "removed_ok": True}
