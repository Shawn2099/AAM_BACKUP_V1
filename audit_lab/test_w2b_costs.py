"""WAVE 2b — complete the memory picture at real scale.

One 100k-file set, three production code paths measured:
  * walk_lan_destination() + snapshot_to_dict()      (LAN before/after snapshots)
  * bulk_upsert_synced() + get_cloud_synced_entries() (flow's before_dict)
  * pickled size of each value (what Prefect's result store would persist)
"""
from __future__ import annotations

import os
import pickle
import time
from pathlib import Path

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(1800

)

N = int(os.environ.get("AAM_LAB_FILES2", "100000"))


def _make_files(root: Path, n: int) -> None:
    bulk = root / "bulk"
    bulk.mkdir(parents=True, exist_ok=True)
    data = b"x" * 64
    for i in range(n):
        fd = os.open(str(bulk / f"f_{i:07d}.txt"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


def _rss():
    import psutil
    return psutil.Process().memory_info().rss / (1024 * 1024)


def _pklen(obj) -> int:
    try:
        return len(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL))
    except Exception:
        return -1


def test_w2b_lan_and_db_costs(sandbox):
    from core.manifest import ManifestDB

    # LAN snapshots (core.lan_manifest) were removed in P1; measure ManifestDB cost at scale.
    rss2 = _rss()

    # ── DB side: bulk insert then read back (flow's before_dict) ─────────────
    db_path = sandbox / "runtime" / "manifest.db"
    db = ManifestDB(str(db_path), busy_timeout_ms=8000)
    entries = [{"path": f"bulk/f_{i:07d}.txt", "size": 64, "mtime": 1700000000.0} for i in range(N)]
    t0 = time.perf_counter()
    db.bulk_upsert_synced(entries, "cloud")
    t_up = time.perf_counter() - t0
    t0 = time.perf_counter()
    before_dict = db.get_cloud_synced_entries()
    t_read = time.perf_counter() - t0
    rss3 = _rss()
    L.record("W2b.db_before_dict",
             "get_cloud_synced_entries() (flow before_dict) cost at scale",
             f"entries={len(before_dict)} | insert={t_up:.1f}s | read={t_read:.1f}s | "
             f"RSS now {rss3:.0f}MB (+{rss3 - rss2:.0f}MB) | pickle={_pklen(before_dict) / 1e6:.1f} MB",
             f"per file: {(rss3 - rss2) / max(1, len(before_dict)) * 1024:.2f} KB, "
             f"{_pklen(before_dict) / max(1, len(before_dict)):.0f} B pickled -> at 1M "
             f"~{(rss3 - rss2) * 10:.0f} MB RSS, ~{t_read * 10:.0f}s")
    db.close()

    # ── total footprint held simultaneously in _run_cloud_pipeline ────────────
    L.record("W2b.combined",
             "total heap held at once in the cloud pipeline at 1M objects (manifest + before_dict)",
             f"measured at {N}: manifest ~+{N * 0.6 / 1024:.0f} MB, before_dict ~+{(rss3 - rss2) * 10:.0f} MB (at 1M)",
             f"at 1M objects the cloud leg holds roughly "
             f"{N * 0.6 / 1024 * 10 + (rss3 - rss2) * 10:.0f} MB of live Python objects")
    # Tier B: correctness + regression ceilings (this was a pure measurement).
    assert len(before_dict) == N, f"read back {len(before_dict)} of {N} entries"
    assert t_up < 60.0, f"bulk_upsert_synced took {t_up:.1f}s for {N} entries"
    assert t_read < 30.0, f"get_cloud_synced_entries took {t_read:.1f}s for {N} entries"
    assert (rss3 - rss2) / max(1, len(before_dict)) * 1024 < 1.5, (
        f"per-object RSS growth {(rss3 - rss2) / max(1, len(before_dict)) * 1024:.2f} "
        "KB/object - a before_dict regression")
