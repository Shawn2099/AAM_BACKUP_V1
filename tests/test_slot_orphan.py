"""AAM-SYS-08: the backup slot is taken but no backup is running (found on the trial box
2026-10-22: a Prefect server restart orphaned the slot and every backup timed out waiting).

A slot wait that times out while NO live process holds the backup lock is told apart from the
ordinary "another backup was still running" (AAM-SYS-01): the row and the alert say that the slot
is stuck and that support must reset it."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from prefect.concurrency._asyncio import AcquireConcurrencySlotTimeoutError

import flow
from core import faults
from core.manifest import ManifestDB
from core.process import acquire_lock
from models.config import MaintenanceConfig
from core.time_utils import now_iso


def _cfg(tmp_path):
    cfg = MagicMock()
    cfg.paths.database_path = str(tmp_path / "m.db")
    cfg.paths.backup_lock_path = tmp_path / "backup.lock"
    cfg.maintenance = MaintenanceConfig()
    return cfg


@contextmanager
def _timing_out_slot(config):
    raise AcquireConcurrencySlotTimeoutError(3600)
    yield  # pragma: no cover


def _run(cfg, leg="cloud"):
    with patch("flow._backup_slot", _timing_out_slot), patch("flow.notify") as notify, \
         pytest.raises(AcquireConcurrencySlotTimeoutError):
        with flow._pipeline_slot(cfg, leg, "run-o", now_iso()):
            pass
    db = ManifestDB(cfg.paths.database_path)
    try:
        row = db.last_run(leg)
    finally:
        db.close()
    [event] = [c.args[1] for c in notify.call_args_list if c.args[1].kind == "FAILURE"]
    return row, event


def test_timeout_with_no_live_backup_is_reported_as_a_stuck_slot(tmp_path):
    row, event = _run(_cfg(tmp_path))
    assert row["status"] == "CLOUD_SLOT_TIMEOUT"
    assert "no backup is running" in row["error_message"]
    assert event.fault_code == "AAM-SYS-08"
    assert "no backup is running" in event.message


def test_timeout_while_a_live_backup_holds_the_lock_stays_the_ordinary_code(tmp_path):
    cfg = _cfg(tmp_path)
    assert acquire_lock(cfg.paths.backup_lock_path)         # this test process = the live holder
    row, event = _run(cfg, "lan")
    assert row["status"] == "LAN_SLOT_TIMEOUT"
    assert "no backup is running" not in row["error_message"]
    assert event.fault_code == "AAM-SYS-01"


def test_a_stale_lock_of_a_dead_process_counts_as_no_backup_running(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.paths.backup_lock_path.write_text("999999:1.0", encoding="utf-8")
    row, event = _run(cfg)
    assert event.fault_code == "AAM-SYS-08"


def test_stuck_slot_fault_is_in_the_catalog_with_a_recovery_step():
    fault = faults.get("AAM-SYS-08")
    assert fault.severity == faults.PROBLEM and fault.steps
    assert faults.classify_run("cloud", "CLOUD_SLOT_TIMEOUT") == "AAM-SYS-01"   # default unchanged
