"""ManifestDB — SQLite database for file catalog and run history.

WAL mode on every connection. Thread-safe. Single writer (deployments run at
different times, no contention).
"""

import sqlite3
import threading
from pathlib import Path

from loguru import logger

from core.time_utils import cutoff_iso, utcnow_iso

SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=30000;

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
CREATE UNIQUE INDEX IF NOT EXISTS idx_run_history_run_id ON run_history(run_id);

CREATE TABLE IF NOT EXISTS db_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

INSERT OR IGNORE INTO db_meta (key, value) VALUES ('schema_version', '1');
"""


class ManifestDB:
    """SQLite manifest with WAL mode, thread-safe writes."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
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
                logger.debug(f"Pre-migration dedup skipped: {e}")

            conn.executescript(DDL)
            
            # Safe schema migration for extended_metrics
            try:
                columns = [row['name'] for row in conn.execute("PRAGMA table_info(run_history)").fetchall()]
                if 'extended_metrics' not in columns:
                    conn.execute("ALTER TABLE run_history ADD COLUMN extended_metrics TEXT")
                    conn.commit()
            except Exception as e:
                logger.error(f"Migration failed: {e}")
                
            self._conn = conn
        return self._conn

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
            now = utcnow_iso()
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
            now = utcnow_iso()
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

    def mark_lan_synced(self, paths: list[str]):
        """Bulk update: set lan_status='synced' on all given paths."""
        if not paths:
            return
        normalized = [p.replace("\\", "/") for p in paths]
        with self._lock:
            conn = self._get_conn()
            now = utcnow_iso()
            conn.executemany(
                """UPDATE file_entries
                   SET lan_status = 'synced',
                       lan_last_synced_at = ?,
                       updated_at = ?
                   WHERE relative_path = ?""",
                [(now, now, p) for p in normalized],
            )
            conn.commit()

    def mark_cloud_synced(self, paths: list[str]):
        """Bulk update: set cloud_status='synced' on all given paths."""
        if not paths:
            return
        normalized = [p.replace("\\", "/") for p in paths]
        with self._lock:
            conn = self._get_conn()
            now = utcnow_iso()
            conn.executemany(
                """UPDATE file_entries
                   SET cloud_status = 'synced',
                       cloud_last_synced_at = ?,
                       updated_at = ?
                   WHERE relative_path = ?""",
                [(now, now, p) for p in normalized],
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

    def update_checksums(self, updates: dict[str, str]):
        """Bulk update md5_checksum for multiple files."""
        if not updates:
            return
        with self._lock:
            conn = self._get_conn()
            now = utcnow_iso()
            conn.executemany(
                """UPDATE file_entries SET md5_checksum = ?, updated_at = ?
                   WHERE relative_path = ?""",
                [(md5, now, path.replace("\\", "/")) for path, md5 in updates.items()],
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
        db_paths = self.get_synced_paths(mode)  # acquires + releases lock
        stale_paths = [p for p in db_paths if p not in active_paths]
        if not stale_paths:
            return 0
        with self._lock:
            conn = self._get_conn()
            conn.executemany(
                f"UPDATE file_entries SET {status_field} = NULL, "
                f"{ts_field} = NULL WHERE relative_path = ?",
                [(path,) for path in stale_paths],
            )
            conn.execute(
                "DELETE FROM file_entries "
                "WHERE lan_status IS NULL AND cloud_status IS NULL"
            )
            conn.commit()
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

    def get_recent_runs(self, limit: int = 10) -> list[dict]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM run_history ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

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

        Conditionally VACUUMs when the freelist exceeds 1000 pages (~4 MB),
        per SQLite best practices: VACUUM is expensive and should only run
        when the space savings justify the cost.
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
            if freelist and freelist[0] > 1000:
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                conn.commit()
                conn.execute("VACUUM")
                logger.debug(
                    f"VACUUM triggered — freelist={freelist[0]} pages "
                    f"(~{freelist[0] * page_size // 1024} KB)"
                )
            else:
                conn.commit()
