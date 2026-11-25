"""M3 3.2 — metrics from the tools' own accounting (plan WP6).

Findings: the cloud report said "579 files, 57 MB copied" every no-change
night (the LAN leg overwrote the shared mtime column; ISO vs float compare);
exit-0 runs reported 0 files; LAN copied counts were re-derived from a
snapshot diff. Now: rclone final stats / robocopy summary are the numbers,
the diff is only a cross-check, unknown is NULL, and size/mtime are per leg.
"""

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

import flow
from core.manifest import SCHEMA_VERSION, ManifestDB
from core.run_artefacts import RunArtefacts
from models.config import LanConfig
from test_support.fake_tools import fake_run_tool

# Real rclone v1.74.3 final stats line (probe 2026-10-15): 1 new + 1 changed
# file uploaded (5002 bytes), 1 extra deleted.
STATS_LINE = ('{"level":"info","msg":"\\nTransferred: ...","stats":{"bytes":5002,"checks":2,'
              '"deletedDirs":0,"deletes":1,"elapsedTime":0.0027857,"errors":0,"eta":null,'
              '"fatalError":false,"listed":4,"renames":0,"retryError":false,"speed":0,'
              '"totalBytes":5002,"totalChecks":2,"totalTransfers":2,"transferTime":0.0022282,'
              '"transfers":2},"source":"accounting/stats.go:551"}')


# ── rclone final stats ────────────────────────────────────────────────────

def test_final_stats_reads_the_last_stats_line():
    from core.cloud_sync import final_stats
    early = STATS_LINE.replace('"transfers":2', '"transfers":1').replace('"bytes":5002', '"bytes":1')
    stats = final_stats("\n".join([early, "garbage", '{"level":"info","msg":"x"}', STATS_LINE]))
    assert (stats["transfers"], stats["bytes"], stats["deletes"], stats["errors"]) == (2, 5002, 1, 0)
    assert final_stats("") is None and final_stats('{"msg":"no stats"}') is None


def test_run_cloud_sync_returns_the_final_stats(tmp_path):
    import contextlib

    from core.cloud_sync import run_cloud_sync

    @contextlib.contextmanager
    def fake_cfg(*a, **k):
        yield "/c"

    (tmp_path / "f.txt").write_text("x")
    with patch("core.cloud_sync.temp_rclone_config", fake_cfg), \
         fake_run_tool("core.cloud_sync", exit_code=0, log=STATS_LINE):
        res = run_cloud_sync(source=str(tmp_path), bucket="b", fy_prefix="FY26-27",
                             gcs_key_path="/k", project_number="1", storage_class="STANDARD")
    assert res["stats"]["transfers"] == 2 and res["stats"]["bytes"] == 5002


# ── schema v2: size/mtime per leg ─────────────────────────────────────────

def test_schema_v2_has_per_leg_columns(tmp_path):
    assert SCHEMA_VERSION >= 2
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        db.file_count()
        conn = sqlite3.connect(str(tmp_path / "m.db"))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(file_entries)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
    finally:
        db.close()
    assert {"lan_size", "lan_mtime", "cloud_size", "cloud_mtime"} <= cols
    assert version == SCHEMA_VERSION


