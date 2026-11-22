"""Adversarial scenarios for the backup flow (flow.backup -> cloud / LAN pipelines).

Every scenario drives the REAL ``flow.backup.fn`` with a real config file and a real SQLite
manifest in ``tmp_path``. Only the external edges are replaced:

* ``run_tool`` in ``core.cloud_sync`` / ``core.lan_sync`` -> a fake tool result (the real exit-code
  classification, log scanning and retry policy still run on it);
* the NTP-based health probe, the guards, the verify/record/publish tasks and the NAS wake-up ->
  stubs, so a scenario can make any one of them raise;
* ``notify`` (email) and the block-request writer -> mocks, so nothing is ever sent.

Invariants checked in every scenario (the backup-flow contract):

  I1  a non-success terminal status raises - the flow never returns normally (Prefect COMPLETED)
      while a leg ended non-success;
  I2  exactly one run_history row per leg run, and every row ends in a terminal status;
  I3  no success status after an error on the same leg.
"""

from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest
import yaml

import flow
from core import guards, statuses
from test_support import sandbox_config
from test_support.fake_tools import fake_run_tool, robocopy_log

# ───────────────────────── fixtures of tool output ─────────────────────────

CLOUD_OK_VERIFY = {
    "verified": True, "verify_termination": "normal", "verify_completion": True,
    "verify_differences": 0, "verify_reason": None,
    "size": {"count": 1, "bytes": 10},
    "manifest": [{"Path": "a.txt", "Size": 10, "ModTime": "2026-08-01T00:00:00Z"}],
    "diff": {"added": [], "removed": [], "modified": [], "unchanged": ["a.txt"]},
    "manifest_error": None,
}


def _rclone_line(level: str, msg: str) -> str:
    return json.dumps({"level": level, "msg": msg}) + "\n"


RCLONE_OK_LOG = (
    _rclone_line("notice", "Transferred: 1 / 1")
    + json.dumps({"stats": {"transfers": 1, "bytes": 10, "deletes": 0, "errors": 0,
                            "checks": 1}}) + "\n"
)
RCLONE_MAX_DELETE_LOG = _rclone_line("error", "--max-delete threshold reached") + (
    json.dumps({"stats": {"transfers": 0, "bytes": 0, "deletes": 500, "errors": 1,
                          "checks": 0}}) + "\n")

OK_CLOUD = {"exit_code": 0, "log": RCLONE_OK_LOG}
OK_LAN = {"exit_code": 1, "log": robocopy_log(copied=1), "log_encoding": "utf-16"}


class _Cancelled(BaseException):
    """Stands in for Prefect's cancellation signal (a BaseException, like KeyboardInterrupt)."""


# ───────────────────────── harness ─────────────────────────

DEFAULTS = {
    "health": None,
    "cloud_preflight": {"ok": True, "exit_code": 0, "error": None},
    "cloud_guards": {"max_delete": 500, "results": [], "approved_guards": []},
    "cloud_access": None,
    "cloud_verify": CLOUD_OK_VERIFY,
    "cloud_record": None,
    "cloud_publish": None,
    "lan_preflight": {"ok": True, "exit_code": 0, "error": None},
    "lan_snapshot_before": {},
    "lan_guards": {"results": [], "approved_guards": []},
    "lan_access": None,
    "lan_snapshot_after": {},
    "lan_record": None,
    "lan_publish": None,
    "lan_shutdown": {"ok": True},
}

_FLOW_ATTR = {
    "health": "health_check_task",
    "cloud_preflight": "cloud_preflight_task",
    "cloud_guards": "cloud_guards_task",
    "cloud_access": "cloud_access_task",
    "cloud_verify": "cloud_verify_and_report_task",
    "cloud_record": "cloud_record_task",
    "cloud_publish": "cloud_publish_artifact_task",
    "lan_preflight": "lan_preflight_task",
    "lan_snapshot_before": "lan_snapshot_before_task",
    "lan_guards": "lan_guards_task",
    "lan_access": "lan_access_task",
    "lan_snapshot_after": "lan_snapshot_after_task",
    "lan_record": "lan_record_task",
    "lan_publish": "lan_publish_artifact_task",
    "lan_shutdown": "lan_shutdown_task",
}


