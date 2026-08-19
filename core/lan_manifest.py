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
    files, _errors = walk_lan_destination_detailed(unc_path)
    return files


def walk_lan_destination_detailed(unc_path: str) -> tuple[list[dict], int]:
    """Like walk_lan_destination, but also reports subdirectory walk errors.

    os.walk() swallows directory errors by default (an SMB session drop, a
    timeout, or a permission fault mid-walk simply skips the subtree). A
    silently truncated walk must never be diffed against the previous
    snapshot — every file missing from the short list would be treated as
    "removed" and pruned from the manifest. Callers that act on the diff must
    use this variant and treat errors > 0 as an unusable snapshot.

    Returns:
        (files, error_count)
    """
    files: list[dict] = []
    errors = 0
    base = str(Path(unc_path).resolve())

    def _onerror(err: OSError) -> None:
        nonlocal errors
        errors += 1
        logger.warning(f"LAN manifest walk error at {getattr(err, 'filename', '?')}: {err}")

    for root, _, filenames in os.walk(unc_path, onerror=_onerror):
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

    if errors:
        logger.warning(f"LAN manifest: walk of {unc_path} had {errors} error(s)")
    logger.info(f"LAN manifest: {len(files)} files at {unc_path}")
    return files, errors


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
