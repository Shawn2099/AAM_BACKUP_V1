"""LAN manifest — walk destination share, produce file inventory + diffs.

No scanner. No log parsing. No regex. Just os.walk + os.stat.
The filesystem IS the truth.
"""

import os
from pathlib import Path, PureWindowsPath

from loguru import logger


def walk_lan_destination(unc_path: str) -> list[dict]:
    """Walk LAN share recursively. Returns every file with size and mtime.

    Skips files where stat() raises OSError (locked/deleted mid-walk).

    H2 failure semantics — an inaccessible destination must be LOUD:
        - Root enumeration fails (share offline/unauthorized) → OSError raised.
          A silent [] here is indistinguishable from "destination empty" and
          corrupts every downstream diff/metric.
        - Subtree enumeration fails (locked dir, deleted mid-walk) → that
          subtree is skipped but a warning is logged; partial inventory returned.
        - Genuinely empty share → returns [] as before.

    Args:
        unc_path: UNC path to walk (e.g. "\\\\192.168.10.10\\share$").

    Returns:
        [{"path": "rel/path/file.txt", "size": 2048, "mtime": 1717200000.0}, ...]

    Raises:
        OSError: if the root of the share cannot be enumerated at all.
    """
    files: list[dict] = []
    errors: list[OSError] = []
    base = str(Path(unc_path).resolve())

    def _on_walk_error(err: OSError) -> None:
        errors.append(err)

    for root, _, filenames in os.walk(unc_path, onerror=_on_walk_error):
        for name in filenames:
            full = os.path.join(root, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue

            rel_raw = os.path.relpath(full, base)
            rel = PureWindowsPath(rel_raw).as_posix().lstrip("/")
            if not rel or rel == ".":
                continue
            files.append({
                "path": rel,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })

    root_failed = any(
        getattr(e, "filename", None) in (unc_path, str(Path(unc_path)))
        for e in errors
    )
    if root_failed and not files:
        raise OSError(
            f"Cannot enumerate LAN destination {unc_path!r} "
            f"({len(errors)} access error(s)) - refusing to report an "
            f"empty inventory; destination may be offline."
        )
    if errors:
        logger.warning(
            f"LAN manifest: {len(errors)} path(s) unreadable under {unc_path} "
            f"(first: {errors[0]}) - partial inventory returned ({len(files)} files)"
        )

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