def _is_exc(value) -> bool:
    return isinstance(value, BaseException) or (isinstance(value, type) and issubclass(value, BaseException))


def _task(value=None, *, side_effect=None):
    """A stand-in for a Prefect task: callable, and `.with_options(...)` returns the same stand-in."""
    m = MagicMock(return_value=value, side_effect=side_effect)
    m.with_options.return_value = m
    return m


def _stub(value):
    """A stub from a plain value (returned), an exception (raised) or a callable (side effect)."""
    if _is_exc(value) or callable(value):
        return _task(side_effect=value)
    return _task(copy.deepcopy(value))


def _plain(task):
    """The REAL body of a Prefect task, called without a Prefect run (see test_flow_orchestration)."""
    m = MagicMock(side_effect=task.fn)
    m.with_options.return_value = m
    return m


@contextmanager
def _tool(module: str, spec):
    """`run_tool` of `module`: a fake result (dict for fake_run_tool), or an exception raised by the
    tool itself (a cancel / Ctrl+C during the subprocess). Yields the list of tool calls."""
    if _is_exc(spec):
        calls: list[str] = []

        def _interrupt(name, argv, **kwargs):
            calls.append(name)
            raise spec

        with patch(f"{module}.run_tool", side_effect=_interrupt):
            yield calls
    else:
        with fake_run_tool(module, **spec) as calls:
            yield calls


def _write_config(root: Path, *, wol: bool = False) -> Path:
    cfg = sandbox_config._config_dict(root)          # source/FY26-27 + runtime under root
    cfg["wol"] = {"enabled": wol, "server_ip": "192.168.10.10",
                  "mac_address": "00:11:22:33:44:55"}
    # A subprocess timeout well inside the default run budget: a timeout is then a real hang
    # (FAILED). A timeout that the run budget caused is PARTIAL (WP14) - see the budget scenario.
    cfg["cloud"]["subprocess_timeout_seconds"] = 3600
    cfg["lan"]["subprocess_timeout_seconds"] = 3600
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


class _NoSleepTime:
    """`flow.time` for a test: retry delays (validated to >= 60 s in config) are skipped, all else is real."""

    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def sleep(seconds):
        return None


@dataclass
class World:
    cfg: Path
    db: Path
    lock: Path
    cloud_calls: list
    lan_calls: list
    tasks: dict
    notify: MagicMock
    block_request: MagicMock
    shutdown_refused: list = field(default_factory=list)


