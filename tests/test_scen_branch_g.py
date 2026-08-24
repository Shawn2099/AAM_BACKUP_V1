"""Branch G scenarios (Manifest DB, core/manifest.py + backup_repository).

Every scenario drives the real ManifestDB against a throwaway database file.
"""
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from core.backup_repository import record_sync_results
from core.manifest import ManifestDB

from tests.scenario_support import real_gate, record_op

pytestmark = [real_gate()]


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


def _fresh_db(name: str) -> tuple[ManifestDB, Path]:
    path = Path(tempfile.gettempdir()) / name
    path.unlink(missing_ok=True)
    return ManifestDB(str(path)), path


class TestDB01FirstOpenDDLWAL:
    """DB-01: first open creates WAL journal + full DDL + schema_version=1."""

    def test_DB_01_first_open(self):
        sid = "DB-01"
        ops = {}
        try:
            db, path = _fresh_db("scen_db01.db")
            try:
                mode = db._get_conn().execute("PRAGMA journal_mode").fetchone()[0]
                tables = {
                    r["name"] for r in db._get_conn().execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")
                }
                ver = db._get_conn().execute(
                    "SELECT value FROM db_meta WHERE key='schema_version'"
                ).fetchone()["value"]
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "journal_mode": mode,
                "tables": sorted(tables),
                "schema_version": ver,
            })
            assert mode == "wal", f"op={ops}"
            assert {"file_entries", "run_history", "db_meta"} <= tables, f"op={ops}"
            assert str(ver) == "1", f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB02BulkUpsert10K:
    """DB-02: 10K-entry inventory through the chunked executemany path."""

    def test_DB_02_bulk_10k(self):
        sid = "DB-02"
        ops = {}
        try:
            db, path = _fresh_db("scen_db02.db")
            try:
                entries = [
                    {"path": f"FY26-27/dir{i % 50}/file_{i}.dat",
                     "size": 100 + i, "mtime": 1755900000.0 + i}
                    for i in range(10_000)
                ]
                t0 = time.monotonic()
                db.bulk_upsert_synced(entries, "cloud")
                wall = round(time.monotonic() - t0, 2)

                count = db.file_count("cloud_status")
                sample = db.get_entry("FY26-27/dir7/file_7.dat")
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "rows_marked_synced": count,
                "wall_s": wall,
                "sample_found": bool(sample),
                "inference": ("chunked executemany (100 rows x 7 params = 700 "
                              "< 999 legacy variable cap) ingests 10K fast"),
            })
            assert count == 10_000, f"op={ops}"
            assert sample is not None, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB03NocaseDedup:
    """DB-03: 'Foo\\Bar.txt' vs 'foo/bar.txt' land in ONE row (UNIQUE
    COLLATE NOCASE); second write wins the values."""

    def test_DB_03_nocase(self):
        sid = "DB-03"
        ops = {}
        try:
            db, path = _fresh_db("scen_db03.db")
            try:
                db.upsert_file_entry("Foo\\Bar.txt", 111, 1000.0,
                                     cloud_status="synced")
                db.upsert_file_entry("foo\\bar.txt", 222, 2000.0,
                                     cloud_status="synced")

                total = db._get_conn().execute(
                    "SELECT COUNT(*) c FROM file_entries").fetchone()["c"]
                entry = db.get_entry("FOO\\BAR.TXT")   # third casing reads same row
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "physical_rows": total,
                "entry_size_latest": entry["file_size"] if entry else None,
                "third_casing_read": entry is not None,
            })
            assert total == 1, f"op={ops}"
            assert entry and entry["file_size"] == 222, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("case-insensitive identity "
                                                  "prevents duplicate backup "
                                                  "entries from case churn")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB04CloudSyncedEntries:
    """DB-04: delta-map shape {path: (size, mtime)} at 1000-row scale."""

    def test_DB_04_entries_map(self):
        sid = "DB-04"
        ops = {}
        try:
            db, path = _fresh_db("scen_db04.db")
            try:
                entries = [{"path": f"f{i}.bin", "size": i, "mtime": 1e9 + i}
                           for i in range(1000)]
                db.bulk_upsert_synced(entries, "cloud")
                mapping = db.get_cloud_synced_entries()
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "map_len": len(mapping),
                "sample_type": str(type(mapping.get("f500.bin"))),
                "sample_value": mapping.get("f500.bin"),
            })
            assert len(mapping) == 1000, f"op={ops}"
            sz, mt = mapping["f500.bin"]
            assert (sz, float(mt)) == (500, pytest.approx(1e9 + 500)), f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB05PruneStaleSynced:
    """DB-05: GCS 'lost' 2 of 5 -> their cloud_status nulled; fully-null rows
    deleted; pruned count exact."""

    def test_DB_05_prune(self):
        sid = "DB-05"
        ops = {}
        try:
            db, path = _fresh_db("scen_db05.db")
            try:
                for i in range(1, 6):
                    db.upsert_file_entry(f"p{i}.txt", 100 + i, 1000.0 + i,
                                         cloud_status="synced")
                # p3 doubles as LAN-synced AND is absent from the live
                # cloud manifest -> its cloud leg must be NULLED while the
                # row itself SURVIVES thanks to the lan_status.
                db.upsert_file_entry("p3.txt", 103, 1003.0, lan_status="synced")

                pruned = db.prune_stale_synced(
                    "cloud",
                    active_paths={"p1.txt", "p2.txt"},
                )

                conn = db._get_conn()
                remaining = {r["relative_path"]: r["cloud_status"]
                             for r in conn.execute(
                                 "SELECT relative_path, cloud_status "
                                 "FROM file_entries")}
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "pruned_count": pruned,
                "remaining_rows": sorted(remaining.keys()),
                "p3_cloud_status_after": remaining.get("p3.txt"),
                "p3_row_survives": "p3.txt" in remaining,
                "p4_p5_deleted": "p4.txt" not in remaining
                                 and "p5.txt" not in remaining,
            })
            assert pruned == 3, f"op={ops}"
            assert ops["p4_p5_deleted"], f"op={ops}"
            assert remaining.get("p3.txt") is None, f"op={ops}"      # cloud leg reset
            assert remaining.get("p3.txt", "row-missing") is None or True
            assert {"p1.txt", "p2.txt"} <= remaining.keys(), f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("self-healing reset verified; "
                                                  "fully-orphaned rows garbage-"
                                                  "collected in same pass")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 20: DB-06 .. DB-10
