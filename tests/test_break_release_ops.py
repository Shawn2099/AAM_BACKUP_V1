"""Release break-it cards U1, U2, U8, U10 (agent_runs/TEST_DESIGN_RELEASE.md, section B).

Oracles are computed here from first principles (psutil create times, hashlib over the file bytes,
SMTP sink messages, SQL counts, MagicMock call counts), never by calling the code under test a second time.
Suspected product bugs are kept as xfail(strict=False) with a "SUSPECTED BUG" reason; the expectation is
never weakened to make a test pass."""

import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import prefect
import psutil
import pytest
from loguru import logger

import flow
import launch
import watchdog
from core import backup_repository as repo
from core.manifest import ManifestDB, ManifestSchemaError
from core.time_utils import now_iso
from models.config import MaintenanceConfig
from test_support.smtp_server import smtp_server
from tests.test_m1_notify import _cfg


def _own_token() -> str:
    """'PID:create_time' of this process, read straight from psutil (the lock-file identity)."""
    pid = os.getpid()
    return f"{pid}:{psutil.Process(pid).create_time():.6f}"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── U1 [unit] malformed backup.lock ──────────────────────────────────────────

def _slot_config(lock_path):
    return SimpleNamespace(
        maintenance=SimpleNamespace(concurrency_wait_seconds=1, lock_release_retries=3,
                                    lock_release_delay_ms=10),
        paths=SimpleNamespace(backup_lock_path=Path(lock_path)))


@pytest.fixture
def production_slot(monkeypatch):
    """The production branch of _backup_slot (conftest forces PREFECT_TEST_MODE, which skips it);
    the Prefect concurrency limit is replaced by a no-op context manager."""
    monkeypatch.delenv("PREFECT_TEST_MODE", raising=False)
    monkeypatch.setattr(flow, "concurrency", lambda *a, **k: contextlib.nullcontext())


U1_STALE_LOCKS = {
    "empty": b"",
    "garbage_text": b"abc",
    "dead_pid": b"999999:0",
    "non_utf8": b"\xff\xfe",
    "live_pid_wrong_create_time": f"{os.getpid()}:1.0".encode(),
}


@pytest.mark.parametrize("name", [
    "empty", "garbage_text", "dead_pid",
    pytest.param("non_utf8", marks=pytest.mark.xfail(
        strict=False,
        reason="SUSPECTED BUG: core/process.py read_lock_alive (~line 116) catches only OSError, so a "
               "non-UTF-8 backup.lock raises UnicodeDecodeError out of acquire_lock and _backup_slot "
               "instead of being taken over")),
    "live_pid_wrong_create_time",
])
def test_malformed_or_stale_backup_lock_is_taken_over(name, tmp_path, production_slot):
    lock = tmp_path / "backup.lock"
    lock.write_bytes(U1_STALE_LOCKS[name])
    seen = {}
    with flow._backup_slot(_slot_config(lock)):
        seen["body_ran"] = True
        seen["content"] = lock.read_bytes()
    assert seen.get("body_ran") is True, "the backup body did not run"
    assert seen["content"] == _own_token().encode(), "the lock was not replaced by our PID:create_time"
    assert not lock.exists(), "the lock was not released at the end of the run"


def test_directory_named_backup_lock_refuses_to_run(tmp_path, production_slot):
    lock = tmp_path / "backup.lock"
    lock.mkdir()
    ran = []
    with pytest.raises(RuntimeError, match="Backup lock"):
        with flow._backup_slot(_slot_config(lock)):
            ran.append(True)
    assert ran == [], "a backup ran without a lock"
    assert lock.is_dir(), "the directory was removed or replaced"


# ── U2 [unit] abandoned sweep with PID reuse ─────────────────────────────────

def test_sweep_abandons_the_pid_reused_row_only_and_alerts_once(tmp_path):
    pid = os.getpid()
    ct = psutil.Process(pid).create_time()
    reused_owner = f"{pid}:{ct - 100:.6f}"      # same PID, different creation time = another process
    live_owner = f"{pid}:{ct:.6f}"              # this very process
    db_path = tmp_path / "m.db"
    (tmp_path / "rt").mkdir()
    db = ManifestDB(str(db_path))
    try:
        for run_id, owner in (("reused", reused_owner), ("live", live_owner)):
            db.insert_run({"fy": "FY26-27", "run_id": run_id, "mode": "lan", "started_at": now_iso(),
                           "status": "LAN_RUNNING", "extended_metrics": json.dumps({"owner": owner})})
        with smtp_server() as srv:
            cfg = _cfg(tmp_path, port=srv.server_address[1])
            first = repo.sweep_abandoned_runs(cfg, db)
            second = repo.sweep_abandoned_runs(cfg, db)
            mails = len(srv.messages)
        statuses = {r["run_id"]: r["status"] for r in db.get_recent_runs(10, fy="FY26-27")}
    finally:
        db.close()

    with contextlib.closing(sqlite3.connect(str(db_path))) as con:
        notif_rows = con.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]

    assert [r["run_id"] for r in first] == ["reused"]
    assert second == [], "the second sweep changed rows again"
    assert statuses == {"reused": "LAN_ABANDONED", "live": "LAN_RUNNING"}
    assert notif_rows == 1, "the abandoned run must produce exactly one alert record"
    assert mails == 1, "exactly one failure mail must reach the SMTP sink"


# ── U8 [unit] watchdog loop survives its own errors ──────────────────────────

class _Halt(BaseException):
    """Ends the endless watchdog loop from inside the test. A BaseException, so the loop's own
    `except Exception` does not swallow it."""