@contextmanager
def _world(root: Path, *, stubs=None, cloud=None, lan=None, wol=False,
           concurrency=None, late_s: float = 0.0, seed: bool = True,
           remove_source: bool = False):
    stubs = stubs or {}
    cfg = _write_config(root, wol=wol)
    runtime = root / "runtime"
    if remove_source:
        shutil.rmtree(root / "source")
    elif seed:   # a real source has files; the command builders refuse an empty one (G1 backstop)
        src = root / "source" / "FY26-27"
        src.mkdir(parents=True, exist_ok=True)
        (src / "client_report.pdf").write_bytes(b"x" * 10)
    with ExitStack() as st:
        tasks = {name: _stub(stubs.get(name, default)) for name, default in DEFAULTS.items()}
        for name, attr in _FLOW_ATTR.items():
            st.enter_context(patch.object(flow, attr, tasks[name]))
        st.enter_context(patch.object(flow, "time", _NoSleepTime()))
        st.enter_context(patch.object(flow, "cloud_sync_task", _plain(flow.cloud_sync_task)))
        st.enter_context(patch.object(flow, "lan_sync_task", _plain(flow.lan_sync_task)))
        st.enter_context(patch.object(flow, "wol_check_task", _plain(flow.wol_check_task)))
        st.enter_context(patch.object(flow, "ensure_server_online",
                                      _stub(stubs.get("nas", True))))
        st.enter_context(patch.object(flow, "_complete_pending_rollover_nas_folder", MagicMock()))
        st.enter_context(patch.object(flow, "_active_fys", MagicMock(return_value=["FY26-27"])))
        st.enter_context(patch.object(flow, "_activate_overlap", MagicMock()))
        st.enter_context(patch.object(flow, "_scheduled_lateness_seconds",
                                      MagicMock(return_value=late_s)))
        st.enter_context(patch.object(flow, "configure_prefect_bridge", MagicMock()))
        st.enter_context(patch.object(flow, "configure_logging", MagicMock()))
        st.enter_context(patch.object(flow, "cleanup_orphaned_robocopy_logs", MagicMock()))
        notify = st.enter_context(patch.object(flow, "notify", MagicMock()))
        block = st.enter_context(patch.object(flow, "_open_block_request", MagicMock()))
        if concurrency is not None:
            st.enter_context(patch.object(flow, "concurrency", concurrency))
        cloud_calls = st.enter_context(_tool("core.cloud_sync", cloud or OK_CLOUD))
        lan_calls = st.enter_context(_tool("core.lan_sync", lan or OK_LAN))
        yield World(cfg=cfg, db=runtime / "manifest.db", lock=runtime / "backup.lock",
                    cloud_calls=cloud_calls, lan_calls=lan_calls, tasks=tasks,
                    notify=notify, block_request=block)


def _invoke(w: World, mode: str, budget_hours: float | None = None):
    """Run the real flow; return what it raised (None when it returned normally).
    budget_hours: a manual per-run budget (the schedule check does not apply to it)."""
    try:
        flow.backup.fn(str(w.cfg), mode, budget_hours=budget_hours)
    except BaseException as e:   # noqa: BLE001 - the scenario asserts on the raise itself
        return e
    return None


def _history(db: Path) -> list[dict]:
    if not db.exists():
        return []
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT run_id, mode, status, exit_code, error_message, extended_metrics "
                           "FROM run_history ORDER BY id").fetchall()
    finally:
        con.close()
    return [dict(zip(("run_id", "mode", "status", "exit_code", "error", "metrics"), r, strict=True))
            for r in rows]


@dataclass
class Outcome:
    raised: BaseException | None
    rows: list[dict]
    world: World

    def row(self, leg: str) -> dict:
        found = [r for r in self.rows if r["mode"] == leg]
        assert len(found) == 1, f"expected exactly one {leg} run_history row, got {self.rows}"
        return found[0]


@contextmanager
def _run(root: Path, mode: str, legs: tuple[str, ...], **kw):
    """Run the flow once in a `_world(root, **kw)`, check the flow invariants, yield the Outcome."""
    budget_hours = kw.pop("budget_hours", None)
    with _world(root, **kw) as w:
        raised = _invoke(w, mode, budget_hours)
        out = Outcome(raised=raised, rows=_history(w.db), world=w)
        _check_invariants(out, legs)
        yield out


def _check_invariants(out: Outcome, legs: tuple[str, ...]) -> None:
    # I2: one row per leg run; run ids unique; every row reached a terminal status
    for leg in legs:
        out.row(leg)
    assert len({r["run_id"] for r in out.rows}) == len(out.rows)
    running = [r for r in out.rows if statuses.category(r["status"]) == statuses.CATEGORY_RUNNING]
    assert not running, f"run(s) left in a RUNNING status: {running}"
    # I1: a non-success terminal row must have raised; a normal return means every leg succeeded
    non_success = [r for r in out.rows if not statuses.is_success(r["status"])]
    if non_success:
        assert out.raised is not None, f"non-success rows but the flow returned: {non_success}"
    if out.raised is None:
        assert not non_success


