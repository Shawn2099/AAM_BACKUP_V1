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

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
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

import flow
from core import lan_sync


def _md5diff_hash() -> dict:
    """Real hash check of the twelve lan_clean files (R11 fixture): a.txt differs (same size,
    other bytes), exit 1. The list is 12 long, so the count matches the copies (R11 rule)."""
    return {"exit_code": 1, "stderr": _fx("lan_hash12_md5diff.stderr.txt"),
            "combined": _fx("lan_hash12_md5diff_combined.txt")}


# ── A12 (flow) a hash mismatch on a real file is SUSPECT, one alert, no shutdown ──

@pytest.mark.parametrize("state", ["absent", "old_mtime"])
def test_a12_hash_mismatch_on_a_missing_or_unchanged_file_is_suspect(tmp_path, state):
    cfg = make_config(tmp_path)
    src = Path(cfg.paths.source_drive)
    if state == "old_mtime":
        # a.txt exists on the source but was last written an hour before the sync began
        (src / "a.txt").write_bytes(b"N" * 300)
        past = time.time() - 3600
        os.utime(src / "a.txt", (past, past))
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
    (src / "a.txt").write_bytes(b"N" * 300)
    future = time.time() + 3600   # written after the sync began: changed during the backup
    os.utime(src / "a.txt", (future, future))
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
              CHECK: check_clean(), HASH: hash_clean(1)}
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
    assert set(hash_) >= HASH_METRIC_KEYS, f"missing keys: {sorted(HASH_METRIC_KEYS - set(hash_))}"
    # rclone's own 'N matching files' count from the hash check stderr (fixture lan_hash12_clean:
    # 12, the same as the copied-file count of lan_clean)
    assert hash_["matching"] == 12
    assert hash_["unverified_paths"] == []


def test_m1_hash_metrics_keys_present_when_the_hash_ran_with_a_mismatch(tmp_path):
    cfg = make_config(tmp_path)
    src = Path(cfg.paths.source_drive)
    (src / "a.txt").write_bytes(b"N" * 300)
    past = time.time() - 3600
    os.utime(src / "a.txt", (past, past))
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: _md5diff_hash()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert steps(w) == [PLAN, SYNC, CHECK, HASH]
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is True
    assert hash_["verified"] is False
    assert set(hash_) >= HASH_METRIC_KEYS, f"missing keys: {sorted(HASH_METRIC_KEYS - set(hash_))}"
    assert hash_["unverified_paths"] == ["a.txt"]


# ── R11 (Opus rulings 2026-11-25): flow rows ─────────────────────────────

def _stats_log(copies, transfers: int) -> str:
    """Synthetic sync log: one info `Copied (new)` per (name, size) in `copies`, then the stats
    line with the `transfers` count rclone reports (layout of the lan_clean stats line)."""
    lines = [json.dumps({"level": "info", "msg": "Copied (new)", "object": name, "size": size,
                         "objectType": "*local.Object"}) for name, size in copies]
    total = sum(size for _name, size in copies)
    lines.append(json.dumps({"level": "info", "msg": "stats", "stats": {
        "bytes": total, "transfers": transfers, "deletes": 0, "errors": 0, "renames": 0,
        "checks": 0}}))
    return "\n".join(lines) + "\n"


# ── A12d (flow): listed files rclone silently skipped: SUSPECT, one alert, no shutdown ──

def test_a12d_silently_skipped_listed_files_are_suspect_one_alert_and_no_shutdown(tmp_path):
    cfg = make_config(tmp_path)
    silent = {"exit_code": 0, "stderr": _fx("lan_hash_silent.stderr.txt"),
              "combined": _fx("lan_hash_silent_combined.txt")}
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: silent}
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
    assert "covered 0 of 12" in hash_["reason"]


# ── A13j (flow): stats say transfers, the log lists fewer copies: fail closed ──