def test_v1_database_is_migrated_without_inventing_per_leg_values(tmp_path):
    path = str(tmp_path / "v1.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE file_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            relative_path TEXT NOT NULL UNIQUE COLLATE NOCASE,
            file_size INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0,
            md5_checksum TEXT DEFAULT 'pending', lan_status TEXT DEFAULT 'unknown',
            cloud_status TEXT DEFAULT 'unknown', lan_last_synced_at TEXT,
            cloud_last_synced_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE run_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL UNIQUE,
            mode TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
            status TEXT NOT NULL, exit_code INTEGER, files_copied INTEGER DEFAULT 0,
            bytes_copied INTEGER DEFAULT 0, files_failed INTEGER DEFAULT 0,
            duration_seconds REAL, error_message TEXT, extended_metrics TEXT);
        INSERT INTO file_entries (relative_path, file_size, mtime, lan_status, cloud_status,
                                  created_at, updated_at)
        VALUES ('a.txt', 10, 1.0, 'synced', 'synced', 'x', 'x');
        PRAGMA user_version = 1;
    """)
    conn.close()
    db = ManifestDB(path)
    try:
        assert db.get_cloud_synced_entries() == {"a.txt": (None, None)}   # unknown, not guessed
    finally:
        db.close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_lan_writes_never_change_the_cloud_baseline(tmp_path):
    """The '579 files copied every night' bug: the LAN leg overwrote the
    shared mtime the cloud diff compared against."""
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        db.bulk_upsert_synced([{"path": "a.txt", "size": 10,
                                "mtime": "2026-10-01T10:00:00Z"}], "cloud")
        db.bulk_upsert_synced([{"path": "a.txt", "size": 10, "mtime": 1727777777.5}], "lan")
        assert db.get_cloud_synced_entries() == {"a.txt": (10, "2026-10-01T10:00:00Z")}
    finally:
        db.close()


# ── pipelines: tool counts are the numbers; the diff is a cross-check ─────

def _cfg():
    cfg = MagicMock()
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.cloud.max_attempts = 2
    cfg.cloud.retry_delay_seconds = 60
    cfg.lan.max_attempts = 2
    cfg.lan.retry_delay_seconds = 600
    cfg.lan.shutdown_on_all_retries_exhausted = False
    return cfg


@patch("flow._record_running")
@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.cloud_publish_artifact_task")
@patch("flow.cloud_record_task")
@patch("flow.cloud_verify_and_report_task")
@patch("flow.cloud_sync_task")
@patch("flow.cloud_access_task")
@patch("flow.cloud_guards_task")
@patch("flow.cloud_preflight_task")
@patch("flow.health_check_task")
@patch("flow.ManifestDB")
@patch("flow._record_run")
def test_cloud_no_change_night_reports_zero_from_rclone_stats(
    rec, mdb, _h, _pre, _g, _acc, sync, verify, _rt, _pub, _art, _running,
):
    # DB baseline disagrees with the bucket (e.g. stale from before v2) ...
    mdb.from_config.return_value.get_cloud_synced_entries.return_value = {}
    sync.with_options.return_value.return_value = {
        "status": "CLOUD_NO_CHANGES_COMPLETE", "exit_code": 9,
        "stats": {"transfers": 0, "bytes": 0, "deletes": 0, "errors": 0}}
    verify.with_options.return_value.return_value = {
        "verified": True, "verify_termination": "normal",
        "size": {"count": 579, "bytes": 57_000_000},
        "manifest": [{"Path": f"f{i}.txt", "Size": 100, "ModTime": "2026-10-01T10:00:00Z"}
                     for i in range(579)],
        "diff": {"added": [], "removed": [], "modified": []}}
    flow._run_cloud_pipeline(_cfg(), "run-c", "2026-10-14T22:00:00")
    args, kwargs = rec.call_args
    assert args[4] == "CLOUD_NO_CHANGES_COMPLETE"
    assert kwargs["files_copied"] == 0 and kwargs["bytes_copied"] == 0
    em = json.loads(kwargs["extended_metrics"])
    assert em["counts_source"] == "rclone"
    assert em["diff_copied"] == 579 and em["metrics_consistent"] is False


@patch("flow._record_running")
@patch("flow._open_run_artefacts", return_value=None)
@patch("flow.cloud_publish_artifact_task")
@patch("flow.cloud_record_task")
@patch("flow.cloud_verify_and_report_task")
@patch("flow.cloud_sync_task")
@patch("flow.cloud_access_task")
@patch("flow.cloud_guards_task")
@patch("flow.cloud_preflight_task")
@patch("flow.health_check_task")
@patch("flow.ManifestDB")
@patch("flow._record_run")
def test_cloud_counts_unknown_without_stats(
    rec, mdb, _h, _pre, _g, _acc, sync, verify, _rt, _pub, _art, _running,
):
    mdb.from_config.return_value.get_cloud_synced_entries.return_value = {}
    sync.with_options.return_value.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0,
                                                   "stats": None}
    verify.with_options.return_value.return_value = {
        "verified": True, "verify_termination": "normal", "size": {"count": 1, "bytes": 1},
        "manifest": [], "diff": {"added": [], "removed": [], "modified": []}}
    flow._run_cloud_pipeline(_cfg(), "run-c2", "2026-10-14T22:00:00")
    kwargs = rec.call_args.kwargs
    assert kwargs["files_copied"] is None and kwargs["bytes_copied"] is None
    assert json.loads(kwargs["extended_metrics"])["counts_source"] == "unavailable"