def _assert_failed_and_raised(out: Outcome, leg: str, status: str | None = None) -> None:
    """The leg ended non-success, the flow raised, and no success row exists for that leg (I3)."""
    row = out.row(leg)
    assert not statuses.is_success(row["status"]), row
    if status is not None:
        assert row["status"] == status, row
    assert out.raised is not None


def _group_contains(raised, kind) -> bool:
    if isinstance(raised, ExceptionGroup):
        return any(isinstance(e, kind) for e in raised.exceptions)
    return isinstance(raised, kind)


# ───────────────────────── 1-2  rclone exit codes ─────────────────────────

@pytest.mark.parametrize("code", [1, 2, 3, 6, 7, 8])
def test_rclone_fatal_exit_codes_fail_and_raise(tmp_path, code):
    """Fatal rclone exits (usage, dir not found, auth/bucket, transfer limit without the
    max-delete marker) are CLOUD_FAILED and make the flow raise - never a silent COMPLETED."""
    with _run(tmp_path, "cloud", ("cloud",),
              cloud={"exit_code": code, "log": _rclone_line("critical", f"rclone failed {code}")}) as out:
        _assert_failed_and_raise_group(out, "cloud", "CLOUD_FAILED")
        assert out.world.tasks["cloud_verify"].call_count == 0   # never verified a failed sync


@pytest.mark.parametrize("code", [4, 5, 10])
def test_rclone_transient_exit_codes_are_partial_and_raise(tmp_path, code):
    """Transient rclone exits (4/5) and the duration limit (10) are PARTIAL: resumable, alerted,
    and never success - even though the evidence gate may approve what did land."""
    with _run(tmp_path, "cloud", ("cloud",),
              cloud={"exit_code": code, "log": RCLONE_OK_LOG}) as out:
        _assert_failed_and_raise_group(out, "cloud", "CLOUD_PARTIAL")
        assert _group_contains(out.raised, flow.PartialRun)


def test_rclone_exit_zero_with_error_lines_is_suspect(tmp_path):
    """Exit 0 contradicted by ERROR lines in its own log is SUSPECT, never COMPLETE (C-DK-001)."""
    log = _rclone_line("error", "Failed to copy: 403 Forbidden") + RCLONE_OK_LOG
    with _run(tmp_path, "cloud", ("cloud",), cloud={"exit_code": 0, "log": log}) as out:
        _assert_failed_and_raise_group(out, "cloud", "CLOUD_SUSPECT")


def _assert_failed_and_raise_group(out: Outcome, leg: str, status: str) -> None:
    _assert_failed_and_raised(out, leg, status)
    assert isinstance(out.raised, ExceptionGroup), out.raised


# ───────────────────────── 3-6  timeouts, missing binary, missing source ─────────────────────────

def test_rclone_timeout_fails_once_without_retry(tmp_path):
    """A hung rclone killed by its own timeout (budget not binding) is FAILED, not retried: its
    deletions are unknown (XR-6)."""
    with _run(tmp_path, "cloud", ("cloud",), cloud={"timed_out": True}) as out:
        _assert_failed_and_raise_group(out, "cloud", "CLOUD_FAILED")
        assert len(out.world.cloud_calls) == 1
        assert "Timeout" in out.row("cloud")["error"]


def test_rclone_missing_binary_fails_with_fault_code(tmp_path):
    """rclone.exe not found is a spawn error: CLOUD_FAILED with the AAM-SYS-04 fault code."""
    spec = {"spawn_error": "FileNotFoundError: [WinError 2] The system cannot find the file"}
    with _run(tmp_path, "cloud", ("cloud",), cloud=spec) as out:
        _assert_failed_and_raise_group(out, "cloud", "CLOUD_FAILED")
        assert "AAM-SYS-04" in (out.row("cloud")["metrics"] or "")