def test_a13j_transfers_reported_but_no_copied_files_is_suspect_not_nothing_copied(tmp_path):
    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(), SYNC: {"exit_code": 0, "log": _stats_log([], 3)},
              CHECK: check_clean(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_SUSPECT", f"status {out.status}; exc {out.exc!r}"
    assert isinstance(out.exc, flow.PartialRun)
    assert steps(w) == [PLAN, SYNC, CHECK], "no hash without a copy list"
    alerts = failures(w)
    assert len(alerts) == 1, [a.message for a in alerts]
    assert alerts[0].fault_code == "AAM-LAN-04"
    assert w.shutdown.call_count == 0
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is False
    assert hash_["verified"] is False
    assert "transfers reported but no copied files in the log" in hash_["reason"]


def test_a13j_fewer_listed_copies_than_reported_transfers_is_suspect(tmp_path):
    cfg = make_config(tmp_path)
    log = _stats_log([("x1.txt", 5), ("x2.txt", 7)], 3)   # two copies listed, 3 reported
    script = {PLAN: clean_plan(), SYNC: {"exit_code": 0, "log": log},
              CHECK: check_clean(), HASH: hash_clean(2)}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_SUSPECT", f"status {out.status}; exc {out.exc!r}"
    assert steps(w) == [PLAN, SYNC, CHECK], "fewer copies than transfers: no hash"
    alerts = failures(w)
    assert len(alerts) == 1, [a.message for a in alerts]
    assert alerts[0].fault_code == "AAM-LAN-04"
    assert w.shutdown.call_count == 0
    hash_ = _hash_metrics(out)
    assert hash_["ran"] is False
    assert hash_["verified"] is False
    assert "transfers" in hash_["reason"]


# ── Matrix section 8: metrics.arrival.verified is the hash verdict when the hash ran ──

def test_m8_hash_mismatch_makes_metrics_arrival_verified_false(tmp_path):
    cfg = make_config(tmp_path)
    src = Path(cfg.paths.source_drive)
    (src / "a.txt").write_bytes(b"N" * 300)
    past = time.time() - 3600
    os.utime(src / "a.txt", (past, past))
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: _md5diff_hash()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_SUSPECT", f"status {out.status}; exc {out.exc!r}"
    assert _hash_metrics(out)["verified"] is False
    assert out.metrics["arrival"]["verified"] is False


def test_m8_clean_hash_makes_metrics_arrival_verified_true(tmp_path):
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: hash_clean()}
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert _hash_metrics(out)["verified"] is True
    assert out.metrics["arrival"]["verified"] is True


def test_m8_hash_timeout_makes_metrics_arrival_verified_false(tmp_path):
    cfg = make_config(tmp_path)
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(),
              HASH: {"exit_code": None, "timed_out": True}}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_SUSPECT", f"status {out.status}; exc {out.exc!r}"
    assert _hash_metrics(out)["ran"] is True
    assert out.metrics["arrival"]["verified"] is False


def test_m8_hash_not_run_keeps_the_size_only_verdict_when_nothing_copied(tmp_path):
    script = {PLAN: clean_plan(), SYNC: {"exit_code": 0, "log": _fx("lan_nochange.jsonl")},
              CHECK: check_clean(), HASH: hash_clean()}
    cfg = make_config(tmp_path)
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert _hash_metrics(out)["ran"] is False
    assert out.metrics["arrival"]["verified"] is True


def test_m8_hash_switched_off_keeps_the_size_only_verdict(tmp_path):
    cfg = make_config(tmp_path, arrival_hash_transferred=False)
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_clean(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.exc is None, f"exc {out.exc!r}"
    assert _hash_metrics(out)["ran"] is False
    assert out.metrics["arrival"]["verified"] is True


def test_m8_failed_size_only_check_is_verified_false_and_no_hash(tmp_path):
    cfg = make_config(tmp_path, modify_window_seconds=0)
    time.sleep(0.3)   # sub/deep/c.txt is older than the sync: the size-only check fails
    script = {PLAN: clean_plan(), SYNC: sync_ok(), CHECK: check_missing(), HASH: hash_clean()}
    with lan_world(tmp_path, script) as w:
        out = run_lan(w, cfg)
    assert out.status == "LAN_SUSPECT", f"status {out.status}; exc {out.exc!r}"
    assert _hash_metrics(out)["ran"] is False
    assert out.metrics["arrival"]["verified"] is False
