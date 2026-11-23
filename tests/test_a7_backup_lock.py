"""A7 (final audit 2026-10-20): backup.lock is mandatory and released by ownership.

Verified defects: (1) a transient unlink failure left a lock holding THIS process's own live PID, so the
next pipeline of the same flow process (mode=all, rollover final cloud + final LAN) failed with
"Backup lock is held by a live process"; (2) a lock that could not be written let the run go on lockless,
so the watchdog could not see it; (3) the tests force PREFECT_TEST_MODE, a branch that never touches the
production lock code - these tests run the shared helper and the production branch of `_backup_slot`."""

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from pydantic import ValidationError

import flow
from core.process import process_token, read_lock_alive
from models.config import MaintenanceConfig, model_default


def _config(lock_path, retries=3, delay_ms=10):
    return SimpleNamespace(
        maintenance=SimpleNamespace(concurrency_wait_seconds=1, lock_release_retries=retries,
                                    lock_release_delay_ms=delay_ms),
        paths=SimpleNamespace(backup_lock_path=Path(lock_path)))


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "backup.lock"


@contextlib.contextmanager
def _flaky_unlink(monkeypatch, fail_times):
    """Path.unlink raises PermissionError `fail_times` times (None = always), like an AV scan or a
    watchdog read holding the file open on Windows."""
    real = Path.unlink
    calls = {"n": 0}

    def unlink(self, *a, **k):
        calls["n"] += 1
        if fail_times is None or calls["n"] <= fail_times:
            raise PermissionError("file is in use")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink)
    yield calls


def test_release_retries_a_transient_failure_and_removes_the_lock(lock_path, monkeypatch):
    with _flaky_unlink(monkeypatch, fail_times=2):
        with flow._hold_backup_lock(_config(lock_path), required=True):
            assert lock_path.exists()
    assert not lock_path.exists(), "the lock was left behind after a transient unlink failure"


def test_a_stuck_release_does_not_block_the_next_pipeline_of_this_process(lock_path, monkeypatch):
    with _flaky_unlink(monkeypatch, fail_times=None):
        with flow._hold_backup_lock(_config(lock_path, retries=1), required=True):
            pass
    assert lock_path.exists() and read_lock_alive(lock_path) == (True, os.getpid())   # the leftover
    monkeypatch.undo()
    with flow._hold_backup_lock(_config(lock_path), required=True):     # next pipeline, same process
        assert read_lock_alive(lock_path) == (True, os.getpid())
    assert not lock_path.exists()


def test_a_lock_of_another_live_process_is_never_taken_over(lock_path):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        created = psutil.Process(other.pid).create_time()
        lock_path.write_text(f"{other.pid}:{created:.6f}", encoding="utf-8")
        body = []
        with pytest.raises(RuntimeError, match="held by a live process"):
            with flow._hold_backup_lock(_config(lock_path), required=True):
                body.append("ran")
        assert not body and lock_path.exists(), "a live foreign lock must stay untouched"
    finally:
        other.kill()
        other.wait()


def test_an_unwritable_lock_stops_the_run_before_any_work(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")                      # the lock's parent is a FILE: the lock cannot be written
    body = []
    with pytest.raises(RuntimeError, match="could not be written"):
        with flow._hold_backup_lock(_config(blocker / "backup.lock"), required=True):
            body.append("ran")
    assert not body


def test_unit_test_mode_keeps_running_without_a_writable_lock(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    with flow._hold_backup_lock(_config(blocker / "backup.lock"), required=False):
        pass                                     # tests with mock configs have no real lock path


def test_production_branch_of_backup_slot_is_mandatory_and_releases(lock_path, tmp_path, monkeypatch):
    monkeypatch.delenv("PREFECT_TEST_MODE", raising=False)
    monkeypatch.setattr(flow, "concurrency", lambda *a, **k: contextlib.nullcontext())
    with flow._backup_slot(_config(lock_path)):
        assert read_lock_alive(lock_path) == (True, os.getpid())
    assert not lock_path.exists()

    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    body = []
    with pytest.raises(RuntimeError, match="could not be written"):
        with flow._backup_slot(_config(blocker / "backup.lock")):
            body.append("ran")
    assert not body, "the production branch ran a backup without backup.lock"


def test_lock_release_keys_are_bounded_config_keys():
    assert model_default(MaintenanceConfig, "lock_release_retries") >= 1
    assert model_default(MaintenanceConfig, "lock_release_delay_ms") > 0
    for field, bad in (("lock_release_retries", -1), ("lock_release_retries", 1000),
                       ("lock_release_delay_ms", 0), ("lock_release_delay_ms", 10**6)):
        with pytest.raises(ValidationError):
            MaintenanceConfig(**{field: bad})
