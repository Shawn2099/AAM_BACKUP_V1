"""WAVE 26d - release card L6 and card T-b3 (docs/TEST_DESIGN_RELEASE_2026-11-22.md section B,
card L6; docs/PLAN_E2E_SIMULATION_2026-11-23.md section 1c, card T-b3).

L6  the source is swapped for a much smaller tree (real robocopy, real rclone): both legs must
    stop as *_BLOCKED (G2 shrink), exactly one approval per leg, and the NAS and the remote
    trees must be byte-identical (sha256) before and after the blocked runs.

T-b3 in the activated FY27-28 folder (small folder, under 100 files):
    night 1 (Jan 2): 9 files, both legs OK; night 2 (Jan 3): 5 deletes + 1 rename, both legs OK
    (5 deletes <= guards.small_folder_max_deletes 10); night 3 (Jan 4): no change, NO_CHANGES
    (cloud) and 0 files copied (LAN), trees equal to the source.
    variant: a 99-file folder (plus the placeholder), delete 11 -> both legs *_BLOCKED,
    one approval each, NAS and remote trees unchanged (sha256 before/after).

Oracles: the card's expected statuses, the approval table, and our own sha256 / mtime
snapshots of the destination trees. Never the product's own verdict as the expected value.

Rigs: L6 uses the WAVE 6 `rig` fixture (FY leaf FY26-27); T-b3 uses the WAVE 25 `lab`
fixture (activation of FY27-28 by the production flows). Both pin the clock through the labclock
seam (no system clock change) and simulate the Google date check (no real network).

Run with a clean Prefect home (CLAUDE.md "Environment gotchas"):
    PREFECT_HOME=<empty dir> .venv\\Scripts\\python.exe -m pytest audit_lab/test_w26d_guards.py -q -s -p no:cacheprovider -o addopts=""
"""
from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pendulum
import pytest

from audit_lab import labclock
from audit_lab import labconf as L
from audit_lab.test_w12_clock import _flow_call, _tree_map
from audit_lab.test_w25_fy_overlap import FY_B, lab  # noqa: F401  (fixture: production wiring)
from audit_lab.test_w6_flow import rig  # noqa: F401  (fixture: production wiring + local GCS)

pytestmark = pytest.mark.timeout(1800)

FY = "FY26-27"
_LEGS = ("lan", "cloud")
#: production modules whose own `pendulum` reference the fake clock must replace
_MODULES = labclock.CLOCK_MODULES + ("flow", "core.fy_overlap", "core.attention", "core.manifest",
                                      "core.run_artefacts")


def _at(y, mo, d, h=1, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz="Asia/Kolkata")


@contextlib.contextmanager
def _db(cfg_path: Path):
    from core.manifest import ManifestDB
    from models.config import load_config

    m = ManifestDB.from_config(load_config(str(cfg_path)))
    try:
        yield m
    finally:
        m.close()


def _run(fn, at, **kwargs):
    """One production flow call under the fake clock. Returns the raised exception or None."""
    exc = None
    with labclock.fake_clock(at, modules=_MODULES, tick=0.001):
        try:
            _flow_call(fn, **kwargs)
        except Exception as e:  # noqa: BLE001 - the caller decides what an exception means
            exc = e
    time.sleep(1.0)
    return exc


def _latest_status(cfg_path: Path, fy: str, leg: str) -> str | None:
    with _db(cfg_path) as m:
        rows = [r for r in m.get_recent_runs(5000, fy=fy) if r["mode"] == leg]
    return rows[0]["status"] if rows else None


def _open_approval_legs(cfg_path: Path, fy: str) -> list[str]:
    with _db(cfg_path) as m:
        return sorted(a["leg"] for a in m.open_approvals() if a["fy"] == fy)


def _mtimes(root: Path) -> dict[str, int]:
    """rel path -> mtime_ns for every file under root (the delivery marker skipped)."""
    return {p.relative_to(root).as_posix(): p.stat().st_mtime_ns
            for p in root.rglob("*") if p.is_file() and p.name != ".AAM_TARGET_MOUNTED"}