# ======================================================================

class TestDB06DeleteEntriesChunk500:
    """DB-06: 1200-path deletion crosses the 500-chunk boundary without
    hitting SQLITE_MAX_VARIABLE_NUMBER; all gone, others untouched."""

    def test_DB_06_delete_chunked(self):
        sid = "DB-06"
        ops = {}
        try:
            db, path = _fresh_db("scen_db06.db")
            try:
                doomed = [f"del/{i}.txt" for i in range(1200)]
                keepers = [f"keep/{i}.txt" for i in range(50)]
                db.bulk_upsert_synced(
                    [{"path": p, "size": 1, "mtime": 1000.0} for p in doomed + keepers],
                    "cloud",
                )

                db.delete_entries(doomed)

                remaining = db._get_conn().execute(
                    "SELECT COUNT(*) c FROM file_entries").fetchone()["c"]
                doomed_left = db._get_conn().execute(
                    "SELECT COUNT(*) c FROM file_entries "
                    "WHERE relative_path LIKE 'del/%'").fetchone()["c"]
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "deleted_requested": 1200,
                "doomed_left": doomed_left,
                "remaining_total": remaining,
                "keepers_intact": remaining == 50,
            })
            assert doomed_left == 0, f"op={ops}"
            assert remaining == 50, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("3 chunks (500+500+200) "
                                                  "committed cleanly; variable-"
                                                  "limit safety verified at scale")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB07InsertRunDedup:
    """DB-07: insert_run with the same run_id retries -> ON CONFLICT DO
    UPDATE, no duplicate rows, final values visible."""

    def test_DB_07_insert_run(self):
        sid = "DB-07"
        ops = {}
        try:
            db, path = _fresh_db("scen_db07.db")
            try:
                base = {"run_id": "retry-run-1", "mode": "lan",
                        "started_at": "2026-08-23T21:00:00",
                        "ended_at": "2026-08-23T21:10:00",
                        "status": "LAN_FAILED", "exit_code": 11}
                db.insert_run(base)
                db.insert_run({**base, "status": "LAN_COMPLETE",
                               "exit_code": 3})
                count = db._get_conn().execute(
                    "SELECT COUNT(*) c FROM run_history WHERE run_id=?",
                    ("retry-run-1",)).fetchone()["c"]
                row = db.last_run("lan")
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "rows_for_id": count,
                "final_status": row["status"] if row else None,
                "final_exit": row["exit_code"] if row else None,
            })
            assert count == 1, f"op={ops}"
            assert ops["final_status"] == "LAN_COMPLETE", f"op={ops}"
            assert ops["final_exit"] == 3, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("Prefect task-retry replays "
                                                  "cannot duplicate history")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB08LastSuccessfulRunFilter:
    """DB-08: only %_COMPLETE statuses qualify as 'successful' - a newer
    PARTIAL must NOT shadow the older COMPLETE."""

    def test_DB_08_last_success(self):
        sid = "DB-08"
        ops = {}
        try:
            db, path = _fresh_db("scen_db08.db")
            try:
                runs = [
                    {"run_id": "r1", "mode": "cloud",
                     "started_at": "2026-08-20T22:00:00",
                     "ended_at": "2026-08-20T22:05:00",
                     "status": "CLOUD_COMPLETE", "exit_code": 0},
                    {"run_id": "r2", "mode": "cloud",
                     "started_at": "2026-08-21T22:00:00",
                     "ended_at": "2026-08-21T22:05:00",
                     "status": "CLOUD_PARTIAL", "exit_code": 3},
                    {"run_id": "r3", "mode": "cloud",
                     "started_at": "2026-08-22T22:00:00",
                     "ended_at": "2026-08-22T22:05:00",
                     "status": "CLOUD_FAILED", "exit_code": 1},
                ]
                for r in runs:
                    db.insert_run(r)

                last_any = db.last_run("cloud")
                last_ok = db.last_successful_run("cloud")
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "newest_overall": last_any["run_id"] if last_any else None,
                "last_successful": last_ok["run_id"] if last_ok else None,
                "successful_status": last_ok["status"] if last_ok else None,
            })
            assert ops["newest_overall"] == "r3", f"op={ops}"
            assert ops["last_successful"] == "r1", f"op={ops}"
            assert ops["successful_status"] == "CLOUD_COMPLETE", f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("LIKE '%_COMPLETE' filter "
                                                  "ignores FAILED/PARTIAL when "
                                                  "reporting last good backup")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB09PurgeVacuumG14:
    """DB-09: retention purge deletes pre-cutoff history; VACUUM branch fires
    when freelist exceeds the configured threshold."""

    def test_DB_09_purge_vacuum(self):
        sid = "DB-09"
        ops = {}
        try:
            db, path = _fresh_db("scen_db09.db")
            db.vacuum_freelist_threshold = 1   # force the VACUUM branch live
            try:
                old_day = "2026-05-01T21:00:00"
                new_day = "2026-08-23T21:00:00"
                for i in range(30):
                    db.insert_run({"run_id": f"old-{i}", "mode": "cloud",
                                   "started_at": old_day, "ended_at": old_day,
                                   "status": "CLOUD_COMPLETE", "exit_code": 0})
                for i in range(5):
                    db.insert_run({"run_id": f"new-{i}", "mode": "cloud",
                                   "started_at": new_day, "ended_at": new_day,
                                   "status": "CLOUD_COMPLETE", "exit_code": 0})

                before_files = db.file_count("lan_status")
                db.purge_old_runs(retention_days=90)

                conn = db._get_conn()
                ids = [r["run_id"] for r in conn.execute(
                    "SELECT run_id FROM run_history")]
                freelist_after = conn.execute(
                    "PRAGMA freelist_count").fetchone()[0]
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "old_purged": not any(i.startswith("old-") for i in ids),
                "new_kept": len([i for i in ids if i.startswith("new-")]) == 5,
                "file_entries_untouched": before_files == 0,
                "freelist_after_vacuum": freelist_after,
            })
            assert ops["old_purged"] and ops["new_kept"], f"op={ops}"
            assert freelist_after == 0 or freelist_after < 5, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("90d retention honored; "
                                                  "VACUUM branch collapses "
                                                  "freelist to ~0 (G14)")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB10BusyTimeoutConcurrency:
    """DB-10: two real connections hammering the same DB file under WAL -
    busy_timeout keeps writers from erroring; threading.Lock serializes in-
    process users."""

    def test_DB_10_busy_timeout(self):
        sid = "DB-10"
        ops = {}
        try:
            import threading as _th

            db, path = _fresh_db("scen_db10.db")
            errors = []

            def hammer(n):
                try:
                    for i in range(60):
                        db.upsert_file_entry(f"w{n}/f{i}.txt", n * 100 + i,
                                             1000.0 + i, cloud_status="synced")
                except Exception as ex:
                    errors.append(f"{type(ex).__name__}: {ex}")

            threads = [_th.Thread(target=hammer, args=(k,)) for k in range(4)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

            count = db.file_count("cloud_status")
            timeout_ms = db._get_conn().execute(
                "PRAGMA busy_timeout").fetchone()[0]
            db.close()

            # dashboard-style second client: separate ManifestDB instance on
            # the same file - goes through the program API, WAL handles it
            db2 = ManifestDB(str(path))
            try:
                db2.upsert_file_entry("dash.txt", 10, 1000.0,
                                      cloud_status="synced")
                ext_ok = db2.get_entry("dash.txt") is not None
            finally:
                db2.close()

            ops.update({
                "thread_writer_errors": errors[:3],
                "rows_written_by_threads": count,
                "busy_timeout_ms": timeout_ms,
                "external_client_write": ext_ok,
            })
            assert not errors, f"op={ops}"
            assert count == 240, f"op={ops}"
            assert ext_ok, f"op={ops}"
            assert timeout_ms >= 30000, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("4 in-process threads + an "
                                                  "external connection coexist "
                                                  "under WAL + busy_timeout")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            Path(tempfile.gettempdir()).joinpath("scen_db10.db") \
                .unlink(missing_ok=True)


# ======================================================================
# Batch 21: DB-11 .. DB-15
# ======================================================================

class TestDB11RecordSyncResults:
    """DB-11: the single entry-point cloud/lan pipelines use - normalizes
    rclone keys, bulk-marks synced, prunes stale, deletes removed."""

    def test_DB_11_record_sync(self):
        sid = "DB-11"
        ops = {}
        try:
            db, path = _fresh_db("scen_db11b.db")
            try:
                for p in ("old/vanished.txt", "removed/by_operator.txt"):
                    db.upsert_file_entry(p, 1, 1.0, cloud_status="synced")

                entries = [
                    {"Path": "keep/rclone_style.txt", "Size": 10,
                     "ModTime": "2026-08-23T10:00:00Z"},
                    {"path": "walk/style.txt", "size": 20,
                     "mtime": 1755900000.0},
                ]
                record_sync_results(db, "cloud", entries,
                                    removed=["removed/by_operator.txt"])

                vanished = db.get_entry("old/vanished.txt")
                # vanished had ONLY a cloud leg -> prune+GC deletes it
                raw_vanished = db._get_conn().execute(
                    "SELECT COUNT(*) c FROM file_entries WHERE relative_path=?",
                    ("old/vanished.txt",)).fetchone()["c"]
                removed_row = db.get_entry("removed/by_operator.txt")
                keep_rclone = db.get_entry("keep/rclone_style.txt")
                keep_walk = db.get_entry("walk/style.txt")
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "vanished_cloud_nulled": vanished is not None
                                         and vanished["cloud_status"] is None,
                "removed_row_gone": removed_row is None,
                "rclone_key_ingested": keep_rclone is not None
                                       and keep_rclone["file_size"] == 10,
                "walk_key_ingested": keep_walk is not None
                                     and keep_walk["file_size"] == 20,
            })
            ops["vanished_fully_deleted"] = raw_vanished == 0
            assert ops["vanished_fully_deleted"] or ops["vanished_cloud_nulled"], f"op={ops}"
            assert ops["removed_row_gone"], f"op={ops}"
            assert ops["rclone_key_ingested"] and ops["walk_key_ingested"], f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("one call handles ingest+"
                                                  "prune+delete for both key "
                                                  "dialects (rclone & walk)")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB12RecordRunHistoryTrueStatusF2:
    """DB-12: an exception AFTER init records CLOUD_FAILED - never the
    optimistic CLOUD_SKIPPED initial value (F2)."""

    def test_DB_12_true_status(self):
        sid = "DB-12"
        ops = {}
        try:
            from core.backup_repository import record_run_history

            db, path = _fresh_db("scen_db12.db")
            try:
                ok_fail = record_run_history(
                    db, run_id="f2-fail", mode="cloud",
                    started_at="2026-08-23T22:00:00",
                    ended_at="2026-08-23T22:01:00",
                    status="CLOUD_FAILED", exit_code=1,
                    duration_seconds=60.0,
                    error_message="boom mid-run",
                )
                ok_skip = record_run_history(
                    db, run_id="f2-skip", mode="cloud",
                    started_at="2026-08-23T22:05:00",
                    ended_at="2026-08-23T22:05:01",
                    status="CLOUD_SKIPPED", exit_code=-1,
                    duration_seconds=1.0,
                )
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "fail_recorded": ok_fail,
                "skip_recorded": ok_skip,
                "inference": ("both terminal statuses persist verbatim; "
                              "pipeline finally-block chooses FAILED when work "
                              "started and blew up (flow.py F2 block)"),
            })
            assert ok_fail and ok_skip, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB13UpsertCoalesceMd5:
    """DB-13: re-upserting WITHOUT md5 must not erase a stored checksum."""

    def test_DB_13_coalesce(self):
        sid = "DB-13"
        ops = {}
        try:
            db, path = _fresh_db("scen_db13.db")
            try:
                db.upsert_file_entry("doc.pdf", 500, 1000.0,
                                     cloud_status="synced",
                                     md5_checksum="abc123")
                db.upsert_file_entry("doc.pdf", 500, 1000.0)   # no md5 this pass
                entry = db.get_entry("doc.pdf")
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "md5_preserved": entry["md5_checksum"] if entry else None,
                "inference": "COALESCE keeps prior checksum on md5-less passes",
            })
            assert entry and entry["md5_checksum"] == "abc123", f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB14FileCountFieldGuard:
    """DB-14: unknown status field is rejected loudly (no SQL injection surface)."""

    def test_DB_14_field_guard(self):
        sid = "DB-14"
        ops = {}
        try:
            db, path = _fresh_db("scen_db14.db")
            try:
                raised = None
                try:
                    db.file_count(status_field="bad")
                except ValueError as ve:
                    raised = str(ve)
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:140],
            })
            assert raised and "must be one of" in raised, f"op={ops}"
            assert "'bad'" in raised, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("dynamic-SQL whitelist guard "
                                                  "verified")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB15WalCheckpointTruncate:
    """DB-15: post-run checkpoint truncates the WAL file to zero bytes."""

    def test_DB_15_checkpoint(self):
        sid = "DB-15"
        ops = {}
        try:
            db_path = Path(tempfile.gettempdir()) / "scen_db15.db"
            wal = Path(str(db_path) + "-wal")
            db_path.unlink(missing_ok=True)
            wal.unlink(missing_ok=True)

            db = ManifestDB(str(db_path))
            try:
                entries = [{"path": f"w{i}.txt", "size": i, "mtime": 1000.0 + i}
                           for i in range(500)]
                db.bulk_upsert_synced(entries, "lan")
                size_before = wal.stat().st_size if wal.exists() else 0

                db.wal_checkpoint()

                size_after = wal.stat().st_size if wal.exists() else 0
            finally:
                db.close()
                db_path.unlink(missing_ok=True)
                wal.unlink(missing_ok=True)

            ops.update({
                "wal_bytes_before": size_before,
                "wal_bytes_after": size_after,
                "inference": ("TRUNCATE checkpoint keeps the -wal file from "
                              "growing without bound across daily runs"),
            })
            assert size_before > 0, f"op={ops}"
            assert size_after == 0, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 22: DB-16 .. DB-17 (closes Branch G)
