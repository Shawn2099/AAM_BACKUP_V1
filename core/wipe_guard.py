"""Wipe-risk guard — pre-sync sanity check that the source still has data.

A mirror sync (/MIR, rclone sync) from an emptied or mis-pointed source DELETES
the destination and is recorded as a *successful* no-change/complete run
(AUDIT-012, live-verified on the Windows server: empty source -> exit 2 =
LAN_COMPLETE; source containing only system directories passes the health gate
and syncs as "empty" too).

This guard runs between preflight and sync in both pipelines. If the last
successful run for this mode tracked a substantial number of files and the
source now holds dramatically fewer, the sync is BLOCKED with a distinct,
loud status instead of wiping the destination.

Design notes
------------
- The baseline is what the DESTINATION currently holds — for cloud, the live
  GCS object count (rclone size — instant, GCS pre-computes it); for LAN, the
  pre-sync destination snapshot. Deliberately NOT the DB manifest: the manifest
  is FY-agnostic and would keep holding the old FY's count after a rollover,
  blocking the new FY's legitimate start for months.
- Below `min_files` on the destination the guard stays silent (an
  empty/tiny destination has nothing to protect — this is what lets a fresh
  post-rollover FY prefix start without a false positive).
- A post-rollover source that is *legitimately* empty against a POPULATED
  destination still BLOCKS (mirroring emptiness would wipe the data): the run
  is recorded as `*_WIPE_RISK_BLOCKED` with an explicit, human-readable
  reason. The presence of the rollover seed marker (`.AAM_SOURCE_SEEDED`)
  makes the alert say "new FY, no data yet" instead of "source looks deleted".
- If the source walk itself is incomplete (errors), the guard BLOCKS: an
  untrustworthy count is never a green light for a mirror.
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

SEED_MARKER = ".AAM_SOURCE_SEEDED"


class WipeRiskError(Exception):
    """Raised when the guard refuses to let a mirror sync proceed."""

    def __init__(self, reason: str, current_files: int, previous_files: int,
                 seed_present: bool, walk_errors: int = 0):
        self.reason = reason
        self.current_files = current_files
        self.previous_files = previous_files
        self.seed_present = seed_present
        self.walk_errors = walk_errors
        super().__init__(reason)


def count_source_files(source_path: str) -> tuple[int, int]:
    """Count regular files under *source_path*, counting walk errors.

    Returns (file_count, error_count).

    The count is meant to answer "how much data would the mirror transfer?",
    so it counts exactly what the mirrors transfer (R4):
      - MIRROR_EXCLUDED_DIRS (core/lan_sync.py: System Volume Information,
        $RECYCLE.BIN) are pruned from the walk — the LAN mirror /XD-excludes
        them and the cloud mirror --exclude's them. Without this, a source
        holding ONLY a large shadow-copy/recycle-bin directory could pass
        the ratio check while the mirror still wipes the destination
        (the SVI bypass of AUDIT-012, narrowed but not closed).
      - the FY-rollover seed marker file is skipped (both syncs exclude it).
      - directory symlinks/junctions are not followed (os.walk default),
        matching robocopy /XJ and rclone's symlink handling.
    """
    from core.lan_sync import MIRROR_EXCLUDED_DIRS

    count = 0
    errors = 0

    def _onerror(err: OSError) -> None:
        nonlocal errors
        errors += 1

    for _root, dirs, files in os.walk(source_path, onerror=_onerror):
        dirs[:] = [d for d in dirs if d not in MIRROR_EXCLUDED_DIRS]
        count += len([f for f in files if f != SEED_MARKER])
    return count, errors


def source_has_seed_marker(source_path: str) -> bool:
    try:
        return (Path(source_path) / SEED_MARKER).is_file()
    except OSError:
        return False


def check_wipe_risk(
    source_path: str,
    previous_files: int,
    min_files: int = 100,
    min_ratio: float = 0.5,
) -> None:
    """Block the sync if the source has collapsed relative to the destination.

    Args:
        source_path: the configured source drive path.
        previous_files: what the DESTINATION currently holds — for cloud, the
            live GCS object count; for LAN, the pre-sync destination snapshot.
            (Deliberately not the manifest: the manifest is FY-agnostic and
            would block a new FY's legitimate start after a rollover.)
        min_files: guard activates only when the destination count is >= this
            (an empty/tiny destination has nothing to protect).
        min_ratio: block when current_files < previous_files * min_ratio
            (or 0 current files with a non-trivial baseline).

    Raises:
        WipeRiskError with a human-readable reason when the sync must not run.
    """
    current_files, walk_errors = count_source_files(source_path)
    seed_present = source_has_seed_marker(source_path)

    if walk_errors:
        raise WipeRiskError(
            f"Source walk reported {walk_errors} error(s) — file count is not "
            f"trustworthy (count={current_files}, destination="
            f"{previous_files}). Refusing to run a mirror sync on an "
            f"unverified source.",
            current_files, previous_files, seed_present, walk_errors,
        )

    if previous_files < min_files:
        return  # no meaningful baseline — nothing to protect

    threshold = max(1, int(previous_files * min_ratio))
    if current_files < threshold:
        if current_files == 0 and seed_present:
            reason = (
                f"Source {source_path} is empty but carries the FY-rollover "
                f"seed marker — this looks like a brand-new fiscal-year folder "
                f"with no data yet. Blocking the mirror sync (it would empty "
                f"the destination). Wait for source data to land; the guard "
                f"re-checks every run."
            )
        elif current_files == 0:
            reason = (
                f"Source {source_path} is EMPTY while the destination currently "
                f"holds {previous_files} files. Blocking the mirror sync — "
                f"running it would DELETE the destination. Check the source "
                f"drive (unmounted? wrong path? data moved?)."
            )
        else:
            reason = (
                f"Source {source_path} holds {current_files} file(s) but the "
                f"destination currently holds {previous_files} (guard "
                f"threshold {threshold}). Blocking the mirror sync — a "
                f"collapse of this size usually means the source is mis-pointed "
                f"or was emptied."
            )
        logger.error(f"Wipe-risk guard BLOCKED sync: {reason}")
        raise WipeRiskError(reason, current_files, previous_files, seed_present)

    logger.info(
        f"Wipe-risk guard OK: source has {current_files} file(s) "
        f"(last known good: {previous_files})"
    )
