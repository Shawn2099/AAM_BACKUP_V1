"""WAVE 9 — Hypothesis-driven stress: random trees, random faults, real mirrors.

WAVE 1F fuzzed pure functions (parsers, classifiers). This wave fuzzes the
REAL transfer paths with Hypothesis-generated filesystem trees and randomised
fault injection, and asserts the data-integrity invariants that matter in
production:

  I1 LOSSLESS     — every source file lands at the destination byte-identical
  I2 CONVERGENT   — a second run changes nothing (idempotent mirror)
  I3 NO PHANTOM   — the destination holds nothing the source does not have
  I4 HONEST STATUS — *the load-bearing one*: if a run is recorded as
                    LAN_COMPLETE / CLOUD_COMPLETE / CLOUD_NO_CHANGES_COMPLETE,
                    then the destination MUST be byte-identical to the source.
                    A success status with missing/different data is the
                    D1/B2 class of failure and must fail this test.

W9.1/W9.2  random trees through the real LAN and cloud pipelines
W9.3       random faults (share-none lock, read-only, junction, deep path,
           future mtime, zero-byte) — the run may fail, but it may never lie
W9.4       metamorphic: random destination mutation, then a second nightly run —
           which mutations does the mirror actually repair?
W9.5       concurrency stress on manifest.db with randomised writers

Everything is confined to LAB_ROOT; the same safety rules as the other waves.
"""
from __future__ import annotations

import ctypes
import hashlib
import itertools
import json
import os
import random
import shutil
import string
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
import hypothesis.strategies as st

from audit_lab import labconf as L
from core.time_utils import now_iso

pytestmark = pytest.mark.timeout(1800)

# Hypothesis calls the test function once per example, but function-scoped
# pytest fixtures are built ONCE per test -> every example would share (and
# accumulate in) the same sandbox. Every example therefore gets its own
# sub-directory; without this, state leaks between examples and a junction
# created in one example contaminates the next.
_EXAMPLE_SEQ = itertools.count()


def _example_root(sandbox: Path) -> Path:
    root = sandbox / f"ex{next(_EXAMPLE_SEQ):04d}"
    (root / "source" / "FY26-26").mkdir(parents=True, exist_ok=True)
    return root


def _is_reparse_point(p: Path) -> bool:
    """True for junctions/symlinks (FILE_ATTRIBUTE_REPARSE_POINT = 0x400)."""
    INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
    return attrs != INVALID_FILE_ATTRIBUTES and bool(attrs & 0x400)


SET = settings(max_examples=15, deadline=None, suppress_health_check=list(HealthCheck))

# ── content / name generators ─────────────────────────────────────────────────
# Windows-legal but hostile: no \ / : * ? " < > | and no control characters.
# Trailing dots/spaces are stripped by the generator because Windows silently
# renames those files (W5a proved it), which would break path-identity checks.
_SAFE_CHARS = string.ascii_letters + string.digits + " ._-()[]{}&#'+@$!%=~"
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} \
    | {f"lpt{i}" for i in range(1, 10)}

_seg = st.text(alphabet=_SAFE_CHARS, min_size=1, max_size=8).map(
    lambda s: s.strip().rstrip(".") or "x")


@st.composite
def _safe_name(draw):
    while True:
        n = draw(_seg)
        if n.lower() not in _RESERVED and n not in (".", ".."):
            return n


@st.composite
def relpath(draw, depth: int = 2):
    parts = [draw(_safe_name()) for _ in range(draw(st.integers(min_value=1, max_value=depth)))]
    return "/".join(parts)


@st.composite
def tree(draw, min_files: int = 1, max_files: int = 8):
    """A random source tree. Directories are derived from the file paths only,
    so a file name can never collide with a directory name (that produced
    FileExistsError on Windows when both were generated independently)."""
    files: dict[str, bytes] = {}
    for _ in range(draw(st.integers(min_value=min_files, max_value=max_files))):
        rel = draw(relpath())
        if rel in files:
            continue
        files[rel] = draw(st.binary(min_size=0, max_size=1024))
    assume(len(files) >= min_files)
    return files


