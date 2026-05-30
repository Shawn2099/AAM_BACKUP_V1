"""Backup repository — DB write operations for backup results.

Extracts duplicated ManifestDB interaction from flow.py tasks.
Centralizes file entry upserts, run history recording, and maintenance.
"""

from loguru import logger

from core.manifest import ManifestDB


def record_sync_results(
    db: ManifestDB,
    mode: str,
    entries: list[dict],
    removed: list[str] | None = None,
) -> None:
    """Record sync results to ManifestDB using bulk operations and prune stale entries.

    Normalizes entry dict keys from both rclone (Path/Size/ModTime)
    and os.walk (path/size/mtime) formats.

    Args:
        db: ManifestDB instance.
        mode: 'cloud' or 'lan'.
        entries: File entries from rclone lsjson or walk_lan_destination.
        removed: List of relative paths removed from destination.
    """
    if entries:
        normalized = [
            {
                "path": e.get("Path") or e.get("path", ""),
                "size": e.get("Size") or e.get("size", 0),
                "mtime": e.get("ModTime") or e.get("mtime", 0),
            }
            for e in entries
        ]
        db.bulk_upsert_synced(normalized, mode)

        # Self-healing: prune stale entries that are marked synced in DB but no longer exist
        active_paths = {item["path"] for item in normalized}
        with db._lock:
            conn = db._get_conn()
            status_field = "cloud_status" if mode == "cloud" else "lan_status"
            db_paths = [
                row["relative_path"]
                for row in conn.execute(
                    f"SELECT relative_path FROM file_entries WHERE {status_field} = 'synced'"
                ).fetchall()
            ]
            stale_paths = [p for p in db_paths if p not in active_paths]
            if stale_paths:
                for path in stale_paths:
                    conn.execute(
                        f"UPDATE file_entries SET {status_field} = NULL, "
                        f"{mode}_last_synced_at = NULL WHERE relative_path = ?",
                        (path,),
                    )
                conn.execute(
                    "DELETE FROM file_entries WHERE lan_status IS NULL AND cloud_status IS NULL"
                )
                conn.commit()

    if removed:
        db.delete_entries(removed)


def record_run_history(
    db: ManifestDB,
    *,
    run_id: str,
    mode: str,
    started_at: str,
    ended_at: str,
    status: str,
    exit_code: int,
    duration_seconds: float,
    error_message: str | None = None,
    files_copied: int = 0,
    bytes_copied: int = 0,
    extended_metrics: str | None = None,
) -> None:
    """Record a backup run to run_history and checkpoint WAL."""
    try:
        db.insert_run({
            "run_id": run_id,
            "mode": mode,
            "started_at": started_at,
            "ended_at": ended_at,
            "status": status,
            "exit_code": exit_code,
            "duration_seconds": duration_seconds,
            "error_message": error_message,
            "files_copied": files_copied,
            "bytes_copied": bytes_copied,
            "extended_metrics": extended_metrics,
        })
        db.wal_checkpoint()
    except Exception as e:
        logger.error(f"Failed to record run history: {e}")
