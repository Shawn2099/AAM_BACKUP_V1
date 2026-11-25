"""NAS versions folder retention purge and size accounting (Phase 1b B6/B7)."""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

from loguru import logger

from core.process import resolve_binary
from core.run_artefacts import RunArtefacts
from core.time_utils import IST
from core.tool_runner import run_tool
from models.config import LanConfig, model_default, resolve_versions_root

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
    timeout_s: int | None = None,
    today: dt.date | None = None,
) -> dict:
    """Purge due `<YYYY-MM-DD_HHmm>_<FY>...` run folders via `rclone purge`."""
    versions_root = resolve_versions_root(lan_destination, versions_folder)
    root_path = Path(versions_root)
    if not root_path.is_dir():
        return {
            "ok": True,
            "versions_root": versions_root,
            "skipped": "versions folder absent",
            "purged": [],
            "failed": [],
        }

    ref_today = today if today is not None else dt.datetime.now(IST).date()
    due = folders_to_purge(versions_root, retention_days, ref_today)
    effective_timeout = (
        int(model_default(LanConfig, "versions_purge_timeout_seconds"))
        if timeout_s is None
        else int(timeout_s)
    )
    rclone = resolve_binary("rclone") or "rclone"
    art = RunArtefacts.open(artefacts_dir) if artefacts_dir else RunArtefacts.ephemeral("versions-purge")
    resolved_root = root_path.resolve()

    purged: list[str] = []
    failed: list[dict[str, str]] = []
    for folder in due:
        if folder.resolve().parent != resolved_root:
            failed.append({"folder": folder.name, "error": "outside versions root"})
            continue
        cmd = [rclone, "purge", str(folder)]
        res = run_tool("rclone-versions-purge", cmd, timeout_s=effective_timeout, artefacts=art)
        if res.spawn_error or res.timed_out or res.exit_code != 0:
            err = res.spawn_error or ("timeout" if res.timed_out else f"exit {res.exit_code}")
            logger.warning(f"Versions purge failed ({folder}): {err}")
            failed.append({"folder": folder.name, "error": err})
        else:
            purged.append(folder.name)

    return {"ok": len(failed) == 0, "versions_root": versions_root, "purged": purged, "failed": failed}
