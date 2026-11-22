"""WAVE 26c - release data cards L4 and L5 (docs/TEST_DESIGN_RELEASE_2026-11-22.md section B;
plan docs/PLAN_E2E_SIMULATION_2026-11-23.md section 1c).

L4  a file that grows while the cloud leg uploads it.
L5  a file that is exclusively locked (share mode 0) while the cloud leg reads it.

Both run through the PRODUCTION flow (flow.backup(mode="cloud")) on the WAVE 6 rig: real rclone against a
`type = local` remote, the bucket being the rig's buckets/lab-bucket directory. Expected values come from the
cards only.

Run (lab rules, CLAUDE.md "Commands" and "Environment gotchas"):
    PREFECT_HOME=<empty dir> .venv\\Scripts\\python.exe -m pytest audit_lab/test_w26c_upload.py -q -s -p no:cacheprovider -o addopts=""
"""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
import win32file

from audit_lab import labconf as L
from audit_lab.test_w6_flow import _db_rows, rig  # noqa: F401 - the production-wired rig fixture

pytestmark = pytest.mark.timeout(1200)

CLOUD_COMPLETE = ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE")
CLOUD_NOT_OK = ("CLOUD_SUSPECT", "CLOUD_PARTIAL")       # card L4 / L5: "run 1 is PARTIAL/SUSPECT"
MIB = 1 << 20


def _chunk(n: int) -> bytes:
    """One deterministic 1 MiB block (n only changes the header)."""
    return (f"w26c block {n:08d}\n").encode("ascii").ljust(MIB, b"~")


def _make_base_file(path: Path, mib: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        for i in range(mib):
            fh.write(_chunk(i))


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(MIB), b""):
            h.update(block)
    return h.hexdigest()


def _logs_mentioning(root: Path, needle: str) -> list[str]:
    """Every file under root whose bytes contain needle (UTF-8 or UTF-16LE, as tools write logs)."""
    hits: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        data = p.read_bytes()
        if needle.encode("utf-8") in data or needle.encode("utf-16-le") in data:
            hits.append(str(p))
    return hits


class _GrowingFile:
    """Appends one MiB every `interval` seconds on a background thread (card L4)."""

    def __init__(self, path: Path, interval: float = 0.2):
        self.path = path
        self.interval = interval
        self.appends = 0
        self.error: Exception | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="w26c-grow", daemon=True)

    def start(self) -> "_GrowingFile":
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            with open(self.path, "ab") as fh:
                while not self._stop.is_set():
                    fh.write(_chunk(self.appends))
                    fh.flush()
                    self.appends += 1
                    self._stop.wait(self.interval)
        except Exception as exc:  # noqa: BLE001 - surfaced by the test, never swallowed
            self.error = exc

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=60)
        assert not self._thread.is_alive(), "the growing-file writer did not stop"


def _cloud_rows(cfg_path: Path) -> list[dict]:
    rows, _lan, _cloud = _db_rows(cfg_path)
    return [r for r in rows if r["mode"] == "cloud"]