# ════════════════════════════════════════════════════════════════════════════════
# L6 - source swapped for a smaller tree (real robocopy, WAVE 6 rig)
# ════════════════════════════════════════════════════════════════════════════════
def test_w26d_l6_source_swapped_for_smaller_tree_is_blocked_on_both_legs(rig, monkeypatch):
    flow = rig["flow"]
    labclock.install_google_date(monkeypatch)
    cfg_path = rig["cfg_path"]
    src = rig["src"]
    remote_root = rig["buckets"] / "lab-bucket" / FY

    # the rig seeds 60 files; the card wants 100 in the tree that is backed up
    for i in range(60, 100):
        L.write_file(src / "Clients" / f"f_{i:04d}.txt", f"LEDGER {i}\n".encode() * 40)
    src_before = _tree_map(src)
    assert len(src_before) == 100, "the card's tree is 100 files"

    # 1. both legs COMPLETE: the baseline the guards compare against
    exc = _run(flow.backup, _at(2026, 11, 23, 12), config_path=str(cfg_path), mode="all")
    assert exc is None, f"the baseline backup must complete: {exc!r}"
    assert _latest_status(cfg_path, FY, "lan") == "LAN_COMPLETE"
    assert _latest_status(cfg_path, FY, "cloud") == "CLOUD_COMPLETE"
    nas_before = _tree_map(rig["nas"])
    remote_before = _tree_map(remote_root)
    assert nas_before == src_before, "NAS must hold the 100 files before the swap"
    assert remote_before == src_before, "remote must hold the 100 files before the swap"

    # 2. the swap: the real tree is moved aside, a new FY26-27 folder with 5 files is created
    src.rename(src.parent / "FY26-27.real")
    for i in range(5):
        L.write_file(src / "Clients" / f"small_{i}.txt", f"small {i}\n".encode() * 10)
    assert len(_tree_map(src)) == 5

    # 3. LAN then cloud: both must refuse to transfer
    exc_lan = _run(flow.backup, _at(2026, 11, 23, 13), config_path=str(cfg_path), mode="lan")
    exc_cloud = _run(flow.backup, _at(2026, 11, 23, 14), config_path=str(cfg_path), mode="cloud")
    assert exc_lan is not None, "the LAN run must not succeed on a 95 % shrink"
    assert exc_cloud is not None, "the cloud run must not succeed on a 95 % shrink"
    assert _latest_status(cfg_path, FY, "lan") == "LAN_BLOCKED"
    assert _latest_status(cfg_path, FY, "cloud") == "CLOUD_BLOCKED"

    # one approval per leg, for this FY
    assert _open_approval_legs(cfg_path, FY) == ["cloud", "lan"], _open_approval_legs(cfg_path, FY)

    # 4. nothing was written: NAS and remote hashes identical to the snapshot before the swap
    assert _tree_map(rig["nas"]) == nas_before, "the NAS tree changed under a blocked backup"
    assert _tree_map(remote_root) == remote_before, "the remote tree changed under a blocked backup"

    L.record("W26d.L6_source_swapped_smaller_tree",
             "a source swapped for a 5-file tree (from 100) is refused by both legs before any transfer",
             f"baseline LAN_COMPLETE + CLOUD_COMPLETE | swap to 5 files | lan={_latest_status(cfg_path, FY, 'lan')} "
             f"cloud={_latest_status(cfg_path, FY, 'cloud')} | approvals per leg={_open_approval_legs(cfg_path, FY)} | "
             "NAS and remote sha256 identical before/after",
             "OK - both legs BLOCKED, one approval each, destination trees untouched")


# ════════════════════════════════════════════════════════════════════════════════
# T-b3 - deletes in the activated FY27-28 folder (WAVE 25 lab)
# ════════════════════════════════════════════════════════════════════════════════
def _night(lab, at, mode: str):
    """One production nightly flow for one leg under the fake clock. Returns the raised exception or None."""
    exc = None
    with lab.clock(at):
        try:
            _flow_call(lab.flow.backup, config_path=str(lab.cfg_path), mode=mode)
        except Exception as e:  # noqa: BLE001 - blocked runs raise by design
            exc = e
    time.sleep(1.0)
    return exc


def _leg_status(lab, fy: str, leg: str) -> str | None:
    rows = [r for r in lab.legs(fy) if r["mode"] == leg]
    return rows[0]["status"] if rows else None


def _approval_legs(lab, fy: str) -> list[str]:
    with lab.db() as (_c, m):
        return sorted(a["leg"] for a in m.open_approvals() if a["fy"] == fy)


def _activate_fy_b(lab) -> None:
    """1 Jan 2027 02:00: the production flows activate FY27-28 (placeholder + NAS marker + cloud prefix)."""
    lab.backup(_at(2027, 1, 1, 2))
    with lab.db() as (_c, m):
        assert m.get_activation(FY_B) is not None, "FY27-28 was not activated"
    assert (lab.src(FY_B) / "README_FY.txt").is_file(), "the placeholder is missing"