def test_cloud_source_folder_missing_fails_closed(tmp_path):
    """The source folder vanished: rclone reports 'directory not found' (exit 3). Nothing is
    recorded as bucket state and the run is FAILED."""
    log = _rclone_line("critical", "Failed to sync: directory not found")
    with _run(tmp_path, "cloud", ("cloud",), remove_source=True,
              cloud={"exit_code": 3, "log": log}) as out:
        _assert_failed_and_raise_group(out, "cloud", "CLOUD_FAILED")
        assert out.world.tasks["cloud_record"].call_count == 0


def test_lan_source_folder_missing_robocopy_fatal_fails(tmp_path):
    """Robocopy cannot open the source (exit 16, fatal): LAN_FAILED, the mirror never runs twice on it."""
    log = robocopy_log(failed=1, body="ERROR 3 (0x00000003) Accessing Source Directory\n")
    spec = {"exit_code": 16, "log": log, "log_encoding": "utf-16"}
    with _run(tmp_path, "lan", ("lan",), remove_source=True, lan=spec) as out:
        _assert_failed_and_raised(out, "lan", "LAN_FAILED")
        assert out.world.tasks["lan_record"].call_count == 0


# ───────────────────────── 7-8  guards: empty source, mass delete ─────────────────────────

def test_empty_source_is_refused_before_rclone_starts(tmp_path):
    """An empty source would make the mirror delete the whole bucket. The real G1 backstop in the
    rclone command builder refuses it, so rclone is never started and the run is FAILED (not
    success, not an upload of nothing)."""
    with _run(tmp_path, "cloud", ("cloud",), seed=False) as out:
        _assert_failed_and_raise_group(out, "cloud", "CLOUD_FAILED")
        assert out.world.cloud_calls == []
        assert "contains no files" in out.row("cloud")["error"]


def test_empty_source_blocked_by_guard_opens_human_request(tmp_path):
    """The pre-sync guard (G1/G3 measurement) blocks a risky mirror: CLOUD_BLOCKED, rclone never
    started, and a human approval request is opened."""
    blocked = guards.backstop_blocked("cloud", {"results": []}, 0)
    with _run(tmp_path, "cloud", ("cloud",), stubs={"cloud_guards": blocked}) as out:
        _assert_failed_and_raised(out, "cloud", "CLOUD_BLOCKED")
        assert out.world.cloud_calls == []
        assert out.world.block_request.called


def test_mass_delete_backstop_blocks_without_retry(tmp_path):
    """rclone stopped at --max-delete: BLOCKED for a human (re-measured), never retried."""
    spec = {"exit_code": 8, "log": RCLONE_MAX_DELETE_LOG}
    with _run(tmp_path, "cloud", ("cloud",), cloud=spec) as out:
        _assert_failed_and_raised(out, "cloud", "CLOUD_BLOCKED")
        assert len(out.world.cloud_calls) == 1
        assert out.world.tasks["cloud_guards"].call_count == 2   # before the sync and the re-measure
        assert out.world.block_request.called


# ───────────────────────── 9-12  exceptions and interrupts in the cloud pipeline ─────────────────────────

def test_cloud_verify_exception_is_verify_failed(tmp_path):
    """An exception in the verify phase (after rclone said COMPLETE) is CLOUD_VERIFY_FAILED."""
    stubs = {"cloud_verify": RuntimeError("manifest listing crashed")}
    with _run(tmp_path, "cloud", ("cloud",), stubs=stubs) as out:
        _assert_failed_and_raise_group(out, "cloud", "CLOUD_VERIFY_FAILED")
        assert out.world.tasks["cloud_record"].call_count == 0    # no bucket state recorded