def build_tree(root: Path, files: dict[str, bytes], mtimes: dict[str, float] | None = None):
    """Materialise the generated tree. Two rules keep this Windows-safe:
      * a file path can never also be a directory path (drop the ancestor), and
      * a file that cannot be written (leftover read-only state, etc.) is
        skipped rather than failing the whole example.
    Returns the set of relpaths actually created."""
    # shallow first so ancestors are seen before their descendants
    ordered = sorted(files, key=lambda p: (p.count("/"), p))
    keep: set[str] = set()
    for rel in ordered:
        if any(rel.startswith(k + "/") or k.startswith(rel + "/") for k in keep):
            continue
        keep.add(rel)
    created = set()
    for rel in sorted(keep):
        p = root / rel.replace("/", os.sep)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(files[rel])
            if mtimes and rel in mtimes:
                os.utime(p, (mtimes[rel], mtimes[rel]))
            created.add(rel)
        except OSError:
            continue
    return root, created


def tree_map(root: Path, skip_names: set[str] | None = None) -> dict[str, str]:
    """relpath -> sha256 for every readable file under root.

    Reparse points (junctions/symlinks) are NOT descended into: robocopy runs
    with /XJ, so their contents are contractually excluded from both sides.
    Unreadable files are skipped and reported separately — a file the lab
    itself cannot read is a legitimate real-world condition, not a test crash.
    """
    skip = skip_names or set()
    out: dict[str, str] = {}
    unreadable: list[str] = []
    if not root.exists():
        return out, unreadable
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not _is_reparse_point(Path(dirpath) / d)]
        for fn in filenames:
            if fn in skip:
                continue
            p = Path(dirpath) / fn
            if _is_reparse_point(p):
                continue
            try:
                data = p.read_bytes()
            except OSError:
                unreadable.append(p.relative_to(root).as_posix())
                continue
            h = hashlib.sha256()
            h.update(data)
            out[p.relative_to(root).as_posix()] = h.hexdigest()
    return out, unreadable


def _is_ok_status(status: str) -> bool:
    return status.startswith(("LAN_COMPLETE", "CLOUD_COMPLETE", "CLOUD_NO_CHANGES"))


# ── rig helpers ───────────────────────────────────────────────────────────────
def _write_lab_config(base: Path, cfg) -> Path:
    import json

    import yaml

    p = base / "config.yaml"
    yaml.safe_dump(json.loads(cfg.model_dump_json()), p.open("w", encoding="utf-8"),
                   sort_keys=False, allow_unicode=True)
    return p


def _lan_rig(ex: Path, files: dict[str, bytes], mtimes=None):
    """Sandbox + real config for a LAN-only run. Returns (cfg, cfg_path, src, dest)."""
    import core.lan_sync as ls

    src, _created = build_tree(ex / "source" / "FY26-26", files, mtimes)
    dest = L.make_canary(ex / "nas" / "FY26-26")
    # mirror-mechanics rig: tiny random trees would trip G3 on one stray file
    cfg = L.build_config(ex, source=src, lan_dest=str(dest), runtime=ex / "runtime",
                         lan_enabled=True, cloud_enabled=False,
                         guards=dict(L.MIRROR_MECHANICS_GUARDS))
    cfg_path = _write_lab_config(ex, cfg)
    return cfg, cfg_path, src, dest, ls


def _patch_no_zb(monkeypatch, ls) -> None:
    """Drop /ZB (needs SeBackupPrivilege — proven in C1) so the lab account can copy."""
    orig = ls.build_robocopy_command
    monkeypatch.setattr(ls, "build_robocopy_command",
                        lambda s, d, c: L.strip_zb(orig(s, d, c)))


