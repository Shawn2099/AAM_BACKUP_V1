"""Plan P1 chunk R8c: flow-level rows of the S3 nightly hash check that the existing tests do
not cover (docs/TESTMATRIX_P1_REVIEW_2026-11-25.md section 8: A12, A12b, A13 flow part,
A13h, A13i).

Each row runs flow._run_lan_pipeline end to end through the same world as
tests/test_p1_c9_lan_pipeline.py (fake_rclone_lan as the only rclone, every other collaborator
a mock). The helpers are imported from that module, not copied.

Rows here expect the plan's fail-closed behaviour. A13i is red on the current code: an
unreadable copy log is recorded as a skip (`ran` False) and the run stays LAN_COMPLETE, while
plan section 8 says the verdict becomes not verified (LAN_SUSPECT). Flagged for Opus.
"""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import flow
from core import lan_sync
from test_p1_c9_lan_pipeline import (  # noqa: F401 - shared world and fixtures of the C9 rows
    CHECK,
    HASH,
    PLAN,
    SYNC,
    _fx,
    _hash_metrics,
    check_clean,
    check_missing,
    clean_plan,
    copy_log,
    failures,
    hash_clean,
    lan_world,
    make_config,
    run_lan,
    steps,
    sync_ok,
)


def _md5diff_hash() -> dict:
    """Real hash check of R8b: new.txt differs (same size, other bytes), exit 1."""
    return {"exit_code": 1, "stderr": _fx("lan_hash_md5diff.stderr.txt"),
            "combined": _fx("lan_hash_md5diff_combined.txt")}


# ── A12 (flow) a hash mismatch on a real file is SUSPECT, one alert, no shutdown ──

@pytest.mark.parametrize("state", ["absent", "old_mtime"])
def test_a12_hash_mismatch_on_a_missing_or_unchanged_file_is_suspect(tmp_path, state):
    cfg = make_config(tmp_path)
    src = Path(cfg.paths.source_drive)
    if state == "old_mtime":
        # new.txt exists on the source but was last written an hour before the sync began
        (src / "new.txt").write_bytes(b"N" * 300)
        past = time.time() - 3600
        os.utime(src / "new.txt", (past, past))
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: _md5diff_hash()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_SUSPECT", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, flow.PartialRun), "SUSPECT must raise, never a Prefect COMPLETED"
    assert steps(w) == [PLAN, SYNC, CHECK, HASH]
    alerts = failures(w)
    assert len(alerts) == 1, [a.message for a in alerts]
    assert alerts[0].fault_code == "AAM-LAN-04"
    assert w.shutdown.call_count == 0
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is True
    assert hash_["verified"] is False


# ── A12b (flow) a hash difference explained by a change during the backup is COMPLETE ──

def test_a12b_hash_difference_from_a_change_during_the_backup_is_complete(tmp_path):
    cfg = make_config(tmp_path)
    src = Path(cfg.paths.source_drive)
    (src / "new.txt").write_bytes(b"N" * 300)
    future = time.time() + 3600   # written after the sync began: changed during the backup
    os.utime(src / "new.txt", (future, future))
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: _md5diff_hash()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert out.status == "LAN_COMPLETE"
    assert steps(w) == [PLAN, SYNC, CHECK, HASH]
    assert failures(w) == []
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is True
    assert hash_["verified"] is True


# ── A13 (flow) hash timeout: SUSPECT, AAM-LAN-04, one alert, no shutdown ──

def test_a13_hash_timeout_is_suspect_one_alert_and_no_shutdown(tmp_path):
    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(),
              HASH: {"exit_code": None, "timed_out": True}}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_SUSPECT", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, flow.PartialRun)
    assert steps(w) == [PLAN, SYNC, CHECK, HASH]
    alerts = failures(w)
    assert len(alerts) == 1, [a.message for a in alerts]
    assert alerts[0].fault_code == "AAM-LAN-04"
    assert w.shutdown.call_count == 0
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is True
    assert hash_["verified"] is False
    assert "timed out" in hash_["reason"]


# ── A13h cap boundary: copies of exactly lan.arrival_hash_max_gb still run the hash ──

