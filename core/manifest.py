"""ManifestDB — SQLite database for file catalog and run history.

WAL mode on every connection. Thread-safe. Single writer (deployments run at
different times, no contention).
"""

import sqlite3
import threading
import time
from pathlib import Path

from loguru import logger

from core.time_utils import cutoff_iso, now_iso

# Critical-6: version gate for schema migrations. Bump when DDL or the
# legacy-migration steps below change. Fresh databases are stamped straight
# to this value; pre-versioning databases run _migrate_legacy_schema once.
SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS file_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    file_size       INTEGER NOT NULL DEFAULT 0,
    mtime           REAL NOT NULL DEFAULT 0,
    md5_checksum    TEXT DEFAULT 'pending',
    lan_status      TEXT DEFAULT 'unknown',
    cloud_status    TEXT DEFAULT 'unknown',
    lan_last_synced_at      TEXT,
    cloud_last_synced_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_file_entries_lan_status ON file_entries(lan_status);
CREATE INDEX IF NOT EXISTS idx_file_entries_cloud_status ON file_entries(cloud_status);

CREATE TABLE IF NOT EXISTS run_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL UNIQUE,
    mode            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL,
    exit_code       INTEGER,
    files_copied    INTEGER DEFAULT 0,
    bytes_copied    INTEGER DEFAULT 0,
    files_failed    INTEGER DEFAULT 0,
    duration_seconds REAL,
    error_message   TEXT,
    extended_metrics TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_history_started_at ON run_history(started_at);
CREATE INDEX IF NOT EXISTS idx_run_history_mode ON run_history(mode);

CREATE TABLE IF NOT EXISTS db_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

-- Independent integrity-audit record (weekly audit; read-only checks).
-- Kept SEPARATE from run_history on purpose: a backup result
-- (COMPLETE/PARTIAL/FAILED) and its integrity-verification status
-- (NOT_VERIFIED/VERIFIED/VERIFICATION_FAILED) are different facts.
-- An audit never rewrites a historical backup row; absence of a row
-- for a mode/scope means NOT_VERIFIED.
CREATE TABLE IF NOT EXISTS integrity_audits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id        TEXT NOT NULL UNIQUE,
    mode            TEXT NOT NULL,          -- 'lan' | 'cloud'
    scope           TEXT NOT NULL DEFAULT 'full',
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL,          -- 'VERIFIED' | 'VERIFICATION_FAILED'
    files_checked   INTEGER DEFAULT 0,
    bytes_checked   INTEGER DEFAULT 0,
    mismatches      INTEGER DEFAULT 0,
    detail          TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_integrity_audits_mode ON integrity_audits(mode, started_at);