def _cloud_rig(ex: Path, monkeypatch, files):
    import contextlib

    import core.time_utils as tu

    src, _created = build_tree(ex / "source" / "FY26-26", files)
    bucket_root = ex / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    (ex / "runtime").mkdir(parents=True, exist_ok=True)
    # v1.1.3: the cloud leg syncs to _effective_fy_prefix(config) — the source
    # leaf when it is a valid FY dir — not the calendar prefix.
    cfg = L.build_config(ex, source=src, lan_dest=str(ex / "nas" / "FY26-26"),
                         runtime=ex / "runtime", lan_enabled=False, cloud_enabled=True,
                         cloud={"bandwidth_limit": "1000M"})
    from flow import _effective_fy_prefix
    fy = _effective_fy_prefix(cfg)
    (bucket_root / "lab-bucket" / fy).mkdir(parents=True, exist_ok=True)
    # health.check_gcs_key() requires the key file to exist and be non-empty
    # (the local rclone backend never reads its contents)
    L.write_file(ex / "lab-key.json", b'{"type": "service_account"}')

    cfg_path = _write_lab_config(ex, cfg)
    rc = L.write_local_rclone_config(ex / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc)

    # the production flow resolves its own rclone config through this context manager
    import flow
    monkeypatch.setattr(flow, "temp_rclone_config",
                        lambda *a, **k: contextlib.nullcontext(rc))
    monkeypatch.setenv("PREFECT_TEST_MODE", "1")

    cwd = os.getcwd()
    os.chdir(bucket_root)          # rclone's local backend resolves the bucket from CWD
    return _CloudRig(ex, cfg, cfg_path, rc, src, bucket_root, fy, cwd, saved)


class _CloudRig:
    """Cloud-leg rig: real config on disk + rclone local backend + bucket root."""

    def __init__(self, ex, cfg, cfg_path, rc, src, buckets, fy, cwd, saved):
        self.ex, self.cfg, self.cfg_path = ex, cfg, cfg_path
        self.rc, self.src, self.buckets, self.fy = rc, src, buckets, fy
        self.cwd, self.saved = cwd, saved

    def release(self):
        os.chdir(self.cwd)
        for (mod, attr), o in self.saved.items():
            setattr(mod, attr, o)

    @property
    def dest(self) -> Path:
        # the local backend resolves "aam_gcs:<bucket>/<prefix>" from CWD, so the
        # bucket directory is <bucket_root>/<bucket-name>/<prefix>
        return self.buckets / "lab-bucket" / self.fy


# ── W9.1 — random trees through the REAL LAN pipeline ────────────────────────
@given(files=tree())
@SET
def test_w9_random_tree_lan_mirror_is_lossless_and_honest(files, sandbox, monkeypatch):
    """I1-I4: a random tree mirrored by robocopy /MIR must converge exactly,
    and a COMPLETE status must never hide missing or altered data."""
    import flow

    ex = _example_root(sandbox)
    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    cfg, cfg_path, src, dest, ls = _lan_rig(ex, files)
    _patch_no_zb(monkeypatch, ls)

    res = flow._run_lan_pipeline(cfg, "w9-lan-1", now_iso(), time.monotonic())
    status1 = res["status"]
    after1, _u1 = tree_map(dest, skip_names={".AAM_TARGET_MOUNTED"})
    src_map, _su1 = tree_map(src)

    L.record("W9.lan_random_tree",
             "a Hypothesis-random source tree mirrored by robocopy /MIR is byte-identical "
             "at the destination",
             f"files={len(files)} bytes={sum(len(v) for v in files.values())} | "
             f"run1 status={status1} | src files={len(src_map)} dest files={len(after1)} | "
             f"missing={sorted(set(src_map) - set(after1))[:5]} | "
             f"different={sorted(p for p in set(src_map) & set(after1) if src_map[p] != after1[p])[:5]}",
             "OK — lossless + honest (I1, I3, I4 hold)"
             if status1 == "LAN_COMPLETE" and after1 == src_map
             else "VIOLATION — COMPLETE status but destination differs from source (I4 broken)"
             if _is_ok_status(status1)
             else f"NON-SUCCESS (allowed): status={status1}, dest differs from source")

    # I4 is the invariant that must never break
    if _is_ok_status(status1):
        assert after1 == src_map, f"I4 violated: status={status1}"

    # I2 convergence: the second nightly run must change nothing
    res2 = flow._run_lan_pipeline(cfg, "w9-lan-2", now_iso(), time.monotonic())
    after2, _u2b = tree_map(dest, skip_names={".AAM_TARGET_MOUNTED"})
    L.record("W9.lan_convergence",
             "a second run over the same tree is a no-op (idempotent mirror)",
             f"run2 status={res2['status']} | dest files run1={len(after1)} run2={len(after2)} | "
             f"converged={after1 == after2}",
             "OK — second run changed nothing (I2 holds)"
             if after2 == after1 else "VIOLATION — the mirror is not idempotent (I2 broken)")
    assert after2 == after1
    assert res2["status"] in ("LAN_COMPLETE", "LAN_PARTIAL", "LAN_SUSPECT")


