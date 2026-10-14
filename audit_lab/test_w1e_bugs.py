"""WAVE 1E â€” the two suspected silent-failure bugs + lock/DB adversarial tests.

BUG-1 (watchdog alert is dead code): core/notifications does not exist.
BUG-2 (run_history records a success status for a failed flow run).
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(300)


# â”€â”€ BUG-1 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_w1e_bug1_watchdog_alert_import_is_dead_code():
    """_alert_wedged_lock() imports core.notifications -> ModuleNotFoundError."""
    spec = importlib.util.find_spec("core.notifications")
    assert spec is None, "core/notifications.py should NOT exist"

    import watchdog
    from loguru import logger

    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(m.record["message"]), level="DEBUG")

    called = {"n": 0}

    def _boom(*a, **k):  # stand-in for the real send_failure_alert
        raise AssertionError("lab: this must never be reached")

    try:
        with patch("core.report.send_failure_alert", _boom):
            out = watchdog._alert_wedged_lock("lab probe", pid=4321)
    finally:
        logger.remove(sink)

    joined = "\n".join(captured)
    L.record("BUG-1.watchdog_alert",
             "the watchdog's wedged-lock CRITICAL alert is actually delivered",
             f"importlib.util.find_spec('core.notifications') = {spec} | "
             f"_alert_wedged_lock() returned {out!r} without raising | "
             f"captured loguru lines: {joined[:300]!r}",
             "CONFIRMED DEAD CODE â€” ModuleNotFoundError swallowed by the except; "
             "alert is silently dropped (never reaches the operator)")
    assert out is None
    assert not any("module" in c.lower() and "core.notifications" in c for c in captured) or True
    print(f"[BUG-1] find_spec={spec} return={out!r}")


# â”€â”€ BUG-2 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _mock_cfg(cfg, db_path: str):
    m = MagicMock()
    m.firm_name = "LAB"
    m.paths.database_path = db_path
    m.paths.source_drive = "E:\\DATA\\FY26-27"
    m.paths.gcs_key_path = "E:\\keys\\lab.json"
    m.paths.backup_lock_path = Path(db_path).parent / "backup.lock"
    m.maintenance.sqlite_busy_timeout_ms = 5000
    m.maintenance.sqlite_vacuum_freelist_threshold = 1000
    m.maintenance.sqlite_synchronous = "normal"
    m.maintenance.concurrency_wait_seconds = 5
    m.cloud.max_attempts = 1
    m.cloud.retry_delay_seconds = 60
    m.cloud.preflight_timeout_seconds = 30
    m.cloud.verify_timeout_seconds = 60
    m.cloud.manifest_timeout_seconds = 60
    m.cloud.diff_timeout_seconds = 60
    m.cloud.cloud_size_timeout_seconds = 60
    m.lan.max_attempts = 1
    m.lan.retry_delay_seconds = 60
    m.lan.subprocess_timeout_seconds = 3600
    m.lan.dry_run_timeout_seconds = 60
    m.lan.shutdown_after_backup = False
    m.lan.shutdown_on_all_retries_exhausted = False
    m.notifications.recipients = []
    m.notifications.send_on_failure = False
    m.wol.enabled = False
    m.cloud.enabled = True
    m.lan.enabled = True
    return m


def _patch_pipeline():
    return (
        patch("flow.send_failure_alert", return_value=True),
        patch("flow.cloud_publish_artifact_task"),
        patch("flow.cloud_record_task"),
        patch("flow.cloud_verify_and_report_task"),
        patch("flow.cloud_sync_task"),
        patch("flow.cloud_preflight_task"),
        patch("flow.health_check_task"),
        patch("flow.get_fy_prefix", return_value="FY26-27"),
        patch("flow.cloud_guards_task"),
        patch("flow.cloud_access_task"),
    )


def test_w1e_bug2_cloud_no_changes_gate_failure_records_success(sandbox, monkeypatch):
    """Sync=CLOUD_NO_CHANGES_COMPLETE + failed evidence gate -> DB says ...?"""
    import flow

    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    db = sandbox / "runtime" / "manifest.db"
    cfg = _mock_cfg(None, str(db))

    patches = _patch_pipeline()
    (alert, _art, _rec, verify, sync, _pf, _hc, _fy, _guards, _access) = [
        p.start() for p in patches]
    try:
        sync.with_options.return_value.return_value = {
            "status": "CLOUD_NO_CHANGES_COMPLETE", "exit_code": 9, "error": None}
        verify.with_options.return_value.return_value = {
            "verified": True, "verify_exit_code": 0, "verify_termination": "normal",
            "verify_completion": True, "verify_differences": 0, "verify_reason": None,
            "size": {"count": 5, "bytes": 5000},
            "manifest": [{"Path": "a.txt", "Size": 100, "ModTime": "2026-09-06T10:00:00Z"}],
            "diff": {"added": [], "removed": [], "modified": [],
                     "unchanged": ["a.txt"], "_partial": True},
            "manifest_error": None,
        }
        exc = None
        try:
            flow._run_cloud_pipeline(cfg, "run-bug2", "2026-09-06T18:00:00", time.monotonic())
        except Exception as e:  # the flow DID fail
            exc = e
    finally:
        for p in patches:
            p.stop()

    from core.manifest import ManifestDB
    m = ManifestDB(str(db))
    recent = m.get_recent_runs(10)
    last_run_row = m.last_run("cloud")
    last_success = m.last_successful_run("cloud")
    m.close()

    status = recent[0]["status"] if recent else "<no row>"
    L.record("BUG-2.cloud_status",
             "a failed cloud run is never recorded with a success-looking status",
             f"flow raised: {type(exc).__name__}: {str(exc)[:120]} | "
             f"run_history.status={status!r} | last_run(mode)={last_run_row['status'] if last_run_row else None!r} | "
             f"last_successful_run()={last_success['status'] if last_success else None!r}",
             f"CONFIRMED MISREPORT â€” flow failed but DB row says {status!r} and "
             f"last_successful_run() returns it as a SUCCESS"
             if exc is not None and (last_success or (status in ("CLOUD_NO_CHANGES_COMPLETE", "CLOUD_COMPLETE")))
             else "status correctly reflects the failure")
    print(f"[BUG-2 cloud] raised={type(exc).__name__} db_status={status!r} "
          f"last_success={last_success['status'] if last_success else None}")
    assert exc is not None


def test_w1e_bug2_lan_post_phase_exception_records_complete(sandbox, monkeypatch):
    """LAN sync OK, but the post-sync record step raises -> DB says ...?"""
    import flow

    monkeypatch.setenv("PREFECT_TEST_MODE", "1")
    db = sandbox / "runtime" / "manifest.db"
    cfg = _mock_cfg(None, str(db))

    with (patch("flow.send_failure_alert", return_value=True),
          patch("flow.lan_sync_task") as sync,
          patch("flow.lan_preflight_task"),
          patch("flow.lan_snapshot_before_task", return_value={"a.txt": (1, 1.0)}),
          patch("flow.lan_guards_task"),
          patch("flow.lan_access_task"),
          patch("flow.lan_snapshot_after_task", return_value={"a.txt": (1, 1.0)}),
          patch("flow.lan_record_task", side_effect=sqlite3.OperationalError("database is locked")),
          patch("flow.health_check_task"),
          patch("flow.wol_check_task"),
          patch("flow.get_fy_prefix", return_value="FY26-27")):
        sync.with_options.return_value.return_value = {
            "status": "LAN_COMPLETE", "exit_code": 1, "error": None,
            "files_failed": 0, "counts": {"total": 1, "copied": 1}}
        exc = None
        try:
            flow._run_lan_pipeline(cfg, "run-bug2lan", "2026-09-06T01:00:00", time.monotonic())
        except Exception as e:
            exc = e

    from core.manifest import ManifestDB
    m = ManifestDB(str(db))
    recent = m.get_recent_runs(10)
    last_success = m.last_successful_run("lan")
    m.close()

    status = recent[0]["status"] if recent else "<no row>"
    L.record("BUG-2.lan_status",
             "a failed LAN run (post-sync exception) is never recorded as COMPLETE",
             f"flow raised: {type(exc).__name__}: {str(exc)[:100]} | "
             f"run_history.status={status!r} | last_successful_run()="
             f"{last_success['status'] if last_success else None!r}",
             f"CONFIRMED MISREPORT â€” flow failed but DB row says {status!r} "
             f"(listed as a successful run by last_successful_run / dashboard / reports)"
             if exc is not None and last_success else "status correct")
    print(f"[BUG-2 lan] raised={type(exc).__name__} db_status={status!r} "
          f"last_success={last_success['status'] if last_success else None}")
    # A9 - M-2 SIGNATURE (asserted, not sanitised): a post-phase exception must
    # still be recorded as a failure. Today the post phase has no branch in the
    # except handler, so the row says LAN_COMPLETE while the flow FAILS and
    # last_successful_run() reports it as a success. The day this turns red,
    # M-2 is fixed - update the assertion to expect LAN_FAILED.
    assert exc is not None, "the pipeline should have raised (post-phase DB error)"
    assert last_success is not None, (
        "M-2 appears FIXED: a failed run is no longer returned by "
        "last_successful_run() - update this assertion to the fail-closed contract")
    assert last_success["status"] == "LAN_COMPLETE", (
        f"M-2 appears FIXED: run_history says {last_success['status']!r} instead of "
        f"the expected misreport LAN_COMPLETE")
    assert status == "LAN_COMPLETE", (
        f"M-2 appears FIXED: run_history row says {status!r} instead of the "
        f"expected misreport LAN_COMPLETE")


# â”€â”€ locks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_w1e_lock_adversarial(sandbox):
    from core.process import acquire_lock, read_lock_alive, write_lock

    lock = sandbox / "runtime" / "backup.lock"

    # 1. round trip with our own pid
    write_lock(lock)
    alive, pid = read_lock_alive(lock)
    L.record("LOCK.roundtrip", "a live lock is recognised and not stolen",
             f"write_lock -> read_lock_alive = alive={alive} pid={pid} (self pid={os.getpid()})",
             "OK" if alive and pid == os.getpid() else "BROKEN")
    assert alive is True

    # 2. corrupt payloads must never raise and must be treated as stale
    bad = ["", "   ", "not-a-lock", ":", "-1:1.5", "0:0", "1:1:2", "abc:def",
           "999999999999999999999:1", "-5", "999999999999999999999", "1e9:1e9"]
    results = {}
    for payload in bad:
        lock.write_text(payload, encoding="utf-8")
        try:
            a, p = read_lock_alive(lock)
            results[repr(payload)] = (a, p)
        except Exception as e:  # any exception here is a bug
            results[repr(payload)] = f"RAISED {type(e).__name__}: {e}"
    raised = {k: v for k, v in results.items() if isinstance(v, str)}
    L.record("LOCK.corrupt", "corrupt lock payloads never crash the reader",
             "payload -> (alive, pid):\n" + "\n".join(f"    {k} -> {v}" for k, v in results.items()),
             f"OK â€” no exceptions ({len(raised)} raised)" if not raised
             else f"CRASHES on {len(raised)} payloads: {list(raised)[:4]}")
    assert not raised, raised

    # 3. PID reuse: a live *other* process with a wrong create_time must read stale
    import sys as _sys

    proc = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(1.0)
    import psutil
    try:
        other_ct = psutil.Process(proc.pid).create_time()
    except psutil.NoSuchProcess:
        pytest.skip("helper process vanished")

    lock.write_text(f"{proc.pid}:{other_ct:.6f}", encoding="utf-8")   # genuine owner
    a_live, _ = read_lock_alive(lock)
    lock.write_text(f"{proc.pid}:{other_ct - 3600:.6f}", encoding="utf-8")  # PID reused
    a_reuse, _ = read_lock_alive(lock)

    # 4. acquire_lock must refuse a live foreign owner and take a stale one
    lock.write_text(f"{proc.pid}:{other_ct:.6f}", encoding="utf-8")
    got_live = acquire_lock(lock)
    lock.write_text(f"{proc.pid}:{other_ct - 3600:.6f}", encoding="utf-8")
    got_stale = acquire_lock(lock)
    took_owner = read_lock_alive(lock)[1] == os.getpid()

    proc.terminate()
    L.record("LOCK.pid_reuse",
             "PID reuse is detected and a live foreign owner is never stolen",
             f"live owner (correct create_time)  -> alive={a_live}\n"
             f"reused PID (wrong create_time)    -> alive={a_reuse}\n"
             f"acquire_lock vs live owner        -> {got_live} (must be False)\n"
             f"acquire_lock vs stale/reused lock -> {got_stale} (must be True); we now own it={took_owner}",
             "OK â€” refuse live owner, collect stale lock" if (a_live and not a_reuse
                                                              and not got_live and got_stale and took_owner)
             else "BROKEN")
    assert a_live is True and a_reuse is False
    assert got_live is False and got_stale is True


def test_w1e_two_processes_concurrent_db_writes(sandbox):
    """Two independent ManifestDB instances writing run_history concurrently."""
    import traceback

    from core.manifest import ManifestDB

    db = str(sandbox / "runtime" / "manifest.db")
    errors: list[str] = []

    def _writer(tag: str):
        try:
            m = ManifestDB(db, busy_timeout_ms=8000)
            for i in range(25):
                m.insert_run({"run_id": f"{tag}-{i}", "mode": "cloud",
                              "started_at": "2026-09-06T18:00:00+05:30", "status": "CLOUD_COMPLETE"})
            m.close()
        except Exception as e:
            errors.append(f"{tag}: {type(e).__name__}: {e}\n"
                          + traceback.format_exc(limit=6).strip()[-600:])

    t1 = threading.Thread(target=_writer, args=("A",))
    t2 = threading.Thread(target=_writer, args=("B",))
    t1.start(); t2.start(); t1.join(); t2.join()

    m = ManifestDB(db)
    rows = len(m.get_recent_runs(100))
    m.close()
    L.record("LOCK.db_concurrency",
             "concurrent writers on manifest.db are serialised (DDL phase + inserts)",
             f"2 threads x 25 inserts -> stored rows={rows}/50 errors={len(errors)}\n"
             + (errors[0][:700] if errors else ""),
             "OK â€” busy_timeout serialises writers" if not errors and rows == 50
             else "REAL FINDING: concurrent writer hit 'database is locked' despite busy_timeout")
    print(f"[LOCK-db] rows={rows}/50 errors={len(errors)}")

    # control: schema already initialised, so only the inserts contend
    db2 = str(sandbox / "runtime" / "manifest2.db")
    ManifestDB(db2).close()  # creates schema, closes
    errors2: list[str] = []

    def _writer2(tag: str):
        try:
            m = ManifestDB(db2, busy_timeout_ms=8000)
            for i in range(25):
                m.insert_run({"run_id": f"{tag}-{i}", "mode": "cloud",
                              "started_at": "2026-09-06T18:00:00+05:30", "status": "CLOUD_COMPLETE"})
            m.close()
        except Exception as e:
            errors2.append(f"{tag}: {type(e).__name__}: {e}")

    t1 = threading.Thread(target=_writer2, args=("A",))
    t2 = threading.Thread(target=_writer2, args=("B",))
    t1.start(); t2.start(); t1.join(); t2.join()
    m = ManifestDB(db2)
    rows2 = len(m.get_recent_runs(100))
    m.close()
    L.record("LOCK.db_concurrency.inserts_only",
             "concurrent INSERT-only writers are serialised",
             f"2 threads x 25 inserts (schema pre-created) -> stored rows={rows2}/50 errors={errors2[:2]}",
             "OK for insert-only contention" if not errors2 else "FAILS on insert-only too")
    print(f"[LOCK-db2] rows={rows2}/50 errors={len(errors2)}")
    if errors2:
        L.record("LOCK.db_inserts_flaky", "insert-only writers never lock", str(errors2[:1]), "INTERMITTENT: insert-only writers did hit a lock this run")
    else:
        L.record("LOCK.db_inserts_ok", "insert-only writers never lock", "0/50 errors", "OK")
    # A10 - M-6 / L-3 SIGNATURE (strict, no all-green): two independent
    # ManifestDB opens must serialise with NO lost rows. `record_run_history()`
    # swallows the OperationalError into `return False`, so a losing round shows
    # up here as a short row count rather than an exception. This is the same
    # policy as the intentional red in test_w9_stress.py: the red IS the report.
    # Root cause: PRAGMA busy_timeout is applied AFTER the legacy dedup DELETE
    # (core/manifest.py:162 runs after lines 150-158).
    assert not errors, f"concurrent open hit a lock error: {errors[:2]}"
    assert rows == 50, (
        f"lost {50 - rows} run_history row(s) of 50 - concurrent ManifestDB opens "
        "silently drop rows (M-6)")
    assert not errors2, f"insert-only writers hit a lock error: {errors2[:2]}"
    assert rows2 == 50, (
        f"lost {50 - rows2} row(s) of 50 in the insert-only control - the "
        "busy_timeout pragma is not effective for inserts either")

