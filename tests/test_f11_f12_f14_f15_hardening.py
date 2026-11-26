"""F11, F12, F14, F15: config_file fsync, lock PermissionError/race, G5 approval match, catching_up cap."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pendulum
from ruamel.yaml import YAML

from core import approvals, attention, config_file, process
from core.manifest import ManifestDB
from models.config import load_config


def test_f11_write_atomic_calls_fsync(tmp_path):
    target = tmp_path / "config.yaml"
    y = YAML()
    fsync_calls = []
    real_fsync = os.fsync

    def spy_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    with patch("os.fsync", side_effect=spy_fsync):
        config_file.write_atomic(str(target), y, {"firm_name": "Test"}, tmp_prefix="test-")

    assert len(fsync_calls) == 1
    assert target.exists()


def test_f12_read_lock_alive_retries_transient_permission_error(tmp_path):
    lock = tmp_path / "backup.lock"
    lock.write_text(process.process_token(), encoding="utf-8")
    calls = {"n": 0}
    real_read = Path.read_text

    def flaky_read(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("AV scan")
        return real_read(self, *args, **kwargs)

    with patch.object(Path, "read_text", flaky_read):
        alive, pid = process.read_lock_alive(lock)

    assert alive is True
    assert pid == os.getpid()
    assert calls["n"] == 2


def test_f14_g5_approval_requires_matching_destination_has_files():
    now = "2026-11-26T10:00:00+05:30"
    expires = "2026-11-28T10:00:00+05:30"
    req = {
        "leg": "lan",
        "guard": "G5_CANARY",
        "approved_at": "2026-11-26T09:00:00+05:30",
        "expires_at": expires,
        "approved_metrics": {"destination": r"\\nas\share\FY26-27", "destination_has_files": False},
    }
    dec = approvals.evaluate_approval(
        req, leg="lan", guard="G5_CANARY",
        fresh_metrics={"destination": r"\\nas\share\FY26-27", "destination_has_files": True},
        now_iso=now, tolerance_percent=10.0,
    )
    assert dec.allowed is False
    assert "destination_has_files" in dec.reason


def test_f15_catching_up_expires_when_last_partial_run_is_older_than_2_days(tmp_path):
    cfg = load_config()
    db = ManifestDB(tmp_path / "m.db")
    try:
        old_ts = "2026-11-20T02:01:00+05:30"
        db.insert_run({
            "run_id": "r-old-partial",
            "mode": "cloud",
            "started_at": old_ts,
            "ended_at": old_ts,
            "status": "CLOUD_PARTIAL",
            "bytes_copied": 1024,
            "extended_metrics": '{"budget": {"stopped": true}}',
        })
        now = pendulum.parse("2026-11-23T10:00:00+05:30")  # > 3 days after old_ts
        assert attention._progressing_partial(db, cfg, "cloud", None, now) is False

        fresh_now = pendulum.parse("2026-11-20T12:00:00+05:30")  # 10 hours after old_ts
        assert attention._progressing_partial(db, cfg, "cloud", None, fresh_now) is True
    finally:
        db.close()