def test_cloud_post_phase_exception_is_failed_not_complete(tmp_path):
    """The sync and the verify both passed; the DB write after them fails. The row must be FAILED,
    not the rclone COMPLETE it was carrying (decide_terminal_status 'post' rule)."""
    stubs = {"cloud_record": OSError("database or disk is full")}
    with _run(tmp_path, "cloud", ("cloud",), stubs=stubs) as out:
        _assert_failed_and_raise_group(out, "cloud", "CLOUD_FAILED")
        assert "disk is full" in out.row("cloud")["error"]


def test_keyboard_interrupt_during_cloud_sync_propagates_and_fails(tmp_path):
    """Ctrl+C / service stop while rclone runs: the interrupt propagates (not wrapped in an
    ExceptionGroup), the row is FAILED and the run is not a success."""
    with _run(tmp_path, "cloud", ("cloud",), cloud=KeyboardInterrupt()) as out:
        _assert_failed_and_raised(out, "cloud", "CLOUD_FAILED")
        assert isinstance(out.raised, KeyboardInterrupt)


def test_cancel_during_cloud_verify_is_verify_failed(tmp_path):
    """A Prefect cancel (BaseException) landing in the verify phase is VERIFY_FAILED and re-raised."""
    with _run(tmp_path, "cloud", ("cloud",), stubs={"cloud_verify": _Cancelled()}) as out:
        _assert_failed_and_raised(out, "cloud", "CLOUD_VERIFY_FAILED")
        assert isinstance(out.raised, _Cancelled)


# ───────────────────────── 13-16  robocopy exit codes, truncated log, timeout ─────────────────────────

@pytest.mark.parametrize("code", [16, 24, 32, 255])
def test_robocopy_fatal_or_unknown_exit_codes_fail_and_raise(tmp_path, code):
    """Robocopy bit 4 (fatal, 16/24/255) and unknown codes (32) are LAN_FAILED, never COMPLETE."""
    spec = {"exit_code": code, "log": robocopy_log(copied=0), "log_encoding": "utf-16"}
    with _run(tmp_path, "lan", ("lan",), lan=spec) as out:
        _assert_failed_and_raise_group(out, "lan", "LAN_FAILED")


@pytest.mark.parametrize("code", [8, 10, 15])
def test_robocopy_copy_error_exit_codes_are_partial_and_raise(tmp_path, code):
    """Bit 3 (copy errors) with a complete summary is LAN_PARTIAL: alerted, NAS not powered off, and
    the flow still raises PartialRun."""
    spec = {"exit_code": code, "log": robocopy_log(copied=1, failed=2), "log_encoding": "utf-16"}
    with _run(tmp_path, "lan", ("lan",), lan=spec) as out:
        _assert_failed_and_raise_group(out, "lan", "LAN_PARTIAL")
        assert _group_contains(out.raised, flow.PartialRun)
        assert out.world.tasks["lan_shutdown"].call_count == 0


def test_robocopy_clean_exit_with_truncated_log_is_suspect(tmp_path):
    """Exit 0 from a process that was killed mid-log has no completed job summary: the exit code is
    not evidence (T04/A1). LAN_SUSPECT, not retried, flow raises."""
    spec = {"exit_code": 0, "log": "ROBOCOPY output cut off here\n", "log_encoding": "utf-16"}
    with _run(tmp_path, "lan", ("lan",), lan=spec) as out:
        _assert_failed_and_raise_group(out, "lan", "LAN_SUSPECT")
        assert len(out.world.lan_calls) == 1


def test_robocopy_timeout_fails_without_retry(tmp_path):
    """A hung robocopy killed by its own timeout (budget not binding) is LAN_FAILED and is not
    retried: what it deleted before the kill is unknown."""
    with _run(tmp_path, "lan", ("lan",), lan={"timed_out": True}) as out:
        _assert_failed_and_raise_group(out, "lan", "LAN_FAILED")
        assert len(out.world.lan_calls) == 1


