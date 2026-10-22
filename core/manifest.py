"""ManifestDB — SQLite database for file catalog and run history.

WAL mode on every connection. Thread-safe. Single writer (deployments run at
different times, no contention).
"""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from loguru import logger

from core import rollover_journal as rj
from core import statuses
from core.statuses import AuditStatus
from core.time_utils import IST, cutoff_iso, now_iso
from models.config import MaintenanceConfig, model_default

# Critical-6: version gate for schema migrations. Bump when DDL or the
# legacy-migration steps below change. Fresh databases are stamped straight
# to this value; pre-versioning databases run _migrate_legacy_schema once.
SCHEMA_VERSION = 5   # v3: notifications.fault_code / message_id / thread_id; v5: audience / recipients
                     # v4 (plan SCH): rollover_journal(+_steps), notifications.attachments_rung

# Per-connection pragmas. journal_mode=WAL is persistent (set once per file)
# and is applied separately with a bounded retry: SQLite does not invoke the
# busy handler for a journal-mode change, so concurrent first opens of a
# fresh database could fail instantly with "database is locked".
_CONNECTION_PRAGMAS = (
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
)

# Schema DDL — executed inside BEGIN IMMEDIATE (which DOES honour the busy
# timeout) so concurrent initialisations serialise instead of failing.
DDL = """
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
    -- v2 (plan WP6): size/mtime PER LEG. The shared file_size/mtime pair was
    -- overwritten by whichever leg wrote last, so the cloud diff compared
    -- against LAN values ("579 files copied" on no-change nights).
    lan_size        INTEGER,
    lan_mtime       REAL,
    cloud_size      INTEGER,
    cloud_mtime     REAL,
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
    status          TEXT NOT NULL,          -- 'VERIFIED' | 'VERIFICATION_FAILED' | 'AUDIT_INCOMPLETE' | 'PENDING_BACKUP'
    files_checked   INTEGER DEFAULT 0,
    bytes_checked   INTEGER DEFAULT 0,
    mismatches      INTEGER DEFAULT 0,
    detail          TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_integrity_audits_mode ON integrity_audits(mode, started_at);

-- Every alert outcome (plan §5.5): SENT / FAILED / SUPPRESSED / DISABLED.
-- Lets the dashboard show that alert delivery itself is failing.
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    mode        TEXT,
    subject     TEXT,
    status      TEXT NOT NULL,
    error       TEXT,
    attempts    INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL,
    fault_code  TEXT,           -- core.faults code (v3, audit 2026-10-18)
    message_id  TEXT,           -- Message-ID of the email sent (v3)
    thread_id   TEXT,           -- Message-ID of the incident's first email (v3)
    attachments_rung TEXT,      -- which attachment rung was delivered (v4, plan B)
    audience    TEXT,           -- alerts | reports | both: which recipient list it was FOR (v5)
    recipients  TEXT            -- JSON list of the addresses it was addressed to (v5)
);

CREATE INDEX IF NOT EXISTS idx_notifications_key ON notifications(event_key, created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status, created_at);

-- Human approval of a BLOCKED run (plan §6.2). One request per blocked run,
-- covering every guard that blocked it; single use; expires.
CREATE TABLE IF NOT EXISTS approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id     TEXT NOT NULL UNIQUE,
    leg             TEXT NOT NULL,          -- 'lan' | 'cloud'
    blocked_run_id  TEXT NOT NULL,
    fy              TEXT,
    guards          TEXT NOT NULL,          -- JSON [{guard, reason, metrics}]
    phrase          TEXT NOT NULL,
    status          TEXT NOT NULL,          -- core.approvals PENDING/APPROVED/...
    created_at      TEXT NOT NULL,
    approved_at     TEXT,
    approved_by     TEXT,
    expires_at      TEXT,
    dismissed_at    TEXT,
    dismissed_by    TEXT,
    used_at         TEXT,
    used_by_run_id  TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_leg_status ON approvals(leg, status);

-- Settings changed on the dashboard (DEC-9): who, when, what. Secrets are
-- stored as '(hidden)', never their value.
CREATE TABLE IF NOT EXISTS config_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at   TEXT NOT NULL,
    changed_by   TEXT NOT NULL,
    key          TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    risky        INTEGER NOT NULL DEFAULT 0,
    backup_file  TEXT
);

-- FY rollover step journal (plan A, v4): one row per (from_fy, to_fy), one row per step.
-- The daily check resumes at the first step that is not done / skipped.
CREATE TABLE IF NOT EXISTS rollover_journal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_fy     TEXT NOT NULL,
    to_fy       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (from_fy, to_fy)
);

CREATE TABLE IF NOT EXISTS rollover_journal_steps (
    journal_id  INTEGER NOT NULL REFERENCES rollover_journal(id),
    step        TEXT NOT NULL,
    state       TEXT NOT NULL,          -- core.rollover_journal pending/done/failed/skipped
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    first_failed_at TEXT,       -- first failure of the current streak (cleared on done)
    started_at  TEXT,
    finished_at TEXT,
    run_id      TEXT,
    PRIMARY KEY (journal_id, step)
);

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
        busy_timeout_ms: int = model_default(MaintenanceConfig, "sqlite_busy_timeout_ms"),
        vacuum_freelist_threshold: int = model_default(MaintenanceConfig, "sqlite_vacuum_freelist_threshold"),
        synchronous: str = model_default(MaintenanceConfig, "sqlite_synchronous"),
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

    @classmethod
    def from_config(cls, config) -> "ManifestDB":
        """Open the manifest DB with every tunable taken from config.

        Single construction path for production code (plan §5.4): no call
        site may hand-copy busy_timeout / vacuum / synchronous settings.
        """
        m = config.maintenance
        return cls(
            config.paths.database_path,
            busy_timeout_ms=m.sqlite_busy_timeout_ms,
            vacuum_freelist_threshold=m.sqlite_vacuum_freelist_threshold,
            synchronous=m.sqlite_synchronous,
        )

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            # decided BEFORE connecting: sqlite creates the file on connect, and the schema script runs
            # on every open, so only "the file did not exist" proves this open creates the database
            fresh = self.db_path != ":memory:" and not os.path.exists(self.db_path)
            # M-6: the busy handler must be active BEFORE the first statement.
            # sqlite3.connect(timeout=) installs it at open time, and the
            # PRAGMA below pins the same value; previously the DDL ran with
            # SQLite's default 0 ms timeout and concurrent opens lost
            # run_history rows ("database is locked" swallowed upstream).
            conn = sqlite3.connect(
                self.db_path,
                timeout=self.busy_timeout_ms / 1000.0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
            try:
                self._retry_on_lock(lambda: conn.execute("PRAGMA journal_mode=WAL"))
                for pragma in _CONNECTION_PRAGMAS:
                    conn.execute(pragma)
                self._retry_on_lock(lambda: self._apply_ddl(conn))
                if fresh:       # INSERT OR IGNORE: several processes may open a new file together
                    conn.execute("INSERT OR IGNORE INTO db_meta (key, value) VALUES ('created_at', ?)",
                                 (now_iso(),))
                    conn.commit()
            except BaseException:
                conn.close()
                raise
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
                conn.execute("BEGIN IMMEDIATE")
                # Re-check inside the write lock: another process may have
                # migrated between our version read and BEGIN IMMEDIATE.
                if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
                    conn.rollback()
                    return
                columns = {
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA table_info(run_history)"
                    ).fetchall()
                }
                if "extended_metrics" not in columns:
                    conn.execute(
                        "ALTER TABLE run_history ADD COLUMN extended_metrics TEXT"
                    )
                # v2 (WP6): per-leg size/mtime. Existing rows stay NULL - the
                # old shared value cannot be attributed to a leg truthfully.
                fe_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(file_entries)").fetchall()
                }
                for column, sql_type in (("lan_size", "INTEGER"), ("lan_mtime", "REAL"),
                                         ("cloud_size", "INTEGER"), ("cloud_mtime", "REAL")):
                    if column not in fe_columns:
                        conn.execute(
                            f"ALTER TABLE file_entries ADD COLUMN {column} {sql_type}")
                # v3 (audit 2026-10-18): incidents - fault code and email ids
                n_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(notifications)").fetchall()
                }
                for column in ("fault_code", "message_id", "thread_id", "attachments_rung",
                               "audience", "recipients"):
                    if n_columns and column not in n_columns:
                        conn.execute(f"ALTER TABLE notifications ADD COLUMN {column} TEXT")
                # Legacy tables may predate UNIQUE(run_id): drop duplicate
                # run_ids (keep the first) and add the unique index that
                # insert_run's ON CONFLICT(run_id) relies on. Runs once,
                # under the version gate — never on every connection open.
                if not self._run_id_is_unique(conn):
                    conn.execute(
                        "DELETE FROM run_history WHERE id NOT IN "
                        "(SELECT MIN(id) FROM run_history GROUP BY run_id)"
                    )
                    conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "idx_run_history_run_id ON run_history(run_id)"
                    )
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

    def _retry_on_lock(self, operation):
        """Run `operation`, retrying only on SQLITE_BUSY/locked errors.

        Bounded by the configured busy timeout (tenacity, already a
        dependency). Any other OperationalError propagates immediately.
        """
        from tenacity import (
            retry,
            retry_if_exception,
            stop_after_delay,
            wait_random_exponential,
        )

        def _is_lock_error(exc: BaseException) -> bool:
            return isinstance(exc, sqlite3.OperationalError) and (
                "locked" in str(exc).lower() or "busy" in str(exc).lower()
            )

        return retry(
            retry=retry_if_exception(_is_lock_error),
            stop=stop_after_delay(max(self.busy_timeout_ms / 1000.0, 1.0)),
            wait=wait_random_exponential(multiplier=0.05, max=1.0),
            reraise=True,
        )(operation)()

    @staticmethod
    def _apply_ddl(conn: sqlite3.Connection) -> None:
        """Create/verify the schema atomically under a RESERVED write lock."""
        try:
            conn.executescript(f"BEGIN IMMEDIATE;{DDL}COMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

    @staticmethod
    def _run_id_is_unique(conn: sqlite3.Connection) -> bool:
        """True if some UNIQUE index covers exactly run_history(run_id)."""
        for idx in conn.execute("PRAGMA index_list(run_history)").fetchall():
            if not idx["unique"]:
                continue
            cols = [r["name"] for r in conn.execute(
                f"PRAGMA index_info('{idx['name']}')").fetchall()]
            if cols == ["run_id"]:
                return True
        return False

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
        size_field = f"{mode}_size"
        mtime_field = f"{mode}_mtime"

        with self._lock:
            conn = self._get_conn()
            now = now_iso()
            # Chunk at 100 rows (700 params) to stay under SQLite's variable limit
            # on older builds (SQLITE_MAX_VARIABLE_NUMBER=999).
            for i in range(0, len(entries), 100):
                chunk = entries[i : i + 100]
                conn.executemany(
                    f"""INSERT INTO file_entries
                        (relative_path, file_size, mtime, {size_field}, {mtime_field},
                         md5_checksum, {status_field}, {ts_field},
                         created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?,
                                'synced', ?,
                                ?, ?)
                        ON CONFLICT(relative_path) DO UPDATE SET
                            file_size = excluded.file_size,
                            mtime = excluded.mtime,
                            {size_field} = excluded.{size_field},
                            {mtime_field} = excluded.{mtime_field},
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

    def clear_mode_status(self, mode: str, paths: list[str]) -> int:
        """Null-out sync status for one mode; delete row only when both modes are NULL.

        Replaces the unconditional DELETE used by record_sync_results so that pruning
        a file from LAN does not destroy its cloud_status (and vice-versa).

        Returns:
            Number of requested paths processed.
        """
        if mode not in ("cloud", "lan"):
            raise ValueError(f"mode must be 'cloud' or 'lan', got {mode!r}")
        if not paths:
            return 0
        status_field = f"{mode}_status"
        ts_field = f"{mode}_last_synced_at"
        normalized = [p.replace("\\", "/") for p in paths]
        with self._lock:
            conn = self._get_conn()
            with conn:
                for i in range(0, len(normalized), 500):
                    chunk = normalized[i : i + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    conn.execute(
                        f"UPDATE file_entries SET {status_field} = NULL, "
                        f"{ts_field} = NULL "
                        f"WHERE relative_path IN ({placeholders})",
                        chunk,
                    )
                # Orphan cleanup: delete rows that are truly gone from both destinations
                conn.execute(
                    "DELETE FROM file_entries "
                    "WHERE lan_status IS NULL AND cloud_status IS NULL"
                )
        return len(normalized)


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
        """Cloud-synced entries as {relative_path: (cloud_size, cloud_mtime)}.

        The CLOUD leg's own last-recorded values (schema v2) - never the LAN
        leg's. None = unknown (rows from before v2). Used only for the
        metrics cross-check (plan WP6).
        """
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT relative_path, cloud_size, cloud_mtime "
                "FROM file_entries WHERE cloud_status = 'synced'"
            ).fetchall()
        return {r["relative_path"]: (r["cloud_size"], r["cloud_mtime"]) for r in rows}

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

    def get_runs_between(self, start, end, mode: str | None = None) -> list[dict]:
        """Runs whose START lies in [start, end), newest first (plan C2). `started_at` is stored
        as an IST ISO string with its offset, so the bounds are rendered the same way."""
        lo, hi = start.in_timezone(IST).isoformat(), end.in_timezone(IST).isoformat()
        sql = "SELECT * FROM run_history WHERE started_at >= ? AND started_at < ?"
        args: list = [lo, hi]
        if mode:
            sql += " AND mode = ?"
            args.append(mode)
        with self._lock:
            rows = self._get_conn().execute(sql + " ORDER BY started_at DESC", args).fetchall()
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
        """Return the last successful run for this mode.

        "Successful" is the exact set core.statuses.SUCCESS_STATUSES —
        never a suffix match.
        """
        from core.statuses import SUCCESS_STATUSES

        success = sorted(SUCCESS_STATUSES)
        placeholders = ",".join("?" for _ in success)
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                f"SELECT * FROM run_history WHERE mode = ? AND status IN ({placeholders}) "
                "ORDER BY started_at DESC LIMIT 1",
                (mode, *success),
            ).fetchone()
            return dict(row) if row else None

    def previous_run_status(self, mode: str, run_id: str) -> str | None:
        """Status of the latest run of this leg other than `run_id`."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT status FROM run_history WHERE mode = ? AND run_id != ? "
                "ORDER BY started_at DESC LIMIT 1", (mode, run_id)).fetchone()
        return row[0] if row else None

    def previous_run_fault_code(self, mode: str, run_id: str) -> str | None:
        """core.faults code stored on the latest run of this leg other than `run_id`."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT extended_metrics FROM run_history WHERE mode = ? AND run_id != ? "
                "ORDER BY started_at DESC LIMIT 1", (mode, run_id)).fetchone()
        try:
            metrics = json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            return None
        return metrics.get("fault_code") if isinstance(metrics, dict) else None

    def guard_baseline(self, mode: str, fy: str) -> dict | None:
        """Guard record of the last successful run of this leg in this FY.

        G2 (source shrink) and G4 (destination identity) compare against it.
        Scoped to the FY so the April rollover (new, almost empty FY folder;
        new destination path) starts a fresh baseline instead of blocking.
        Rows written before guards existed carry no record and are skipped.
        """
        from core.statuses import SUCCESS_STATUSES

        success = sorted(SUCCESS_STATUSES)
        placeholders = ",".join("?" for _ in success)
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                f"SELECT extended_metrics FROM run_history "
                f"WHERE mode = ? AND status IN ({placeholders}) "
                "AND json_valid(extended_metrics) "
                "AND json_extract(extended_metrics, '$.guards.fy') = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (mode, *success, fy),
            ).fetchone()
        return json.loads(row[0])["guards"] if row else None

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
        if data["status"] not in (AuditStatus.VERIFIED, AuditStatus.VERIFICATION_FAILED,
                                  AuditStatus.AUDIT_INCOMPLETE, AuditStatus.PENDING_BACKUP):
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
        """Return the audit row that counts for a mode (and optional scope).

        Returns None when no audit has ever run — callers render that as
        NOT_VERIFIED. A VERIFICATION_FAILED row stays authoritative until a
        later audit reaches a verdict (A1): an AUDIT_INCOMPLETE row newer than it
        (the check could not run) does not replace it; the failed row is returned
        with `newer_incomplete_at` set so readers can say so. Audits never touch
        run_history.
        """
        with self._lock:
            conn = self._get_conn()
            newest = self._newest_audit(conn, mode, scope)
            if newest is None or newest["status"] != AuditStatus.AUDIT_INCOMPLETE:
                return newest
            verdict = self._newest_audit(conn, mode, scope, statuses.AUDIT_VERDICT_STATUSES)
            if verdict is not None and verdict["status"] == AuditStatus.VERIFICATION_FAILED:
                return {**verdict,
                        "newer_incomplete_at": newest.get("ended_at") or newest["started_at"]}
            return newest

    @staticmethod
    def _newest_audit(conn, mode: str, scope: str | None, only_statuses=None) -> dict | None:
        sql, params = "SELECT * FROM integrity_audits WHERE mode = ?", [mode]
        if scope is not None:
            sql += " AND scope = ?"
            params.append(scope)
        if only_statuses is not None:
            sql += f" AND status IN ({','.join('?' * len(only_statuses))})"
            params.extend(sorted(only_statuses))
        row = conn.execute(sql + " ORDER BY started_at DESC LIMIT 1", params).fetchone()
        return dict(row) if row else None

    # ── Notifications ────────────────────────────────────────

    # ── Approvals (plan §6.2) ─────────────────────────────────────────────

    def open_approval_request(self, *, leg: str, blocked_run_id: str, fy: str | None,
                              guards: list[dict], phrase: str, created_at: str) -> str:
        """Record a request for a blocked run; older PENDING ones of the leg
        are SUPERSEDED (only the latest situation can be approved)."""
        from core import approvals as ap

        approval_id = ap.new_approval_id()
        with self._lock:
            conn = self._get_conn()
            with conn:
                conn.execute("UPDATE approvals SET status = ? WHERE leg = ? AND status = ?",
                             (ap.SUPERSEDED, leg, ap.PENDING))
                conn.execute(
                    """INSERT INTO approvals (approval_id, leg, blocked_run_id, fy, guards,
                                              phrase, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (approval_id, leg, blocked_run_id, fy, json.dumps(guards), phrase,
                     ap.PENDING, created_at))
        return approval_id

    @staticmethod
    def _approval_row(row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        d["guards"] = json.loads(d["guards"])
        return d

    def get_approval(self, approval_id: str) -> dict | None:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT * FROM approvals WHERE approval_id = ?",
                               (approval_id,)).fetchone()
        return self._approval_row(row)

    def open_approvals(self) -> list[dict]:
        """Requests a human can still act on: PENDING, or APPROVED but unused."""
        from core import approvals as ap

        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status IN (?, ?) ORDER BY id",
                (ap.PENDING, ap.APPROVED)).fetchall()
        return [self._approval_row(r) for r in rows]

    def _transition(self, sql: str, params: tuple) -> bool:
        with self._lock:
            conn = self._get_conn()
            with conn:
                return conn.execute(sql, params).rowcount == 1

    def approve_request(self, approval_id: str, *, approved_by: str, approved_at: str,
                        expires_at: str) -> bool:
        from core import approvals as ap

        return self._transition(
            "UPDATE approvals SET status = ?, approved_by = ?, approved_at = ?, expires_at = ? "
            "WHERE approval_id = ? AND status = ?",
            (ap.APPROVED, approved_by, approved_at, expires_at, approval_id, ap.PENDING))

    def dismiss_request(self, approval_id: str, *, dismissed_by: str, dismissed_at: str) -> bool:
        from core import approvals as ap

        return self._transition(
            "UPDATE approvals SET status = ?, dismissed_by = ?, dismissed_at = ? "
            "WHERE approval_id = ? AND status IN (?, ?)",
            (ap.DISMISSED, dismissed_by, dismissed_at, approval_id, ap.PENDING, ap.APPROVED))

    def mark_approval_used(self, approval_id: str, *, run_id: str, used_at: str) -> bool:
        """Consume an approval exactly once (atomic; False if already used)."""
        from core import approvals as ap

        return self._transition(
            "UPDATE approvals SET status = ?, used_at = ?, used_by_run_id = ? "
            "WHERE approval_id = ? AND status = ? AND used_at IS NULL",
            (ap.USED, used_at, run_id, approval_id, ap.APPROVED))

    def record_notification(self, data: dict) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO notifications
                   (event_key, kind, mode, subject, status, error, attempts, created_at,
                    fault_code, message_id, thread_id, attachments_rung, audience, recipients)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (data["event_key"], data["kind"], data.get("mode"), data.get("subject"),
                 data["status"], data.get("error"), data.get("attempts", 0),
                 data["created_at"], data.get("fault_code"), data.get("message_id"),
                 data.get("thread_id"), data.get("attachments_rung"), data.get("audience"),
                 json.dumps(data["recipients"]) if data.get("recipients") is not None else None),
            )
            conn.commit()

    # ── FY rollover journal (plan A) ─────────────────────────────────────

    def _journal_row(self, conn, from_fy: str, to_fy: str):
        return conn.execute("SELECT * FROM rollover_journal WHERE from_fy = ? AND to_fy = ?",
                            (from_fy, to_fy)).fetchone()

    def _journal_dict(self, conn, row) -> dict:
        steps = {r["step"]: {k: r[k] for k in ("state", "attempts", "last_error", "started_at",
                                               "first_failed_at", "finished_at", "run_id")}
                 for r in conn.execute("SELECT * FROM rollover_journal_steps WHERE journal_id = ?",
                                       (row["id"],))}
        return {"from_fy": row["from_fy"], "to_fy": row["to_fy"],
                "created_at": row["created_at"],
                "steps": {name: steps[name] for name in rj.STEPS}}

    def rollover_open(self, from_fy: str, to_fy: str, now: str) -> dict:
        """The journal of this transition; created with every step pending if new
        (idempotent: an existing journal is returned unchanged)."""
        with self._lock:
            conn = self._get_conn()
            with conn:
                conn.execute("INSERT OR IGNORE INTO rollover_journal (from_fy, to_fy, created_at) "
                             "VALUES (?, ?, ?)", (from_fy, to_fy, now))
                row = self._journal_row(conn, from_fy, to_fy)
                for name in rj.STEPS:
                    conn.execute("INSERT OR IGNORE INTO rollover_journal_steps "
                                 "(journal_id, step, state) VALUES (?, ?, ?)",
                                 (row["id"], name, rj.PENDING))
            return self._journal_dict(conn, row)

    def rollover_get(self, from_fy: str, to_fy: str) -> dict | None:
        with self._lock:
            conn = self._get_conn()
            row = self._journal_row(conn, from_fy, to_fy)
            return self._journal_dict(conn, row) if row else None

    def rollover_incomplete(self) -> dict | None:
        """The newest journal that still has a step to do, or None."""
        with self._lock:
            conn = self._get_conn()
            for row in conn.execute("SELECT * FROM rollover_journal ORDER BY id DESC"):
                journal = self._journal_dict(conn, row)
                if not rj.is_complete(journal):
                    return journal
        return None

    def _rollover_step_update(self, from_fy: str, to_fy: str, step: str, sql: str,
                              params: tuple) -> None:
        if step not in rj.STEPS:
            raise ValueError(f"unknown rollover step {step!r}")
        with self._lock:
            conn = self._get_conn()
            row = self._journal_row(conn, from_fy, to_fy)
            if row is None:
                raise ValueError(f"no rollover journal for {from_fy} -> {to_fy}")
            with conn:
                conn.execute(f"UPDATE rollover_journal_steps SET {sql} "
                             "WHERE journal_id = ? AND step = ?", (*params, row["id"], step))

    def rollover_step_start(self, from_fy: str, to_fy: str, step: str, now: str,
                            run_id: str | None = None) -> None:
        self._rollover_step_update(
            from_fy, to_fy, step,
            "attempts = attempts + 1, started_at = ?, finished_at = NULL, run_id = ?",
            (now, run_id))

    def rollover_step_done(self, from_fy: str, to_fy: str, step: str, now: str) -> None:
        self._rollover_step_update(from_fy, to_fy, step,
                                   "state = ?, finished_at = ?, last_error = NULL, "
                                   "first_failed_at = NULL", (rj.DONE, now))

    def rollover_step_failed(self, from_fy: str, to_fy: str, step: str, now: str,
                             error: str) -> None:
        self._rollover_step_update(from_fy, to_fy, step,
                                   "state = ?, finished_at = ?, last_error = ?, "
                                   "first_failed_at = COALESCE(first_failed_at, ?)",
                                   (rj.FAILED, now, error, now))

    def rollover_step_reset(self, from_fy: str, to_fy: str, step: str) -> None:
        """Back to pending (attempts kept) - used when a done step must be repeated."""
        self._rollover_step_update(from_fy, to_fy, step,
                                   "state = ?, finished_at = NULL", (rj.PENDING,))

    def rollover_step_skipped(self, from_fy: str, to_fy: str, step: str, now: str,
                              reason: str) -> None:
        self._rollover_step_update(from_fy, to_fy, step,
                                   "state = ?, finished_at = ?, last_error = ?",
                                   (rj.SKIPPED, now, reason))

    def last_notification(self, event_key: str, status: str | None = None) -> dict | None:
        with self._lock:
            conn = self._get_conn()
            if status:
                row = conn.execute(
                    "SELECT * FROM notifications WHERE event_key = ? AND status = ? "
                    "ORDER BY id DESC LIMIT 1", (event_key, status)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM notifications WHERE event_key = ? "
                    "ORDER BY id DESC LIMIT 1", (event_key,)).fetchone()
            return dict(row) if row else None

    def sent_in_mode_after(self, mode: str, after_id: int, *, recovered: bool) -> bool:
        """Was a RECOVERED (recovered=True) or a problem email (False) of this
        mode SENT after notification `after_id`? Ends / starts an incident."""
        op = "=" if recovered else "!="
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                f"SELECT 1 FROM notifications WHERE mode = ? AND id > ? AND status = 'SENT' "
                f"AND kind {op} 'RECOVERED' LIMIT 1", (mode, after_id)).fetchone()
        return row is not None

    def last_open_incident(self, mode: str) -> dict | None:
        """Newest SENT problem email of this mode with no RECOVERED after it."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM notifications WHERE mode = ? AND status = 'SENT' "
                "AND kind != 'RECOVERED' AND id > COALESCE((SELECT MAX(id) FROM notifications "
                "WHERE mode = ? AND status = 'SENT' AND kind = 'RECOVERED'), 0) "
                "ORDER BY id DESC LIMIT 1", (mode, mode)).fetchone()
        return dict(row) if row else None

    def notification_by_message_id(self, message_id: str) -> dict | None:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT * FROM notifications WHERE message_id = ? "
                               "ORDER BY id LIMIT 1", (message_id,)).fetchone()
        return dict(row) if row else None

    def notification_failures_since(self, days: int) -> list[dict]:
        """Emails that could not be delivered in the last `days` days AND are newer than the last
        SENT email of the SAME audience (D3): a later SENT proves delivery to those addresses works
        again, so the earlier failure no longer describes the present. A SENT `both` mail (the test
        email) proves either list. A delivered report must not hide a rejected alert address, so one
        audience never clears another. Rows from before v5 have no audience and count as alerts.
        SUPPRESSED / DISABLED are not proof."""
        cutoff = cutoff_iso(days)
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT n.* FROM notifications n WHERE n.status = 'FAILED' AND n.created_at >= ? "
                "AND n.id > COALESCE((SELECT MAX(s.id) FROM notifications s WHERE s.status = 'SENT' "
                "AND (COALESCE(s.audience, 'alerts') = COALESCE(n.audience, 'alerts') "
                "OR s.audience = 'both')), 0) "
                "ORDER BY n.id DESC", (cutoff,)).fetchall()
            return [dict(r) for r in rows]

    def record_config_change(self, data: dict) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO config_changes
                   (changed_at, changed_by, key, old_value, new_value, risky, backup_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (data["changed_at"], data["changed_by"], data["key"], data.get("old_value"),
                 data.get("new_value"), int(data.get("risky", 0)), data.get("backup_file")),
            )
            conn.commit()

    # ── what the overdue check needs to know (core.overdue, core.attention) ─────────────

    def _meta(self, key: str) -> str | None:
        with self._lock:
            row = self._get_conn().execute("SELECT value FROM db_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES (?, ?)", (key, value))
            conn.commit()

    def meta_created_at(self) -> str | None:
        """When this database file was created (None for one that predates the marker)."""
        return self._meta("created_at")

    def set_meta_created_at(self, iso_ts: str) -> None:
        self._set_meta("created_at", iso_ts)

    def first_known_at(self) -> str | None:
        """The earliest moment this installation is known to have existed: the creation marker or the
        oldest run, whichever is earlier (a pre-marker database has only the runs)."""
        import pendulum

        with self._lock:
            row = self._get_conn().execute("SELECT MIN(started_at) FROM run_history").fetchone()
        candidates = [t for t in (self.meta_created_at(), row[0] if row else None) if t]
        if not candidates:
            return None
        return min(candidates, key=lambda t: pendulum.parse(t).timestamp())

    _SCHEDULE_KEYS = ("cloud_cron", "lan_cron", "timezone")

    def _stored_schedule(self) -> dict | None:
        raw = self._meta("schedule_in_effect")
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            return None
        if (isinstance(data, dict) and all(isinstance(data.get(k), str) and data[k]
                                           for k in self._SCHEDULE_KEYS)):
            since = data.get("since")
            return {**{k: data[k] for k in self._SCHEDULE_KEYS},
                    "since": since if isinstance(since, str) and since else None}
        return None

    def set_schedule_in_effect(self, schedule: dict, since: str | None = None) -> None:
        """Record the schedules the service actually registered (written by serve.py just before it
        starts serving). A schedule saved in Settings but not yet applied is NOT in effect.
        `since` = when this schedule took effect: kept while the schedule is unchanged (a restart must
        not move it), set to now (or `since`) when a cron string or the timezone changed."""
        new = {k: str(schedule[k]) for k in self._SCHEDULE_KEYS}
        old = self._stored_schedule()
        unchanged = old is not None and all(old[k] == new[k] for k in self._SCHEDULE_KEYS)
        stamp = old["since"] if unchanged and old.get("since") else (since or now_iso())
        self._set_meta("schedule_in_effect", json.dumps({**new, "since": stamp}))

    def get_schedule_in_effect(self) -> dict | None:
        """The recorded schedules (+ `since`), or None when missing or not valid (callers fall back
        to the config)."""
        return self._stored_schedule()

    def told_about_leg_since(self, mode: str, since_iso: str) -> bool:
        """True when a failure / action email for this leg was created after `since_iso` and the owner
        therefore already knows: SENT, or SUPPRESSED (only ever written when an earlier mail of the same
        unchanged incident was SENT). FAILED and DISABLED mails told nobody, and the overdue mail
        itself (`stale:` keys) is what is being decided, so neither counts. Times are compared as
        instants, never as strings."""
        import pendulum

        since = pendulum.parse(since_iso).timestamp()
        with self._lock:
            rows = self._get_conn().execute(
                "SELECT created_at FROM notifications WHERE mode = ? AND status IN ('SENT', 'SUPPRESSED') "
                "AND kind IN ('FAILURE', 'ACTION_REQUIRED') AND event_key NOT LIKE 'stale:%' "
                "AND COALESCE(audience, 'alerts') IN ('alerts', 'both') "
                "ORDER BY id DESC LIMIT 200", (mode,)).fetchall()
        for (created,) in rows:
            try:
                if pendulum.parse(created).timestamp() > since:
                    return True
            except Exception:  # noqa: BLE001 - an unreadable row counts as nothing
                continue
        return False

    def last_config_change_at(self, key_prefix: str) -> str | None:
        """When a setting starting with `key_prefix` was last changed on the dashboard."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT MAX(changed_at) FROM config_changes WHERE key LIKE ?",
                               (key_prefix + "%",)).fetchone()
        return row[0] if row else None

    def last_sent_at(self, mode: str) -> str | None:
        """When an email of this mode (e.g. 'test') was last delivered."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT MAX(created_at) FROM notifications WHERE mode = ? "
                               "AND status = 'SENT'", (mode,)).fetchone()
        return row[0] if row else None

    def config_changes(self, limit: int = 100) -> list[dict]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute("SELECT * FROM config_changes ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
            return [dict(r) for r in rows]

    def latest_notification_for_mode(self, mode: str, since_days: int) -> dict | None:
        """Newest notification of a pipeline/mode (e.g. 'watchdog') in the window."""
        cutoff = cutoff_iso(since_days)
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM notifications WHERE mode = ? AND created_at >= ? "
                "ORDER BY id DESC LIMIT 1", (mode, cutoff)).fetchone()
            return dict(row) if row else None

    # ── Maintenance ──────────────────────────────────────────

    def wal_checkpoint(self):
        """Truncate WAL file after backup run to prevent bloat."""
        with self._lock:
            conn = self._get_conn()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def abandon_dead_runs(self, owner_alive, older_than_hours: float) -> list[dict]:
        """*_RUNNING rows whose run can no longer finish -> *_ABANDONED (plan WP5).

        A RUNNING row carries its owner process in extended_metrics["owner"]
        ('PID:create_time'). owner_alive(token) -> True / False / None:
          * False (the process is provably gone) -> ABANDONED at once, whatever
            the age (agent restart, reboot or crash minutes after the start);
          * True -> never touched (the run is really in progress);
          * None or no owner (unknown, rows written before owners were
            recorded) -> ABANDONED only after `older_than_hours`.
        Returns the changed rows ({run_id, mode, status, started_at, error_message}).
        """
        import pendulum

        from core.time_utils import IST

        cutoff = pendulum.now(IST).subtract(hours=older_than_hours)
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT run_id, mode, status, started_at, extended_metrics FROM run_history "
                "WHERE status IN ('CLOUD_RUNNING', 'LAN_RUNNING', 'ROLLOVER_RUNNING')"
            ).fetchall()
        changed = []
        for run_id, mode, status, started_at, metrics in rows:
            try:
                owner = (json.loads(metrics) or {}).get("owner") if metrics else None
            except (ValueError, AttributeError):
                owner = None
            alive = owner_alive(owner) if owner else None
            if alive is True:
                continue
            if alive is False:
                reason = ("The run's process ended without recording an outcome (service "
                          "stop, reboot or crash) - this backup did not finish.")
            else:
                try:
                    stale = pendulum.parse(started_at) < cutoff
                except Exception:  # noqa: BLE001 - unparsable start = treat as stale
                    stale = True
                if not stale:
                    continue
                reason = (f"The run stopped without recording an outcome; still RUNNING "
                          f"after {older_than_hours}h - this backup did not finish.")
            new_status = status.replace("_RUNNING", "_ABANDONED")
            with self._lock:
                conn = self._get_conn()
                with conn:
                    cur = conn.execute(
                        "UPDATE run_history SET status = ?, ended_at = COALESCE(ended_at, ?), "
                        "error_message = COALESCE(error_message, ?) "
                        "WHERE run_id = ? AND status = ?",
                        (new_status, now_iso(), reason, run_id, status))
            if cur.rowcount == 1:   # the run did not finish meanwhile
                changed.append({"run_id": run_id, "mode": mode, "status": new_status,
                                "started_at": started_at, "error_message": reason})
        return changed

    def purge_old_runs(self, retention_days: int = model_default(MaintenanceConfig, "db_retention_days")):
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