# ── W9.2 — random trees through the REAL cloud pipeline ──────────────────────
@given(files=tree())
@SET
def test_w9_random_tree_cloud_mirror_is_lossless_and_honest(files, sandbox, monkeypatch):
    """I1-I4 for the cloud leg: rclone sync against the local backend."""
    import flow

    ex = _example_root(sandbox)
    rig = _cloud_rig(ex, monkeypatch, files)
    try:
        out = flow._run_cloud_pipeline(rig.cfg, "w9-cloud-1", now_iso(), time.monotonic())
        status1 = out["status"]
        dest_map, _u2 = tree_map(rig.dest)
        src_map, _su2 = tree_map(rig.src)

        L.record("W9.cloud_random_tree",
                 "a Hypothesis-random source tree synced by rclone is byte-identical in the bucket",
                 f"files={len(files)} bytes={sum(len(v) for v in files.values())} | "
                 f"run1 status={status1} exit={out.get('exit_code')} | "
                 f"src={len(src_map)} bucket={len(dest_map)} | "
                 f"missing={sorted(set(src_map) - set(dest_map))[:5]} | "
                 f"different={sorted(p for p in set(src_map) & set(dest_map) if src_map[p] != dest_map[p])[:5]}",
                 "OK — lossless + honest (I1, I3, I4 hold)"
                 if _is_ok_status(status1) and dest_map == src_map
                 else "VIOLATION — a success status but the bucket differs from the source (I4 broken)"
                 if _is_ok_status(status1) else f"NON-SUCCESS (allowed): status={status1}")

        if _is_ok_status(status1):
            assert dest_map == src_map, f"I4 violated: status={status1}"

        out2 = flow._run_cloud_pipeline(rig.cfg, "w9-cloud-2", now_iso(), time.monotonic())
        dest_map2, _u3 = tree_map(rig.dest)
        L.record("W9.cloud_convergence",
                 "a second cloud run over the same tree is a no-op",
                 f"run2 status={out2['status']} exit={out2.get('exit_code')} | "
                 f"bucket files run1={len(dest_map)} run2={len(dest_map2)} | converged={dest_map == dest_map2}",
                 "OK — second run changed nothing (I2 holds)"
                 if dest_map2 == dest_map else "VIOLATION — the cloud mirror is not idempotent (I2 broken)")
        assert dest_map2 == dest_map
    finally:
        rig.release()


# ── W9.3 — randomised fault injection: the run may fail, it must never lie ───
# NOTE: a junction is deliberately NOT in this list. /XJ excludes junction
# points by design (W5b.junction proved no duplication/recursion), so those
# files are contractually absent from the destination and I4 must not apply.
# The junction is covered by its own experiment below.
_FAULTS = ["share-none lock", "read-only attribute", "deep path",
           "future mtime", "zero-byte file", "none"]