def test_w26d_tb3_three_nights_with_deletes_in_a_small_folder(lab):
    _activate_fy_b(lab)
    for i in range(9):
        L.write_file(lab.src(FY_B) / "Clients" / f"c_{i:03d}.txt", f"{FY_B} client {i}\n".encode() * 25)

    # night 1 (2 Jan): first copy of the nine files, cloud 02:00 and LAN 21:00
    assert _night(lab, _at(2027, 1, 2, 2), "cloud") is None
    assert _leg_status(lab, FY_B, "cloud") == "CLOUD_COMPLETE"
    assert _night(lab, _at(2027, 1, 2, 21), "lan") is None
    assert _leg_status(lab, FY_B, "lan") == "LAN_COMPLETE"
    lab.converged(FY_B, "T-b3 night 1 (2 Jan)")

    # night 2 (3 Jan): 5 deletes and 1 rename (the old name is one more delete: 6 <= 10)
    clients = lab.src(FY_B) / "Clients"
    for i in range(5):
        (clients / f"c_{i:03d}.txt").unlink()
    (clients / "c_005.txt").rename(clients / "c_005_renamed.txt")
    assert _night(lab, _at(2027, 1, 3, 2), "cloud") is None
    assert _leg_status(lab, FY_B, "cloud") == "CLOUD_COMPLETE"
    assert _night(lab, _at(2027, 1, 3, 21), "lan") is None
    assert _leg_status(lab, FY_B, "lan") == "LAN_COMPLETE"
    lab.converged(FY_B, "T-b3 night 2 (3 Jan)")

    # night 3 (4 Jan): no change at all
    nas_mt = _mtimes(lab.nas(FY_B))
    cloud_mt = _mtimes(lab.cloud(FY_B))
    assert _night(lab, _at(2027, 1, 4, 2), "cloud") is None
    assert _leg_status(lab, FY_B, "cloud") == "CLOUD_NO_CHANGES_COMPLETE"
    assert _night(lab, _at(2027, 1, 4, 21), "lan") is None
    assert _leg_status(lab, FY_B, "lan") == "LAN_COMPLETE"
    lab.converged(FY_B, "T-b3 night 3 (4 Jan)")
    # 0 copied: no destination file was rewritten by the LAN night (mtimes unchanged)
    assert _mtimes(lab.nas(FY_B)) == nas_mt, "the LAN night rewrote files although nothing changed"
    assert _mtimes(lab.cloud(FY_B)) == cloud_mt, "the cloud night rewrote objects although nothing changed"

    assert _approval_legs(lab, FY_B) == [], "an approval was opened by a within-limit night"

    L.record("W26d.T-b3_small_folder_three_nights",
             "deletes inside the small-folder allowance pass on both legs; a no-change night copies nothing",
             f"night1 cloud/lan=CLOUD_COMPLETE/LAN_COMPLETE | night2 (5 deletes + 1 rename) CLOUD_COMPLETE/LAN_COMPLETE "
             f"| night3 CLOUD_NO_CHANGES_COMPLETE/LAN_COMPLETE, mtimes unchanged | NAS and remote == source (sha256)",
             "OK - small-folder deletes within the allowance are accepted; the no-change night is a no-op")


def test_w26d_tb3_99_file_folder_blocks_eleven_deletes_on_both_legs(lab):
    _activate_fy_b(lab)
    for i in range(99):
        L.write_file(lab.src(FY_B) / "Clients" / f"c_{i:03d}.txt", f"{FY_B} client {i}\n".encode() * 20)

    # the first copy of the 99 files plus the placeholder, both legs OK
    assert _night(lab, _at(2027, 1, 2, 2), "cloud") is None
    assert _night(lab, _at(2027, 1, 2, 21), "lan") is None
    lab.converged(FY_B, "T-b3 99-file folder, first copy")

    # 11 deletes: over the 10 % of 100 destination files, and over the small-folder allowance of 10
    clients = lab.src(FY_B) / "Clients"
    for i in range(11):
        (clients / f"c_{i:03d}.txt").unlink()
    nas_before = _tree_map(lab.nas(FY_B))
    remote_before = _tree_map(lab.cloud(FY_B))
    assert len(nas_before) >= 100, "the destination must hold the 99 files plus the placeholder"

    exc_cloud = _night(lab, _at(2027, 1, 3, 2), "cloud")
    exc_lan = _night(lab, _at(2027, 1, 3, 21), "lan")
    assert exc_cloud is not None, "the cloud night must be refused at 11 deletes"
    assert exc_lan is not None, "the LAN night must be refused at 11 deletes"
    assert _leg_status(lab, FY_B, "cloud") == "CLOUD_BLOCKED"
    assert _leg_status(lab, FY_B, "lan") == "LAN_BLOCKED"

    assert _approval_legs(lab, FY_B) == ["cloud", "lan"], "one approval per leg is expected"
    assert _tree_map(lab.nas(FY_B)) == nas_before, "the NAS tree changed under a blocked night"
    assert _tree_map(lab.cloud(FY_B)) == remote_before, "the remote tree changed under a blocked night"

    L.record("W26d.T-b3_99_file_variant_blocked",
             "a 99-file folder losing 11 files is refused on both legs; one approval per leg; no tree change",
             f"lan={_leg_status(lab, FY_B, 'lan')} cloud={_leg_status(lab, FY_B, 'cloud')} | "
             f"approvals={_approval_legs(lab, FY_B)} | NAS and remote sha256 identical before/after",
             "OK - the 11-delete night is blocked on both legs, no destination change")
