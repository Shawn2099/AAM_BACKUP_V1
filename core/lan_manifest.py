"""LAN manifest — walk destination share, produce file inventory + diffs.

No scanner. No log parsing. No regex. Just os.walk + os.stat.
The filesystem IS the truth.
"""

import os

from loguru import logger


class WalkIncompleteError(RuntimeError):
    """One or more subdirectories could not be read during the walk.

    A snapshot built from such a walk is INCOMPLETE: directories that failed
    to read are missing from it. Callers must not treat it as authoritative —
    an incomplete post-sync snapshot in particular would make the diff report
    intact files as "removed" and prune their DB rows.
    """

    def __init__(self, unc_path: str, errors: list[str]):
        self.unc_path = unc_path
        self.errors = errors
        super().__init__(
            f"LAN destination walk incomplete at {unc_path}: {len(errors)} "
            f"director{'y was' if len(errors) == 1 else 'ies were'} unreadable; "
            f"first: {errors[0]}"
        )


def walk_lan_destination(unc_path: str) -> list[dict]:
    """Walk LAN share recursively. Returns every file with size and mtime.

    Skips files where stat() raises OSError (locked/deleted mid-walk).

    WalkIncompleteError is raised when a subdirectory could not be read
    (transient SMB session blip, quota, locked dir). Without this, os.walk
    swallows per-directory errors silently and the caller computes a diff
    from an incomplete snapshot — which prunes DB rows for files that exist
    on the NAS and were copied fine (silent manifest under-reporting until
    the next run self-heals).

    Args:
        unc_path: UNC path to walk (e.g. "\\\\192.168.10.10\\share$").

    Returns:
        [{"path": "rel\\path\\file.txt", "size": 2048, "mtime": 1717200000.0}, ...]
    """
    files: list[dict] = []
    # normcase (pure string op) instead of Path.resolve(): relpath on Windows
    # is case-insensitive, and resolve() would add a network round-trip to
    # the UNC plus a dependency on the server's on-disk casing.
    base = os.path.normcase(unc_path)
    walk_errors: list[str] = []

    def _onerror(err: OSError) -> None:
        # os.walk calls this for each directory it cannot enter/read and then
        # continues with the rest — so we record and let the walk finish, and
        # raise at the end (see WalkIncompleteError).
        target = getattr(err, "filename", "?")
        walk_errors.append(f"{target}: {err}")
        logger.warning(f"LAN walk: cannot read directory {target}: {err}")

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

    if walk_errors:
        raise WalkIncompleteError(unc_path, walk_errors)

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