def test_watchdog_loop_survives_a_failing_alert_and_keeps_restarting(monkeypatch):
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="DEBUG")
    try:
        monkeypatch.setattr(watchdog, "FAILURE_THRESHOLD", 1)
        monkeypatch.setattr(watchdog, "_resolve_paths", lambda: None)
        monkeypatch.setattr(watchdog, "_configure_logging", lambda: None)
        monkeypatch.setattr(watchdog, "_check_stale_legs", MagicMock(return_value=[]))
        monkeypatch.setattr(watchdog, "_reload_tunables", lambda mtime: mtime)
        monkeypatch.setattr(watchdog, "_attention_raised", False)
        monkeypatch.setattr(watchdog, "_check_health", lambda: False)          # Prefect API unhealthy
        monkeypatch.setattr(watchdog, "_is_backup_running", lambda: False)
        monkeypatch.setattr(watchdog, "_transfer_process_running", lambda: False)
        monkeypatch.setattr(watchdog, "_service_state", lambda service: "STOPPED")
        start_allowed = MagicMock(side_effect=[False, True, True])   # iteration 1: breaker open
        monkeypatch.setattr(watchdog, "_start_allowed", start_allowed)
        start = MagicMock(return_value=SimpleNamespace(ok=True))
        monkeypatch.setattr(watchdog.services, "start", start)

        # The alert of iteration 1 fails on its first call, like a locked manifest DB.
        # NOTE: watchdog._notify wraps core.notify.notify and swallows exceptions at WARNING, so a
        # failure raised inside core.notify never reaches the loop; here the failure is raised out
        # of the alert call site so the loop's own iteration handler is what is under test.
        notify_calls = []

        def failing_notify(event_kwargs):
            notify_calls.append(event_kwargs)
            if len(notify_calls) == 1:
                raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(watchdog, "_notify", failing_notify)

        sleeps = []

        def counting_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise _Halt()

        monkeypatch.setattr(watchdog, "time", SimpleNamespace(
            sleep=counting_sleep, monotonic=time.monotonic, time=time.time))

        with pytest.raises(_Halt):
            watchdog.main()
    finally:
        logger.remove(sink)

    assert len(sleeps) == 3, "the loop did not run three iterations"
    assert start_allowed.call_count == 3
    assert start.call_count == 2, "iterations 2 and 3 must still start the stopped service"
    assert all(call.args[0] == watchdog.WATCHED_SERVICE for call in start.call_args_list)
    loop_errors = [r for r in records
                   if r["level"].name == "ERROR" and "Watchdog main loop error" in r["message"]]
    assert len(loop_errors) == 1, "one ERROR line per failed iteration is expected"
    assert "database is locked" in str(loop_errors[0]["exception"].value)


# ── U10 [unit] old-schema manifest.db at boot ────────────────────────────────

def _old_schema_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE run_history(id INTEGER)")
        con.execute("PRAGMA user_version=5")
        con.commit()
    finally:
        con.close()


def _no_sidecars(path: Path) -> bool:
    return not any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))


def test_old_schema_manifest_is_refused_and_left_byte_identical(tmp_path):
    db_file = tmp_path / "manifest.db"
    _old_schema_db(db_file)
    before = _sha(db_file)
    cfg = SimpleNamespace(paths=SimpleNamespace(database_path=str(db_file)),
                          maintenance=MaintenanceConfig())
    db = ManifestDB.from_config(cfg)
    try:
        with pytest.raises(ManifestSchemaError, match="schema version 5"):
            db.get_recent_runs(10, fy="FY26-27")
    finally:
        db.close()
    assert _sha(db_file) == before, "the old database was modified"
    assert _no_sidecars(db_file), "a -wal/-shm/-journal file was created beside the old database"


@pytest.mark.xfail(strict=False, reason=(
    "SUSPECTED BUG: launch._mark_abandoned_runs (launch.py ~line 123-141) catches every exception as "
    "'non-fatal', so main() goes on to prefect serve() with an old-schema manifest.db; the scheduler "
    "starts while the history cannot be read"))
def test_launch_exits_before_prefect_serve_on_old_schema(tmp_path, monkeypatch, capsys):
    import models.config as models_config

    db_file = tmp_path / "manifest.db"
    _old_schema_db(db_file)
    before = _sha(db_file)
    cfg = SimpleNamespace(paths=SimpleNamespace(database_path=str(db_file)),
                          maintenance=MaintenanceConfig(),
                          dashboard=SimpleNamespace(bind_address="127.0.0.1", port=8080))
    serve_calls = MagicMock()

    monkeypatch.setattr(models_config, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(launch, "_check_prefect_api", lambda *a, **k: True)
    monkeypatch.setattr(launch, "_run_dashboard", lambda: None)
    monkeypatch.setattr(launch, "_supervise_dashboard", lambda thread, *a, **k: thread)
    monkeypatch.setattr(launch, "_ensure_concurrency_limit", MagicMock())
    monkeypatch.setattr(launch, "_cancel_orphaned_runs", MagicMock())
    monkeypatch.setattr(launch, "_reconcile_disabled_legs", MagicMock(return_value={}))
    monkeypatch.setattr(prefect, "serve", serve_calls)
    monkeypatch.setitem(sys.modules, "serve", SimpleNamespace(deployments=lambda: []))

    with pytest.raises(SystemExit) as exit_info:
        launch.main()

    out = capsys.readouterr().out
    assert exit_info.value.code not in (0, None), "launch must exit non-zero on an old schema"
    assert "schema version 5" in out, "the boot output must name the schema version"
    serve_calls.assert_not_called()
    assert _sha(db_file) == before
    assert _no_sidecars(db_file)
