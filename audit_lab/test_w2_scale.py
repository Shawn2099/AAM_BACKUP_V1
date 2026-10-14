"""WAVE 2 — scale: what does the manifest/snapshot path cost at real file counts?

Generates real files in the fake bucket, then measures the PRODUCTION
get_cloud_manifest() / walk_lan_destination() + snapshot_to_dict() on them:
wall time, process RSS growth, JSON bytes, and the pickled size of the value
the @task returns (that is what Prefect's result store would persist).
"""
from __future__ import annotations

import os
import pickle
import subprocess
import sys
import time
from pathlib import Path

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(2400)

N = int(os.environ.get("AAM_LAB_FILES", "200000"))


def _make_files(root: Path, n: int, *, payload: int = 64) -> float:
    """Create n tiny files under <root>/bulk as fast as CPython allows."""
    bulk = root / "bulk"
    bulk.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    data = b"x" * payload
    for i in range(n):
        fd = os.open(str(bulk / f"f_{i:07d}.txt"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    return time.perf_counter() - t0


def _rss_mb() -> float:
    import psutil
    return psutil.Process().memory_info().rss / (1024 * 1024)


def test_w2_scale_manifest_and_snapshots(sandbox):
    import core.cloud_reporter as crep
    from core.lan_manifest import snapshot_to_dict, walk_lan_destination
    from audit_lab.labconf import write_local_rclone_config

    # ── 1. generate a real file set in the fake bucket ────────────────────────
    bucket_root = sandbox / "buckets"
    fy = bucket_root / "lab-bucket" / "FY26-26"
    gen_s = _make_files(fy, N)
    real = len([p for p in fy.rglob("*") if p.is_file()])
    print(f"[W2] generated {real} files in {gen_s:.1f}s at {fy}")

    rc_path = write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc_path)
    cwd = os.getcwd()
    os.chdir(bucket_root)
    try:
        # ── 2. production get_cloud_manifest (rclone lsjson -R) ───────────────
        rss0 = _rss_mb()
        t0 = time.perf_counter()
        manifest = crep.get_cloud_manifest("lab-bucket", "FY26-26", rc_path, timeout=1800)
        t_manifest = time.perf_counter() - t0
        rss1 = _rss_mb()

        json_len = len(str(manifest))
        try:
            pickle_len = len(pickle.dumps(manifest, protocol=pickle.HIGHEST_PROTOCOL))
        except Exception:
            pickle_len = -1
        deep = sum(sys.getsizeof(m) for m in manifest[:2000]) / max(1, min(2000, len(manifest)))

        L.record("W2.manifest",
                 "get_cloud_manifest's cost at this file count (and extrapolation)",
                 f"objects={len(manifest)} | wall={t_manifest:.1f}s | RSS {rss0:.0f}MB -> {rss1:.0f}MB "
                 f"(+{rss1 - rss0:.0f}MB, {(rss1 - rss0) / max(1, len(manifest)) * 1024:.1f} KB/object) | "
                 f"str(list)={json_len / 1e6:.1f} MB ({json_len / max(1, len(manifest)):.0f} B/object) | "
                 f"pickle={pickle_len / 1e6:.1f} MB ({pickle_len / max(1, len(manifest)):.0f} B/object)",
                 f"extrapolation -> 1M: ~{(rss1 - rss0) * 1e6 / max(1, len(manifest)) * 1024:.0f} MB RSS, "
                 f"~{t_manifest * 1e6 / max(1, len(manifest)):.0f}s | "
                 f"2.5M: ~{(rss1 - rss0) * 2.5e6 / max(1, len(manifest)) * 1024:.0f} MB RSS, "
                 f"~{t_manifest * 2.5e6 / max(1, len(manifest)):.0f}s")
        # Tier B: this experiment was a pure measurement with no threshold, so a
        # regression (a leak, a quadratic parse) was invisible in a green run.
        # Generous ceilings only - they fire on real regressions, not box noise.
        assert len(manifest) == N, f"manifest returned {len(manifest)} of {N} objects"
        per_object_kb = (rss1 - rss0) / max(1, len(manifest)) * 1024
        assert per_object_kb < 1.0, (
            f"per-object RSS growth {per_object_kb:.2f} KB/object - a manifest "
            "regression would push the 1M-object figure past the measured ~500 MB")
        assert t_manifest < 60.0, f"lsjson-based manifest took {t_manifest:.1f}s at {N} objects"
        assert pickle_len < 30_000_000, (
            f"pickled manifest is {pickle_len / 1e6:.1f} MB at {N} objects - "
            "M-3: this is what Prefect persists per task result")

        # ── 3. LAN snapshot cost (os.walk path, used before AND after sync) ────
        nas = sandbox / "nas" / "FY26-26"
        t0 = time.perf_counter()
        snap = snapshot_to_dict(walk_lan_destination(str(nas))) if nas.exists() else {}
        t_snap = time.perf_counter() - t0
        if snap:
            rss2 = _rss_mb()
            try:
                pk = len(pickle.dumps(snap, protocol=pickle.HIGHEST_PROTOCOL))
            except Exception:
                pk = -1
            L.record("W2.lan_snapshot",
                     "LAN before/after snapshot cost (two of these per run, passed as task results)",
                     f"files={len(snap)} | wall={t_snap:.1f}s | RSS now {rss2:.0f}MB "
                     f"(+{(rss2 - rss1):.0f}MB) | pickle={pk / 1e6:.1f} MB",
                     f"extrapolation -> 1M: ~{t_snap * 1e6 / max(1, len(snap)):.0f}s per snapshot, "
                     f"~{pk * 1e6 / max(1, len(snap)) / 1e6:.0f} MB pickled each; two of them per run")

        # ── 4. lsjson alone (rclone side) vs Python parsing ───────────────────
        from audit_lab.labconf import run as _run

        t0 = time.perf_counter()
        r = _run([L.RCLONE_EXE, "lsjson", f"aam_gcs:lab-bucket/FY26-26", "-R",
                  "--config", rc_path, "--fast-list"], timeout=1800)
        t_rclone = time.perf_counter() - t0
        L.record("W2.lsjson_subprocess",
                 "rclone lsjson -R wall time alone (is manifest_timeout_seconds=900 enough?)",
                 f"rclone exit={r.returncode} wall={t_rclone:.1f}s stdout={(len(r.stdout) / 1e6):.1f} MB "
                 f"stderr={r.stderr.strip()[:160]}",
                 f"extrapolation -> 1M ~{t_rclone * 1e6 / max(1, len(manifest)):.0f}s, "
                 f"2.5M ~{t_rclone * 2.5e6 / max(1, len(manifest)):.0f}s "
                 f"(config manifest_timeout_seconds=900)")
    finally:
        os.chdir(cwd)
        for (mod, attr), orig in saved.items():
            setattr(mod, attr, orig)
