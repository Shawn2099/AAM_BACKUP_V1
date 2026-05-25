"""LAN manifest — walk destination share, produce file inventory + diffs.

No scanner. No log parsing. No regex. Just os.walk + os.stat.
The filesystem IS the truth.
"""

import os
from pathlib import Path

from loguru import logger


def walk_lan_destination(unc_path: str) -> list[dict]:
    """Walk LAN share recursively. Returns every file with size and mtime.

    Skips files where stat() raises OSError (locked/deleted mid-walk).

    Args:
        unc_path: UNC path to walk (e.g. "\\\\192.168.10.10\\share$").

    Returns:
        [{"path": "rel\\path\\file.txt", "size": 2048, "mtime": 1717200000.0}, ...]
    """
    files: list[dict] = []
    base = str(Path(unc_path).resolve())

    for root, _, filenames in os.walk(unc_path):
        for name in filenames:
            full = os.path.join(root, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue

            rel = os.path.relpath(full, base)
            files.append({
                "path": rel,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })

    logger.info(f"LAN manifest: {len(files)} files at {unc_path}")
    return files


def snapshot_to_dict(files: list[dict]) -> dict[str, tuple[int, float]]:
    """Convert walk result to {relative_path: (size, mtime)} for O(1) diff."""
    return {f["path"]: (f["size"], f["mtime"]) for f in files}


def diff_snapshots(
    before: dict[str, tuple[int, float]],
    after: dict[str, tuple[int, float]],
) -> dict:
    """Compare two snapshots. Returns added, removed, modified, and unchanged paths.

    O(n) where n = number of files.

    Returns:
        {
            "added": [paths new in after],
            "removed": [paths gone from after],
            "modified": [paths where (size, mtime) changed],
            "unchanged": [paths where (size, mtime) is identical],
        }
    """
    before_set = set(before)
    after_set = set(after)

    intersection = before_set & after_set

    return {
        "added": sorted(after_set - before_set),
        "removed": sorted(before_set - after_set),
        "modified": sorted(
            p for p in intersection if before[p] != after[p]
        ),
        "unchanged": sorted(
            p for p in intersection if before[p] == after[p]
        ),
    }
