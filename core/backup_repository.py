"""Backup repository — DB write operations for backup results.

Extracts duplicated ManifestDB interaction from flow.py tasks.
Centralizes file entry upserts, run history recording, and maintenance.
"""

from pathlib import PureWindowsPath
from loguru import logger

from core.manifest import ManifestDB


def _clean_path(raw_path: str | None) -> str:
    """Normalize path to POSIX forward-slash format and strip whitespace/leading slashes.

    Returns '' for empty, root, dot, or whitespace-only paths.
    """
    if not raw_path or not str(raw_path).strip():
        return ""
    p = PureWindowsPath(str(raw_path).strip()).as_posix().lstrip("/")
    return "" if p in (".", "") else p


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
        normalized = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            raw_path = e.get("Path") if e.get("Path") is not None else e.get("path")
            cleaned_p = _clean_path(raw_path)
            if not cleaned_p:
                continue
            normalized.append({
                "path": cleaned_p,
                "size": e.get("Size") if e.get("Size") is not None else e.get("size", 0),
                "mtime": e.get("ModTime") if e.get("ModTime") is not None else e.get("mtime", 0),
                "md5_checksum": e.get("md5_checksum") or e.get("MD5") or e.get("md5"),
            })

        if normalized:
            db.bulk_upsert_synced(normalized, mode)

            # Self-healing: prune stale entries that are marked synced in DB but no longer exist.
            active_paths = {item["path"] for item in normalized}
            pruned = db.prune_stale_synced(mode, active_paths)
            if pruned:
                logger.info(f"Pruned {pruned} stale {mode} entries from manifest")

    if removed:
        clean_removed = [_clean_path(p) for p in removed if _clean_path(p)]
        if clean_removed:
            db.delete_entries(clean_removed)


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
    files_failed: int = 0,
    extended_metrics: str | None = None,
) -> bool:
    """Record a backup run to run_history and checkpoint WAL.

    Returns True on success, False if recording failed.
    Never raises — safe to call from finally blocks.
    """
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
            # F12: persisted so reports/CSV show how many files actually failed
            "files_failed": files_failed,
            "extended_metrics": extended_metrics,
        })
        db.wal_checkpoint()
        return True
    except Exception as e:
        logger.error(f"Failed to record run history: {e}")
        return False