INSERT OR IGNORE INTO db_meta (key, value) VALUES ('schema_version', '1');
"""


class ManifestSchemaError(RuntimeError):
    """Critical-6: a schema migration could not be applied.

    Raised instead of silently continuing with a schema that insert_run
    cannot write to (which previously lost every subsequent run_history
    record with no visible error). Startup fails loudly; NSSM restarts the
    service and the operator sees the cause in the log.
    """


class ManifestDB:
    """SQLite manifest with WAL mode, thread-safe writes."""

    def __init__(
        self,
        db_path: str | Path,
        busy_timeout_ms: int = 30000,
        vacuum_freelist_threshold: int = 1000,
        synchronous: str = "normal",
    ):
        """Create or open the manifest database.

        Args:
            db_path: Path to the SQLite file.
            busy_timeout_ms: PRAGMA busy_timeout value in milliseconds.
                             Override via config.maintenance.sqlite_busy_timeout_ms.
            vacuum_freelist_threshold: Trigger VACUUM when freelist page count exceeds
                                       this value. Override via config.maintenance.sqlite_vacuum_freelist_threshold.
            synchronous: R4 - PRAGMA synchronous level, "normal" (default; no
                         corruption risk, possible last-commit loss on power
                         cut) or "full" (per-commit WAL fsync, maximum
                         durability). Override via
                         config.maintenance.sqlite_synchronous.
        """
        if synchronous not in ("normal", "full"):
            raise ValueError(f"invalid synchronous level: {synchronous!r}")
        self.db_path = str(db_path)
        self.busy_timeout_ms = busy_timeout_ms
        self.vacuum_freelist_threshold = vacuum_freelist_threshold
        self.synchronous = synchronous
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            
            # Clean up legacy duplicate run_id values before applying the UNIQUE index in DDL.
            # Guarded by table existence check — avoids error on fresh databases.
            try:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
                if "run_history" in tables:
                    conn.execute("""
                        DELETE FROM run_history
                        WHERE id NOT IN (
                            SELECT MIN(id)
                            FROM run_history
                            GROUP BY run_id
                        )
                    """)
                    conn.commit()
            except Exception as e:
                logger.warning(f"Pre-migration dedup skipped: {e}")

            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.executescript(DDL)
            # R4: DDL pins NORMAL as the safe default; an operator-chosen
            # "full" is applied on top so the toggle takes effect per open.
            if self.synchronous == "full":
                conn.execute("PRAGMA synchronous=FULL")

            # Critical-6: version-gated schema migration. Fresh databases are
            # stamped straight to SCHEMA_VERSION (DDL already creates every
            # column, so no ALTER is needed); legacy databases — user_version
            # 0 — run the migration steps exactly once.
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                try:
                    self._migrate_legacy_schema(conn)
                except BaseException:
                    # Never cache a connection whose schema could not be
                    # brought current — the next open retries from scratch.
                    conn.close()
                    raise

            self._conn = conn
        return self._conn

    def _migrate_legacy_schema(self, conn: sqlite3.Connection) -> None:
        """Bring a pre-versioning database up to SCHEMA_VERSION.

        Critical-6: a failed migration must be LOUD. The old code swallowed
        the ALTER TABLE failure with ``logger.error`` and returned a usable
        connection — after which every insert_run failed (missing column)
        and record_run_history silently dropped all run history.

        Retries transient SQLITE_BUSY up to 3 times; permanent failure
        raises ManifestSchemaError (caller closes the connection).

        Note: sqlite3's implicit transactions cover DML only — DDL would
        otherwise autocommit and rollback() could not undo it. The ALTER +
        version stamp therefore run inside an explicit BEGIN IMMEDIATE so
        they commit atomically or not at all.
        """
        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(1, 4):
            try:
                columns = {
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA table_info(run_history)"
                    ).fetchall()
                }
                if "extended_metrics" not in columns:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "ALTER TABLE run_history ADD COLUMN extended_metrics TEXT"
                    )
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    conn.commit()
                else:
                    # Column present but never stamped (legacy database).
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    conn.commit()
                return
            except sqlite3.OperationalError as exc:
                conn.rollback()
                last_exc = exc
                if "locked" not in str(exc).lower() or attempt == 3:
                    break
                logger.warning(
                    f"Schema migration attempt {attempt}/3 hit a locked "
                    "database - retrying"
                )
                time.sleep(1.0 * attempt)
        raise ManifestSchemaError(
            f"run_history schema migration failed: {last_exc}"
        ) from last_exc

    def close(self):
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ── File Entries ─────────────────────────────────────────

    def upsert_file_entry(
        self,
        relative_path: str,
        file_size: int,
        mtime: float,
        *,
        lan_status: str | None = None,
        cloud_status: str | None = None,
        md5_checksum: str | None = None,
    ):
        relative_path = relative_path.replace("\\", "/")
        with self._lock:
            conn = self._get_conn()
            now = now_iso()
            conn.execute(
                """INSERT INTO file_entries
                   (relative_path, file_size, mtime, md5_checksum,
                    lan_status, cloud_status, lan_last_synced_at, cloud_last_synced_at,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?,
                           ?, ?, ?, ?,
                           ?, ?)
                   ON CONFLICT(relative_path) DO UPDATE SET
                       file_size = excluded.file_size,
                       mtime = excluded.mtime,
                       md5_checksum = COALESCE(excluded.md5_checksum, file_entries.md5_checksum),
                       lan_status = COALESCE(excluded.lan_status, file_entries.lan_status),
                       cloud_status = COALESCE(excluded.cloud_status, file_entries.cloud_status),
                       lan_last_synced_at = CASE
                           WHEN excluded.lan_status = 'synced' AND file_entries.lan_status != 'synced'
                           THEN excluded.lan_last_synced_at
                           ELSE file_entries.lan_last_synced_at
                       END,
                       cloud_last_synced_at = CASE
                           WHEN excluded.cloud_status = 'synced' AND file_entries.cloud_status != 'synced'
                           THEN excluded.cloud_last_synced_at
                           ELSE file_entries.cloud_last_synced_at
                       END,
                       updated_at = excluded.updated_at""",
                (
                    relative_path,
                    file_size,
                    mtime,
                    md5_checksum,
                    lan_status,
                    cloud_status,
                    now if lan_status else None,
                    now if cloud_status else None,
                    now,
                    now,
                ),
            )
            conn.commit()

    def bulk_upsert_synced(
        self,
        entries: list[dict],
        mode: str,
    ) -> None:
        """Bulk upsert file entries and mark as synced in one transaction.

        Replaces per-file upsert_file_entry() + mark_*_synced() with a single
        executemany() call. 10-100x faster for large inventories (10K+ files).

        Args:
            entries: List of dicts, each with 'path', 'size', 'mtime'.
                     Optional: 'md5_checksum'.
            mode: 'cloud' or 'lan' — determines which status/timestamp to set.
        """
        if not entries:
            return

        if mode not in ("cloud", "lan"):
            raise ValueError(f"mode must be 'cloud' or 'lan', got {mode!r}")

        status_field = f"{mode}_status"
        ts_field = f"{mode}_last_synced_at"

        with self._lock:
            conn = self._get_conn()
            now = now_iso()
            # Chunk at 100 rows (700 params) to stay under SQLite's variable limit
            # on older builds (SQLITE_MAX_VARIABLE_NUMBER=999).
            for i in range(0, len(entries), 100):
                chunk = entries[i : i + 100]
                conn.executemany(
                    f"""INSERT INTO file_entries
                        (relative_path, file_size, mtime, md5_checksum,
                         {status_field}, {ts_field},
                         created_at, updated_at)
                        VALUES (?, ?, ?, ?,
                                'synced', ?,
                                ?, ?)
                        ON CONFLICT(relative_path) DO UPDATE SET
                            file_size = excluded.file_size,
                            mtime = excluded.mtime,
                            md5_checksum = COALESCE(excluded.md5_checksum, file_entries.md5_checksum),
                            {status_field} = 'synced',
                            {ts_field} = CASE
                                WHEN file_entries.{status_field} != 'synced'
                                THEN excluded.{ts_field}
                                ELSE file_entries.{ts_field}
                            END,
                            updated_at = excluded.updated_at""",
                    [
                        (
                            e["path"].replace("\\", "/"),
                            e.get("size", 0),
                            e.get("mtime", 0),
                            e.get("md5_checksum"),
                            now,
                            now,
                            now,
                        )
                        for e in chunk
                    ],
                )
            conn.commit()

    def delete_entries(self, paths: list[str]):
        """Delete entries for files no longer on destination. Chunk to avoid SQLite variable limit."""
        if not paths:
            return
        normalized = [p.replace("\\", "/") for p in paths]
        with self._lock:
            conn = self._get_conn()
            for i in range(0, len(normalized), 500):
                chunk = normalized[i : i + 500]
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(
                    f"DELETE FROM file_entries WHERE relative_path IN ({placeholders})",
                    chunk,
                )
            conn.commit()

    def get_entry(self, relative_path: str) -> dict | None:
        relative_path = relative_path.replace("\\", "/")
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM file_entries WHERE relative_path = ?", (relative_path,)
            ).fetchone()
            return dict(row) if row else None

    def file_count(self, status_field: str = "lan_status") -> int:
        _ALLOWED = {"lan_status", "cloud_status"}
        if status_field not in _ALLOWED:
            raise ValueError(f"status_field must be one of {_ALLOWED}, got {status_field!r}")
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM file_entries WHERE {status_field} = 'synced'"
            ).fetchone()
            return row["cnt"] if row else 0

    def get_cloud_synced_entries(self) -> dict[str, tuple[int, float]]:
        """Return all cloud-synced entries as {relative_path: (file_size, mtime)}.

        Used by _run_cloud_pipeline to compute differential transfer metrics
        without accessing private DB internals.
        """
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT relative_path, file_size, mtime "
                "FROM file_entries WHERE cloud_status = 'synced'"
            ).fetchall()
        return {r["relative_path"]: (r["file_size"], r["mtime"]) for r in rows}

    def get_synced_paths(self, mode: str) -> list[str]:
        """Return all relative_paths where the given mode status is 'synced'.

        Used by backup_repository for self-healing stale-entry detection.
        """
        if mode not in ("cloud", "lan"):
            raise ValueError(f"mode must be 'cloud' or 'lan', got {mode!r}")
        status_field = f"{mode}_status"
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                f"SELECT relative_path FROM file_entries WHERE {status_field} = 'synced'"
            ).fetchall()
        return [r["relative_path"] for r in rows]

    def prune_stale_synced(self, mode: str, active_paths: set[str]) -> int:
        """Null-out sync status for entries no longer present on destination.

        Self-healing: entries marked 'synced' but absent from the live manifest
        are reset so they are re-evaluated on the next run. Entries with both
        lan_status and cloud_status NULL are fully deleted.

        Args:
            mode: 'cloud' or 'lan'.
            active_paths: Set of relative paths currently on destination.

        Returns:
            Number of stale entries pruned.
        """
        if mode not in ("cloud", "lan"):
            raise ValueError(f"mode must be 'cloud' or 'lan', got {mode!r}")
        status_field = f"{mode}_status"
        ts_field = f"{mode}_last_synced_at"
        with self._lock:
            conn = self._get_conn()
            with conn:
                rows = conn.execute(
                    f"SELECT relative_path FROM file_entries WHERE {status_field} = 'synced'"
                ).fetchall()
                db_paths = {r["relative_path"] for r in rows}
                stale_paths = [p for p in db_paths if p not in active_paths]
                if not stale_paths:
                    return 0
                conn.executemany(
                    f"UPDATE file_entries SET {status_field} = NULL, "
                    f"{ts_field} = NULL WHERE relative_path = ?",
                    [(path,) for path in stale_paths],
                )
                conn.execute(
                    "DELETE FROM file_entries "
                    "WHERE lan_status IS NULL AND cloud_status IS NULL"
                )
        return len(stale_paths)

    # ── Run History ──────────────────────────────────────────

    def insert_run(self, data: dict):
        required = ("run_id", "mode", "started_at", "status")
        missing = [k for k in required if k not in data]
        if missing:
            raise KeyError(f"insert_run missing required keys: {missing}")
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO run_history
                   (run_id, mode, started_at, ended_at, status, exit_code,
                    files_copied, bytes_copied, files_failed, duration_seconds, error_message, extended_metrics)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       ended_at = excluded.ended_at,
                       status = excluded.status,
                       exit_code = excluded.exit_code,
                       files_copied = excluded.files_copied,
                       bytes_copied = excluded.bytes_copied,
                       files_failed = excluded.files_failed,
                       duration_seconds = excluded.duration_seconds,
                       error_message = excluded.error_message,
                       extended_metrics = COALESCE(excluded.extended_metrics, run_history.extended_metrics)""",
                (
                    data["run_id"],
                    data["mode"],
                    data["started_at"],
                    data.get("ended_at"),
                    data["status"],
                    data.get("exit_code"),
                    data.get("files_copied", 0),
                    data.get("bytes_copied", 0),
                    data.get("files_failed", 0),
                    data.get("duration_seconds"),
                    data.get("error_message"),
                    data.get("extended_metrics"),
                ),
            )
            conn.commit()

    def get_runs_since(self, days: int, mode: str | None = None) -> list[dict]:
        cutoff = cutoff_iso(days)
        with self._lock:
            conn = self._get_conn()
            if mode:
                rows = conn.execute(
                    """SELECT * FROM run_history
                       WHERE started_at >= ?
                       AND mode = ?
                       ORDER BY started_at DESC""",
                    (cutoff, mode),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM run_history
                       WHERE started_at >= ?
                       ORDER BY started_at DESC""",
                    (cutoff,),
                ).fetchall()
            return [dict(r) for r in rows]

    def last_run(self, mode: str | None = None) -> dict | None:
        with self._lock:
            conn = self._get_conn()
            if mode:
                row = conn.execute(
                    "SELECT * FROM run_history WHERE mode = ? ORDER BY started_at DESC LIMIT 1",
                    (mode,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM run_history ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            return dict(row) if row else None

    def last_successful_run(self, mode: str) -> dict | None:
        """Return the last successful run for this mode (status ends with _COMPLETE)."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM run_history WHERE mode = ? AND status LIKE '%_COMPLETE' ORDER BY started_at DESC LIMIT 1",
                (mode,),
            ).fetchone()
            return dict(row) if row else None

    def get_recent_runs(self, limit: int = 10) -> list[dict]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM run_history ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Integrity audits ─────────────────────────────────────

    def record_audit(self, data: dict):
        """Persist one integrity-audit execution (upsert on audit_id)."""
        required = ("audit_id", "mode", "started_at", "status")
        missing = [k for k in required if k not in data]
        if missing:
            raise KeyError(f"record_audit missing required keys: {missing}")
        if data["status"] not in ("VERIFIED", "VERIFICATION_FAILED"):
            raise ValueError(f"invalid audit status: {data['status']!r}")
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO integrity_audits
                   (audit_id, mode, scope, started_at, ended_at, status,
                    files_checked, bytes_checked, mismatches, detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(audit_id) DO UPDATE SET
                       ended_at = excluded.ended_at,
                       status = excluded.status,
                       files_checked = excluded.files_checked,
                       bytes_checked = excluded.bytes_checked,
                       mismatches = excluded.mismatches,
                       detail = excluded.detail""",
                (
                    data["audit_id"],
                    data["mode"],
                    data.get("scope", "full"),
                    data["started_at"],
                    data.get("ended_at"),
                    data["status"],
                    data.get("files_checked", 0),
                    data.get("bytes_checked", 0),
                    data.get("mismatches", 0),
                    data.get("detail"),
                    data.get("created_at", data["started_at"]),
                ),
            )
            conn.commit()

    def latest_audit(self, mode: str, scope: str | None = None) -> dict | None:
        """Return the newest audit row for a mode (and optional scope).

        Returns None when no audit has ever run — callers render that as
        NOT_VERIFIED. A VERIFICATION_FAILED row stays authoritative until a
        later audit passes; audits never touch run_history.
        """
        with self._lock:
            conn = self._get_conn()
            if scope is not None:
                row = conn.execute(
                    """SELECT * FROM integrity_audits
                       WHERE mode = ? AND scope = ?
                       ORDER BY started_at DESC LIMIT 1""",
                    (mode, scope),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM integrity_audits
                       WHERE mode = ?
                       ORDER BY started_at DESC LIMIT 1""",
                    (mode,),
                ).fetchone()
            return dict(row) if row else None

    # ── Maintenance ──────────────────────────────────────────

    def wal_checkpoint(self):
        """Truncate WAL file after backup run to prevent bloat."""
        with self._lock:
            conn = self._get_conn()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def purge_old_runs(self, retention_days: int = 90):
        """Delete run_history entries older than retention_days.

        Keeps file_entries intact — only purges the run log to prevent
        unbounded DB growth over years of daily runs.

        Conditionally VACUUMs when the freelist exceeds self.vacuum_freelist_threshold
        pages (~4 MB at default of 1000), per SQLite best practices.
        Override via config.maintenance.sqlite_vacuum_freelist_threshold.
        """
        with self._lock:
            conn = self._get_conn()
            cutoff = cutoff_iso(retention_days)
            conn.execute(
                "DELETE FROM run_history WHERE started_at < ?",
                (cutoff,),
            )
            conn.execute("PRAGMA optimize")
            conn.execute("ANALYZE")
            freelist = conn.execute("PRAGMA freelist_count").fetchone()
            if freelist and freelist[0] > self.vacuum_freelist_threshold:
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                conn.commit()
                conn.execute("VACUUM")
                logger.debug(
                    f"VACUUM triggered - freelist={freelist[0]} pages "
                    f"(~{freelist[0] * page_size // 1024} KB)"
                )
            else:
                conn.commit()