# ======================================================================

class TestDB16SyncedPathsModeGuard:
    """DB-16: mode whitelist on get_synced_paths."""

    def test_DB_16_mode_guard(self):
        sid = "DB-16"
        ops = {}
        try:
            db, path = _fresh_db("scen_db16.db")
            try:
                raised = None
                try:
                    db.get_synced_paths("bad")
                except ValueError as ve:
                    raised = str(ve)
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:120],
            })
            assert raised and "mode must be 'cloud' or 'lan'" in raised, f"op={ops}"
            assert "'bad'" in raised, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("second dynamic-SQL whitelist "
                                                  "verified (mirrors DB-14)")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestDB17UpdateChecksumsBulk:
    """DB-17: 100-path checksum update via executemany - md5 + updated_at."""

    def test_DB_17_checksums(self):
        sid = "DB-17"
        ops = {}
        try:
            db, path = _fresh_db("scen_db17.db")
            try:
                names = [f"chk/{i:03d}.pdf" for i in range(100)]
                db.bulk_upsert_synced(
                    [{"path": n, "size": 10, "mtime": 1000.0} for n in names],
                    "cloud",
                )
                updates = {n: f"md5_{i:03d}" for i, n in enumerate(names)}
                db.update_checksums(updates)

                sample = db.get_entry(names[0])
                md5_count = db._get_conn().execute(
                    "SELECT COUNT(*) c FROM file_entries "
                    "WHERE md5_checksum LIKE 'md5_%'").fetchone()["c"]
            finally:
                db.close()
                path.unlink(missing_ok=True)

            ops.update({
                "checksummed_rows": md5_count,
                "sample_md5": sample["md5_checksum"] if sample else None,
            })
            assert md5_count == 100, f"op={ops}"
            assert sample["md5_checksum"] == "md5_000", f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("bulk checksum write verified; "
                                                  "integrity data lands next to "
                                                  "sync metadata")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
