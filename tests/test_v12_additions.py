"""v1.2 additions - R4 (sqlite synchronous toggle) + C-A (rclone duration cap).

R4: WAL + synchronous=NORMAL is already explicit in the DDL; the addition is
the operator-facing maintenance.sqlite_synchronous key (normal|full) with a
pragma-readback assertion (DB-18 contract).
C-A: cloud sync passes --max-duration (+ --cutoff-mode SOFT) so rclone
self-terminates gracefully inside its window instead of being hard-killed by
the subprocess timeout; default auto = subprocess_timeout_seconds - 300s
margin; 0 disables. retries-sleep is already pinned (30s) so retries cannot
silently reset the deadline.
"""
import sqlite3

import pytest


# ── R4 ────────────────────────────────────────────────────────────────────────

def test_db18_pragma_normal_readback(tmp_path):
    from core.manifest import ManifestDB

    db = ManifestDB(str(tmp_path / "m18a.db"), synchronous="normal")
    try:
        mode = db._get_conn().execute("PRAGMA synchronous").fetchone()[0]
    finally:
        db.close()
    assert mode == 1, f"expected NORMAL(1), got {mode}"


def test_db18_pragma_full_readback(tmp_path):
    from core.manifest import ManifestDB

    db = ManifestDB(str(tmp_path / "m18b.db"), synchronous="full")
    try:
        mode = db._get_conn().execute("PRAGMA synchronous").fetchone()[0]
    finally:
        db.close()
    assert mode == 2, f"expected FULL(2), got {mode}"


def test_maintenance_config_accepts_toggle():
    from models.config import MaintenanceConfig

    assert MaintenanceConfig().sqlite_synchronous == "normal"
    assert MaintenanceConfig(sqlite_synchronous="full").sqlite_synchronous == "full"
    with pytest.raises(Exception):
        MaintenanceConfig(sqlite_synchronous="off")


# ── C-A ───────────────────────────────────────────────────────────────────────

def _cmd(**kwargs):
    from core.cloud_sync import build_rclone_sync_command

    return build_rclone_sync_command(
        "C:\\src", "bucket", "FY26-27", "/tmp/cfg", "STANDARD", **kwargs
    )


def test_max_duration_flag_present_when_set():
    cmd = _cmd(max_duration_seconds=7200)
    idx = cmd.index("--max-duration")
    assert cmd[idx + 1] == "7200s"
    assert "SOFT" in cmd[cmd.index("--cutoff-mode") + 1]


def test_no_duration_flag_when_absent():
    cmd = _cmd()
    assert "--max-duration" not in cmd


def test_auto_margin_from_timeout():
    from core.cloud_sync import resolve_max_duration_seconds

    assert resolve_max_duration_seconds(timeout=21600, configured=None) == 21300
    # explicit config wins
    assert resolve_max_duration_seconds(timeout=21600, configured=9000) == 9000
    # 0 disables the cap entirely
    assert resolve_max_duration_seconds(timeout=21600, configured=0) is None
    # tiny timeouts leave no margin -> no flag rather than a negative one
    assert resolve_max_duration_seconds(timeout=120, configured=None) is None