def test_robocopy_timeout_at_run_budget_is_partial_not_failed(tmp_path):
    """The same timeout when the LAN run budget is what stopped robocopy: LAN_PARTIAL (resumable,
    alerted), not FAILED, still not success."""
    # 1 h budget minus the 30 min reserve: robocopy starts with a capped timeout and the budget stops it.
    with _run(tmp_path, "lan", ("lan",), lan={"timed_out": True}, budget_hours=1.0) as out:
        _assert_failed_and_raise_group(out, "lan", "LAN_PARTIAL")
        assert _group_contains(out.raised, flow.PartialRun)
        assert len(out.world.lan_calls) == 1


# ───────────────────────── 17-18  NAS unreachable, LAN post-phase exception ─────────────────────────

def test_nas_unreachable_fails_before_any_copy(tmp_path):
    """The NAS does not answer after Wake-on-LAN: the run fails in the pre phase, robocopy never
    starts, the NAS is not powered off, and exactly one alert is sent."""
    stubs = {"nas": RuntimeError("NAS did not answer SMB within 300 s after Wake-on-LAN")}
    with _run(tmp_path, "lan", ("lan",), stubs=stubs, wol=True) as out:
        _assert_failed_and_raise_group(out, "lan", "LAN_FAILED")
        assert out.world.lan_calls == []
        assert out.world.tasks["lan_shutdown"].call_count == 0
        assert out.world.notify.call_count == 1


def test_lan_post_phase_exception_is_failed_not_complete(tmp_path):
    """robocopy finished cleanly, then the run record fails: LAN_COMPLETE must not survive as the row."""
    stubs = {"lan_record": RuntimeError("sqlite: database is locked")}
    with _run(tmp_path, "lan", ("lan",), stubs=stubs) as out:
        _assert_failed_and_raise_group(out, "lan", "LAN_FAILED")


def test_keyboard_interrupt_during_mirror_fails_and_propagates(tmp_path):
    """Ctrl+C while robocopy mirrors: the row is FAILED (not SKIPPED, the data may be half copied)
    and the interrupt propagates."""
    with _run(tmp_path, "lan", ("lan",), lan=KeyboardInterrupt()) as out:
        _assert_failed_and_raised(out, "lan", "LAN_FAILED")
        assert isinstance(out.raised, KeyboardInterrupt)


# ───────────────────────── 19-21  locks ─────────────────────────

def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_stale_lock_with_dead_pid_is_taken_over(tmp_path):
    """A lock left by a killed backup (dead PID) must not block tonight's run: it is replaced,
    the run completes, and the lock is released afterwards."""
    with _world(tmp_path) as w:
        w.lock.parent.mkdir(parents=True, exist_ok=True)
        w.lock.write_text(f"{_dead_pid()}:1700000000.0", encoding="utf-8")
        raised = _invoke(w, "cloud")
        rows = _history(w.db)
    assert raised is None
    assert [r["status"] for r in rows] == ["CLOUD_COMPLETE"]
    assert len(w.cloud_calls) == 1
    assert not w.lock.exists()