def test_a13h_copies_of_exactly_the_size_cap_still_run_the_hash(tmp_path):
    one_gib = 1024 ** 3
    cfg = make_config(tmp_path, arrival_hash_max_gb=1)
    script = {PLAN: clean_plan(), SYNC: {"exit_code": 0, "log": copy_log([("big.bin", one_gib)])},
              CHECK: check_clean(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert out.status == "LAN_COMPLETE"
    assert steps(w) == [PLAN, SYNC, CHECK, HASH], "bytes == cap is not over the cap"
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is True
    assert hash_["bytes"] == one_gib


# ── A13i unreadable sync log: the hash cannot vouch for the copies, so not verified ──

def _drop_log_after_check(inner):
    """Wraps the run_tool mock: the sync's JSON copy log is removed from disk once the arrival
    check has run. The sync itself has already parsed its log by then (removing it earlier
    makes the sync fail on its own), so the only reader left is the hash plan."""
    seen = {}

    def run(name, argv, **kw):
        if name == SYNC:
            seen["log"] = kw.get("log_path")
            assert seen["log"] is not None, "the sync step must pass its log path"
        result = inner(name, argv, **kw)
        if name == CHECK:
            Path(seen["log"]).unlink()
        return result
    return run


def test_a13i_unreadable_sync_log_is_not_verified_and_is_suspect(tmp_path):
    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w, \
            patch("core.lan_sync.run_tool", side_effect=_drop_log_after_check(lan_sync.run_tool)):
        out = run_lan(w, cfg)
    assert out.status == "LAN_SUSPECT", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, flow.PartialRun)
    assert steps(w) == [PLAN, SYNC, CHECK], "no hash without the copy list"
    alerts = failures(w)
    assert len(alerts) == 1
    assert alerts[0].fault_code == "AAM-LAN-04"
    assert w.shutdown.call_count == 0
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is False
    assert "could not read tonight's sync log" in hash_["reason"]


# ── A14 (flow) the hash list cannot be written: SUSPECT, one alert, no shutdown, no crash ──

def test_a14_list_file_write_error_is_suspect_one_alert_and_no_shutdown(tmp_path):
    real_write = Path.write_bytes

    def failing_write(self, data):
        if "rclone-lan-hash-files" in self.name:
            raise OSError(28, "No space left on device")
        return real_write(self, data)

    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w, \
            patch.object(Path, "write_bytes", failing_write):
        out = run_lan(w, cfg)
    assert out.status == "LAN_SUSPECT", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, flow.PartialRun)
    assert steps(w) == [PLAN, SYNC, CHECK], "no hash rclone call without a list file"
    alerts = failures(w)
    assert len(alerts) == 1, [a.message for a in alerts]
    assert alerts[0].fault_code == "AAM-LAN-04"
    assert w.shutdown.call_count == 0
    hash_ = _hash_metrics(out)
    assert hash_["verified"] is False
    assert hash_["reason"].startswith("hash check could not start:")


# ── M1 metrics.arrival.hash carries the full hash result keys (matrix section 8, Return) ──

HASH_METRIC_KEYS = {"ran", "verified", "files", "bytes", "matching", "missing", "differ",
                    "changed_during_backup", "reason", "unverified_paths"}


def test_m1_hash_metrics_keys_present_when_the_hash_ran_clean(tmp_path):
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: hash_clean()}
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert steps(w) == [PLAN, SYNC, CHECK, HASH]
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is True
    assert HASH_METRIC_KEYS <= set(hash_), f"missing keys: {sorted(HASH_METRIC_KEYS - set(hash_))}"
    # rclone's own 'N matching files' count from the hash check stderr (fixture lan_hash_clean: 3), not the copied-file count
    assert hash_["matching"] == 3
    assert hash_["unverified_paths"] == []


def test_m1_hash_metrics_keys_present_when_the_hash_ran_with_a_mismatch(tmp_path):
    cfg = make_config(tmp_path)
    src = Path(cfg.paths.source_drive)
    (src / "new.txt").write_bytes(b"N" * 300)
    past = time.time() - 3600
    os.utime(src / "new.txt", (past, past))
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: _md5diff_hash()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert steps(w) == [PLAN, SYNC, CHECK, HASH]
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is True
    assert hash_["verified"] is False
    assert HASH_METRIC_KEYS <= set(hash_), f"missing keys: {sorted(HASH_METRIC_KEYS - set(hash_))}"
    assert hash_["unverified_paths"] == ["new.txt"]
