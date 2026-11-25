"""NAS versions folder retention purge and size accounting (Phase 1b B6/B7)."""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

from loguru import logger

from core.process import resolve_binary
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool
from models.config import resolve_versions_root

_RUN_FOLDER_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_\d{4}_FY\d{2}-\d{2}")


def parse_run_folder_date(name: str) -> dt.date | None:
    """Extract the deletion date from `<YYYY-MM-DD_HHmm>_<FY>...`, or None."""
    m = _RUN_FOLDER_DATE_RE.match(name or "")
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def folders_to_purge(versions_root: str | Path, retention_days: int, today: dt.date) -> list[Path]:
    """Immediate run-folder children of `versions_root` whose folder date is >= `retention_days` old."""
    root = Path(versions_root)
    if not root.is_dir():
        return []
    due: list[Path] = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        d = parse_run_folder_date(p.name)
        if d is not None and (today - d).days >= int(retention_days):
            due.append(p)
    return due


def versions_folder_size_bytes(versions_root: str | Path) -> int:
    """Total bytes currently stored under `versions_root` (0 if missing or unreadable)."""
    root = Path(versions_root)
    if not root.is_dir():
        return 0
    total = 0
    try:
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    continue
    except OSError:
        return total
    return total


def purge_old_versions(
    lan_destination: str,
    versions_folder: str,
    retention_days: int,
    *,
    artefacts_dir: str | None = None,
    timeout_s: int = 1800,
) -> dict:
    """Purge versions older than `retention_days` via `rclone delete --min-age Nd` + `rclone rmdirs`."""
    versions_root = resolve_versions_root(lan_destination, versions_folder)
    if not Path(versions_root).is_dir():
        return {"ok": True, "versions_root": versions_root, "skipped": "versions folder absent"}

    rclone = resolve_binary("rclone") or "rclone"
    art = RunArtefacts.open(artefacts_dir) if artefacts_dir else None

    del_cmd = [rclone, "delete", versions_root, "--min-age", f"{int(retention_days)}d", "--ignore-case"]
    del_res = run_tool("rclone-versions-delete", del_cmd, timeout_s=timeout_s, artefacts=art)
    if del_res.spawn_error or del_res.timed_out or del_res.exit_code != 0:
        err = del_res.spawn_error or ("timeout" if del_res.timed_out else f"exit {del_res.exit_code}")
        logger.warning(f"Versions purge delete failed ({versions_root}): {err}")
        return {"ok": False, "versions_root": versions_root, "error": err}

    rm_cmd = [rclone, "rmdirs", versions_root, "--leave-root"]
    rm_res = run_tool("rclone-versions-rmdirs", rm_cmd, timeout_s=timeout_s, artefacts=art)
    if rm_res.spawn_error or rm_res.timed_out or rm_res.exit_code != 0:
        err = rm_res.spawn_error or ("timeout" if rm_res.timed_out else f"exit {rm_res.exit_code}")
        logger.warning(f"Versions purge rmdirs failed ({versions_root}): {err}")
        return {"ok": False, "versions_root": versions_root, "error": err}

    return {"ok": True, "versions_root": versions_root}