def test_live_foreign_lock_refuses_run_without_transfer(tmp_path):
    """Another live process (a real child process here) holds the backup lock: this run must refuse,
    record a SLOT_TIMEOUT row, never start rclone, and leave the other process's lock untouched."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        create_time = psutil.Process(child.pid).create_time()
        with _world(tmp_path) as w:
            w.lock.parent.mkdir(parents=True, exist_ok=True)
            w.lock.write_text(f"{child.pid}:{create_time!r}", encoding="utf-8")
            raised = _invoke(w, "cloud")
            rows = _history(w.db)
            lock_text = w.lock.read_text(encoding="utf-8")
    finally:
        child.kill()
        child.wait()
    assert isinstance(raised, ExceptionGroup), raised
    assert [r["status"] for r in rows] == ["CLOUD_SLOT_TIMEOUT"]
    assert w.cloud_calls == []
    assert lock_text.startswith(f"{child.pid}:")


def test_two_concurrent_backups_second_never_transfers(tmp_path, monkeypatch):
    """Two backups at once (scheduled + manual). The Prefect 'aam-backup' limit of 1 is simulated
    with a semaphore; the second run waits, times out and must not transfer or touch the first
    run's row. The first run completes normally."""
    monkeypatch.setenv("PREFECT_TEST_MODE", "0")   # production slot branch (not the test bypass)
    limit = threading.BoundedSemaphore(1)

    @contextmanager
    def fake_limit(name, occupy, timeout_seconds):
        if not limit.acquire(timeout=0.5):
            raise TimeoutError(f"concurrency limit '{name}' is full")
        try:
            yield
        finally:
            limit.release()

    in_verify = threading.Event()
    gate = threading.Event()

    def verify_while_holding_the_slot(*args, **kwargs):
        in_verify.set()
        gate.wait(30)
        return copy.deepcopy(CLOUD_OK_VERIFY)

    first: dict = {}
    with _world(tmp_path, stubs={"cloud_verify": verify_while_holding_the_slot},
                concurrency=fake_limit) as w:
        worker = threading.Thread(target=lambda: first.update(raised=_invoke(w, "cloud")))
        worker.start()
        try:
            assert in_verify.wait(30), "first run never reached the verify phase"
            second_raised = _invoke(w, "cloud")
        finally:
            gate.set()
            worker.join(60)
        rows = _history(w.db)
    assert first["raised"] is None
    assert isinstance(second_raised, ExceptionGroup), second_raised
    assert sorted(r["status"] for r in rows) == ["CLOUD_COMPLETE", "CLOUD_SLOT_TIMEOUT"]
    assert len(w.cloud_calls) == 1          # only the first run ever reached rclone


@pytest.mark.xfail(strict=False, reason=(
    "SUSPECTED BUG: flow.py:3281 (_run_leg_folders, `except Exception` around _backup_slot) "
    "does not catch a BaseException (cancel / Ctrl+C) raised while waiting for the slot, so no "
    "*_SLOT_TIMEOUT row is written and the *_RUNNING row from before the wait is left behind"))
def test_interrupt_while_waiting_for_the_slot_leaves_a_terminal_row(tmp_path, monkeypatch):
    """A cancel while the run waits for the backup slot must not leave the row in *_RUNNING.

    SUSPECTED BUG (flow.py:_run_leg_folders, the `except Exception` around _backup_slot): a
    BaseException raised while entering the slot is not caught there, so no SLOT_TIMEOUT row is
    written and the RUNNING row written before the wait stays until a later sweep."""
    monkeypatch.setenv("PREFECT_TEST_MODE", "0")

    @contextmanager
    def cancelled_while_waiting(name, occupy, timeout_seconds):
        raise KeyboardInterrupt()
        yield  # pragma: no cover

    with _world(tmp_path, concurrency=cancelled_while_waiting) as w:
        raised = _invoke(w, "cloud")
        rows = _history(w.db)
    assert isinstance(raised, KeyboardInterrupt)
    assert w.cloud_calls == []
    assert [r["status"] for r in rows] != ["CLOUD_RUNNING"], rows
    assert all(not statuses.is_success(r["status"]) for r in rows)


# ───────────────────────── 22  mixed legs ─────────────────────────

def test_mode_all_failing_lan_keeps_cloud_success_and_raises_once(tmp_path):
    """mode=all: cloud completes, LAN fatal-fails. Each leg keeps its own honest status, the flow
    raises exactly one error (the LAN one), and the cloud success row is not rewritten."""
    spec = {"exit_code": 16, "log": robocopy_log(copied=0), "log_encoding": "utf-16"}
    with _run(tmp_path, "all", ("cloud", "lan"), lan=spec) as out:
        assert out.row("cloud")["status"] == "CLOUD_COMPLETE"
        _assert_failed_and_raise_group(out, "lan", "LAN_FAILED")
        assert len(out.raised.exceptions) == 1
        assert out.world.tasks["cloud_record"].call_count == 1