# ── L4 ──────────────────────────────────────────────────────────────────────────
def test_w26c_L4_file_growing_during_upload(rig, monkeypatch):
    """Card L4: 200 MB file, +1 MB every 200 ms while the cloud sync runs (started on the first run_tool
    call). Run 1: never COMPLETE (CLOUD_SUSPECT/PARTIAL). Writer stopped, run 2: COMPLETE and remote MD5 ==
    source MD5."""
    import core.cloud_sync as cs

    flow = rig["flow"]
    fy = flow._effective_fy_prefix(rig["cfg"])
    grow_src = rig["src"] / "Clients" / "growing.bin"
    remote = rig["buckets"] / "lab-bucket" / fy / "Clients" / "growing.bin"
    _make_base_file(grow_src, mib=200)

    real_run_tool = cs.run_tool
    holder: dict[str, _GrowingFile | None] = {"grow": None}

    def _run_tool_starting_writer(step, *args, **kwargs):
        # the writer starts on the first run_tool call of the cloud leg (card L4)
        if holder["grow"] is None:
            holder["grow"] = _GrowingFile(grow_src).start()
        return real_run_tool(step, *args, **kwargs)

    monkeypatch.setattr(cs, "run_tool", _run_tool_starting_writer)

    try:
        run1_exc = None
        try:
            flow.backup(config_path=str(rig["cfg_path"]), mode="cloud")
        except Exception as exc:  # noqa: BLE001 - a non-COMPLETE run raises by design
            run1_exc = exc
    finally:
        if holder["grow"] is not None:
            holder["grow"].stop()

    grow = holder["grow"]
    assert grow is not None, "the writer never started: run_tool was not called on the cloud leg"
    assert grow.error is None, f"the writer failed: {grow.error!r}"
    assert grow.appends >= 2, "the file did not grow during the run"

    run1 = _cloud_rows(rig["cfg_path"])
    assert run1, "run 1 wrote no cloud run_history row"
    assert run1[0]["status"] in CLOUD_NOT_OK, (run1[0]["status"], run1_exc)
    assert run1[0]["status"] not in CLOUD_COMPLETE

    # run 2: the writer is stopped, the source is static
    flow.backup(config_path=str(rig["cfg_path"]), mode="cloud")
    run2 = _cloud_rows(rig["cfg_path"])
    assert run2[0]["status"] in CLOUD_COMPLETE, run2[0]["status"]

    assert remote.is_file(), "run 2 did not leave the object in the remote"
    src_md5, remote_md5 = _md5_file(grow_src), _md5_file(remote)
    assert remote_md5 == src_md5, "remote MD5 differs from the source MD5"

    L.record("W26c.L4_growing_file",
             "a file that grows during the cloud upload is never reported COMPLETE; the next run "
             "uploads the settled file and the remote MD5 equals the source MD5",
             f"appends={grow.appends} | run1={run1[0]['status']} | run2={run2[0]['status']} | "
             f"md5 source={src_md5} remote={remote_md5}",
             "OK — run 1 not COMPLETE, run 2 COMPLETE, remote MD5 == source MD5")


# ── L5 ──────────────────────────────────────────────────────────────────────────
def test_w26c_L5_exclusively_locked_file_cloud_leg(rig):
    """Card L5: Clients\\lock.xlsx held open with share mode 0 during cloud run 1. Run 1 is not COMPLETE
    (PARTIAL/SUSPECT) and the file is named in the log. Handle closed in finally; run 2 COMPLETE and the
    object exists."""
    flow = rig["flow"]
    fy = flow._effective_fy_prefix(rig["cfg"])
    locked = rig["src"] / "Clients" / "lock.xlsx"
    remote = rig["buckets"] / "lab-bucket" / fy / "Clients" / "lock.xlsx"
    L.write_file(locked, b"WORKBOOK" * 4096)
    runtime = rig["sandbox"] / "runtime"

    handle = win32file.CreateFile(
        str(locked), win32file.GENERIC_READ, 0, None, win32file.OPEN_EXISTING, 0, None)
    try:
        try:
            flow.backup(config_path=str(rig["cfg_path"]), mode="cloud")
        except Exception:  # noqa: BLE001 - a non-COMPLETE run raises by design
            pass
    finally:
        win32file.CloseHandle(handle)

    run1 = _cloud_rows(rig["cfg_path"])
    assert run1, "run 1 wrote no cloud run_history row"
    assert run1[0]["status"] in CLOUD_NOT_OK, run1[0]["status"]
    named = _logs_mentioning(runtime, "lock.xlsx")
    assert named, "the locked file is not named in any run log"

    flow.backup(config_path=str(rig["cfg_path"]), mode="cloud")
    run2 = _cloud_rows(rig["cfg_path"])
    assert run2[0]["status"] in CLOUD_COMPLETE, run2[0]["status"]
    assert remote.is_file(), "run 2 did not leave lock.xlsx in the remote"

    L.record("W26c.L5_locked_file_cloud",
             "an exclusively locked file on the cloud leg makes the run not COMPLETE, is named in the log, "
             "and uploads on the next run after the handle is closed",
             f"run1={run1[0]['status']} | logs naming lock.xlsx={len(named)} | "
             f"run2={run2[0]['status']} | remote object present={remote.is_file()}",
             "OK — run 1 not COMPLETE and names the file, run 2 COMPLETE, object present")