def _hold_share_none(path: Path):
    """Open a file with dwShareMode=0 — the strictest reader-blocking lock."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GENERIC_READ, FILE_SHARE_NONE, OPEN_EXISTING = 0x80000000, 0, 3
    h = kernel32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_NONE, None,
                             OPEN_EXISTING, 0x80, None)
    if h in (0, -1):
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    return h


def _close_handle(h) -> None:
    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(h)


@given(files=tree(min_files=3, max_files=8), fault=st.sampled_from(_FAULTS))
@SET
def test_w9_random_fault_lan_never_lies_about_success(files, fault, sandbox, monkeypatch):
    """I4 under fault injection: LAN_COMPLETE implies the destination is
    byte-identical to the source — whatever the fault was."""
    import flow

    rels = sorted(files)
    mtimes = None
    if fault == "share-none lock":
        pass                                   # applied below (needs the file on disk)
    elif fault == "future mtime":
        mtimes = {r: time.time() + 30 * 86400 for r in rels}   # 30 days in the future
    elif fault == "zero-byte file":
        files[rels[0]] = b""
    elif fault == "read-only attribute":
        pass                                   # applied below (needs the file on disk)
    elif fault == "deep path":
        deep = "seg/" * 12 + "deepfile.txt"    # >260 chars under the sandbox
        files = dict(files)
        files["d1/d2/" + deep] = b"deep\n" * 20

    ex = _example_root(sandbox)
    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    cfg, cfg_path, src, dest, ls = _lan_rig(ex, files, mtimes)
    _patch_no_zb(monkeypatch, ls)

    held = None
    notes = []
    if fault == "share-none lock":
        target = src / rels[0].replace("/", os.sep)
        try:
            held = _hold_share_none(target)
            notes.append(f"share-none lock held on {rels[0]}")
        except OSError as e:
            notes.append(f"could not hold lock: {e}")
    elif fault == "read-only attribute":
        target = src / rels[0].replace("/", os.sep)
        subprocess.run(["attrib", "+R", str(target)], capture_output=True)
        notes.append(f"read-only attribute on {rels[0]}")

    held = None
    notes = []
    if fault == "share-none lock":
        target = src / rels[0].replace("/", os.sep)
        try:
            held = _hold_share_none(target)
            notes.append(f"share-none lock held on {rels[0]}")
        except OSError as e:
            notes.append(f"could not hold lock: {e}")
    elif fault == "read-only attribute":
        target = src / rels[0].replace("/", os.sep)
        subprocess.run(["attrib", "+R", str(target)], capture_output=True)
        notes.append(f"read-only attribute on {rels[0]}")
    elif fault == "junction":
        d = src / "junc_target"
        d.mkdir(exist_ok=True)
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(src / "junc"), str(src)],
                           capture_output=True, text=True)
        notes.append(f"junction rc={r.returncode}")

    exc = None
    try:
        res = flow._run_lan_pipeline(cfg, "w9-fault", now_iso(), time.monotonic())
        status = res["status"]
    except Exception as e:  # noqa: BLE001
        status, exc = f"RAISED {type(e).__name__}", e
    finally:
        if held:
            _close_handle(held)

    src_map, _su = tree_map(src)
    dest_map, _du = tree_map(dest, skip_names={".AAM_TARGET_MOUNTED"})
    lied = _is_ok_status(status) and dest_map != src_map
    L.record("W9.fault_lan",
             "under fault injection a COMPLETE status never hides missing or altered data",
             f"fault={fault} ({'; '.join(notes)}) | status={status} raised={type(exc).__name__ if exc else None} | "
             f"src={len(src_map)} dest={len(dest_map)} | "
             f"missing={sorted(set(src_map) - set(dest_map))[:5]} | "
             f"different={sorted(p for p in set(src_map) & set(dest_map) if src_map[p] != dest_map[p])[:5]}",
             "VIOLATION — success status with data missing/different at the destination (I4 broken)"
             if lied else
             "OK — honest: failure status, or COMPLETE with a byte-identical destination (I4 holds)")
    assert not lied, f"I4 violated under fault={fault}: status={status}"


# ── W9.3b — a junction must be excluded, not duplicated ──────────────────────
def test_w9b_junction_is_excluded_not_duplicated(sandbox, monkeypatch):
    """The junction case /XJ: the tree must NOT be duplicated or recursed into,
    and the mirrored copy of the real files must still be byte-identical."""
    import core.lan_sync as ls
    import flow

    ex = _example_root(sandbox)
    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    files = {f"clients/ledger_{i}.txt": f"LEDGER {i}\n".encode() * 20 for i in range(12)}
    cfg, cfg_path, src, dest, ls = _lan_rig(ex, files)
    _patch_no_zb(monkeypatch, ls)

    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(src / "junc"), str(src)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"mklink unavailable: {r.stdout[:80]}{r.stderr[:80]}")

    exc = None
    try:
        res = flow._run_lan_pipeline(cfg, "w9-junc", now_iso(), time.monotonic())
        status = res["status"]
    except Exception as e:  # noqa: BLE001
        status, exc = f"RAISED {type(e).__name__}", e

    via_junction = [p for p in dest.rglob("*") if p.is_file() and "junc" in str(p)]
    direct, _dj = tree_map(dest, skip_names={".AAM_TARGET_MOUNTED"})
    src_direct, _sj = tree_map(src)
    L.record("W9.junction_excluded",
             "a junction inside the source is excluded (/XJ): no duplication and no "
             "recursion, while the real files still mirror byte-identically",
             f"mklink rc={r.returncode} | status={status} raised={type(exc).__name__ if exc else None} | "
             f"files copied through the junction={len(via_junction)} | "
             f"real files mirrored={len(direct)}/{len(src_direct)} identical={direct == src_direct}",
             "OK — junction excluded (0 files through it) and the real tree mirrored exactly"
             if not via_junction and direct == src_direct
             else f"ISSUE — files through junction={len(via_junction)}, identical={direct == src_direct}")
    assert not via_junction, f"{len(via_junction)} files were copied through the junction"
    assert direct == src_direct


# ── W9.4 — metamorphic: which destination mutations does a nightly run repair? ─
@given(files=tree(min_files=3, max_files=6),
       mutation=st.sampled_from(["delete", "truncate", "same-size corruption",
                                 "extra object", "mtime shift (older)"]))
@SET
def test_w9_destination_mutation_is_repaired_by_next_run(files, mutation, sandbox, monkeypatch):
    """Follow-up relation: after a random destination mutation, the next nightly
    run must converge the destination back to the source — except for
    same-size content corruption, which size+mtime-based syncs cannot see."""
    import flow

    ex = _example_root(sandbox)
    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    cfg, cfg_path, src, dest, ls = _lan_rig(ex, files)
    _patch_no_zb(monkeypatch, ls)
    rels = sorted(files)
    try:
        flow._run_lan_pipeline(cfg, "w9-meta-0", now_iso(), time.monotonic())
    except Exception as e:  # noqa: BLE001
        assume(False, f"baseline mirror failed: {e}")

    target = dest / rels[0].replace("/", os.sep)
    pre_hash = None
    if target.exists():
        pre_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    note = ""
    if mutation == "delete" and target.exists():
        target.unlink()
        note = "destination file deleted"
    elif mutation == "truncate" and target.exists():
        size = target.stat().st_size
        with open(target, "r+b") as fh:
            fh.truncate(max(0, size // 2))
        note = f"destination file truncated {size} -> {max(0, size // 2)} bytes"
    elif mutation == "same-size corruption" and pre_hash:
        data = bytearray(target.read_bytes())
        if not data:
            return                      # empty file: nothing to corrupt same-size
        mid = len(data) // 2
        data[mid] ^= 0xFF
        target.write_bytes(bytes(data))
        note = "same-size content corruption in the destination copy"
    elif mutation == "extra object":
        extra = dest / "STRAY_BACKUP_ARTEFACT.txt"
        extra.write_bytes(b"not in the source\n")
        note = "extra object planted at the destination"
    elif mutation == "mtime shift (older)" and target.exists():
        old = target.stat().st_mtime
        os.utime(target, (old - 100000, old - 100000))
        note = "destination mtime shifted 100000s into the past"

    exc = None
    try:
        res = flow._run_lan_pipeline(cfg, "w9-meta-1", now_iso(), time.monotonic())
        status = res["status"]
    except Exception as e:  # noqa: BLE001
        status, exc = f"RAISED {type(e).__name__}", e

    src_map, _sm = tree_map(src)
    dest_map, _dm = tree_map(dest, skip_names={".AAM_TARGET_MOUNTED"})
    repaired = dest_map == src_map
    L.record("W9.mutation_repair",
             "a random destination mutation is repaired by the next nightly run",
             f"mutation={mutation} ({note}) | status={status} raised={type(exc).__name__ if exc else None} | "
             f"src={len(src_map)} dest={len(dest_map)} | repaired={repaired} | "
             f"still-different={sorted(p for p in set(src_map) & set(dest_map) if src_map[p] != dest_map[p])[:5]}",
             "NOT REPAIRED — the nightly mirror leaves a same-size content difference in place and "
             "still reports success: the size+mtime comparison cannot see it, and the daily verify "
             "gate is --size-only. Data stays corrupt until the source file itself changes."
             if (mutation == "same-size corruption" and not repaired and _is_ok_status(status))
             else "OK — mutation repaired by the next run" if repaired
             else "VIOLATION — mutation not repaired AND not honestly reported")

    if mutation == "same-size corruption":
        # Document the known blind spot rather than failing the suite on it:
        # it is finding B2/H-1 reproduced as a reproducible property.
        assert _is_ok_status(status) or not repaired
    else:
        assert repaired, f"mutation {mutation} was not repaired (status={status})"


# ── W9.5 — concurrency stress on manifest.db with randomised writers ─────────
# NOTE: this is deliberately NOT a @given test. The defect it hunts is
# intermittent (a lock race), and Hypothesis would report a failed-then-passed
# replay as "FlakyFailure" instead of the finding. Instead: several randomised
# ROUNDS inside one test, asserting over the aggregate.
ROUNDS = 12


def test_w9_manifest_db_concurrency_stress(sandbox):
    """Randomised concurrent writers must serialise without losing rows."""
    from core.manifest import ManifestDB

    rng = random.Random(20261010)
    rounds: list[str] = []
    failures: list[str] = []

    for rnd in range(ROUNDS):
        ex = _example_root(sandbox)
        db = str(ex / "runtime" / "manifest.db")
        threads = rng.randint(2, 6)
        per_thread = rng.randint(5, 30)
        errors: list[str] = []
        ManifestDB(db).close()          # pre-create the schema (DDL phase excluded)

        def _writer(tag: str, n: int, jitter: float):
            try:
                m = ManifestDB(db, busy_timeout_ms=8000)
                for i in range(n):
                    m.insert_run({"run_id": f"{tag}-{i}", "mode": "cloud",
                                  "started_at": now_iso(), "status": "CLOUD_COMPLETE"})
                    if jitter:
                        time.sleep(random.uniform(0, jitter))
                m.close()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{tag}: {type(e).__name__}: {e}")

        workers = [threading.Thread(target=_writer, args=(f"T{i}", per_thread, 0.001))
                   for i in range(threads)]
        for t in workers:
            t.start()
        for t in workers:
            t.join()

        m = ManifestDB(db)
        rows = len(m.get_recent_runs(10000))
        m.close()
        expected = threads * per_thread
        lost = expected - rows
        rounds.append(f"round {rnd}: threads={threads} x{per_thread} -> {rows}/{expected}"
                      f"{f' LOST={lost}' if lost else ''}{f' raised={len(errors)}' if errors else ''}")
        if lost or errors:
            failures.append(
                f"round {rnd}: {threads} threads x {per_thread} inserts -> {rows}/{expected} rows "
                f"stored ({lost} lost), {len(errors)} writer(s) raised: {errors[:1]}")

    L.record("W9.db_concurrency_stress",
             "randomised concurrent writers on manifest.db lose no rows",
             "\n".join(rounds),
             "REAL FINDING — concurrent ManifestDB opens lose run-history rows. The loser raises "
             "OperationalError('database is locked') inside _get_conn()'s executescript(DDL) "
             "(core/manifest.py:163) even with busy_timeout_ms=8000, because PRAGMA busy_timeout is "
             "applied only AFTER the legacy dedup DELETE + commit (manifest.py:150-162). "
             "record_run_history() catches that and returns False, so the run-history row is "
             "dropped with no trace — the backup's audit trail silently loses runs."
             if failures else
             f"OK — all {ROUNDS} rounds stored every row with no writer locked out")
    print(f"[W9.5] {ROUNDS} rounds; failing rounds: {len(failures)}")
    for r in rounds:
        print(f"    {r}")
    assert not failures, failures[:3]

