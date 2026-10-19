"""Which audit differences are "waiting for the next backup" and which are really wrong.

The safety check (core/integrity.py) compares the LIVE source with a backup copy, so a file that
changed after the last good backup shows up as a difference although nothing is wrong. This module
sorts the raw differences into PENDING and REAL. It only reads (stat); it never changes data.

A difference is PENDING only when the source itself proves a change after `reference`, the start
of the last successful backup of that leg:

  + missing from the copy   newest of the file's modified time, creation time and its folder's
                            modified time is after the reference
  * content differs         the source file's modified time is after the reference; on the LAN, a
                            copy with the SAME modified time but other bytes is corruption (REAL)
  - only in the copy        the nearest existing source folder changed after the reference (a
                            delete or rename always updates the folder it happened in)

Unknown is never safe: no reference, a time in the future, a path that leaves the source, or a file
that cannot be read is REAL. If the waiting deletes exceed the delete limits of the backup guards
(G3), everything is REAL ("unusually many files deleted since the last backup").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath

import pendulum


@dataclass(frozen=True)
class PendingContext:
    source_root: str
    dest_root: str | None            # the LAN copy; None for the cloud (no local copy to stat)
    reference: float | None          # epoch seconds: start of the last successful backup
    now: float
    modify_window_seconds: int
    max_delete_files: int            # guards.max_delete_files
    max_delete_percent: float        # guards.max_delete_percent
    files_checked: int | None        # size of the copy, the denominator of the percent limit
    stat: object = os.stat           # injectable for tests (creation time cannot be back-dated)


@dataclass
class Classification:
    pending: list = field(default_factory=list)    # [(kind, path)] kind = new | changed | removed
    real: list = field(default_factory=list)
    tripwire: str | None = None

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    @property
    def real_count(self) -> int:
        return len(self.real)

    @property
    def real_paths(self) -> list[str]:
        return [p for _, p in self.real]

    @property
    def summary(self) -> str:
        kinds = [k for k, _ in self.pending]
        return (f"{kinds.count('new')} new, {kinds.count('changed')} changed, "
                f"{kinds.count('removed')} removed")


def reference_epoch(started_at) -> float | None:
    """Epoch seconds of a stored ISO time (run_history.started_at); None when missing or unusable."""
    if not isinstance(started_at, str) or not started_at:
        return None
    try:
        return pendulum.parse(started_at).timestamp()
    except Exception:  # noqa: BLE001 - an unreadable time simply means "unknown"
        return None


def _safe_relative(path: str) -> str | None:
    """The path as a clean relative POSIX path, or None when it could leave the source."""
    if PureWindowsPath(path).drive or PureWindowsPath(path).root or path.startswith(("/", "\\")):
        return None
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _after(t: float, ctx: PendingContext) -> bool:
    """t is after the reference and not in the future (a future time proves nothing)."""
    return ctx.reference < t <= ctx.now + ctx.modify_window_seconds


def _mtime(stat, path: str) -> float | None:
    try:
        return stat(path).st_mtime
    except OSError:
        return None


def _new_file_is_pending(rel: str, ctx: PendingContext) -> bool:
    full = os.path.join(ctx.source_root, *rel.split("/"))
    try:
        st = ctx.stat(full)
        parent = ctx.stat(os.path.dirname(full))
    except OSError:
        return False
    times = [st.st_mtime, getattr(st, "st_birthtime", 0) or 0, parent.st_mtime]
    if any(t > ctx.now + ctx.modify_window_seconds for t in times):
        return False
    return _after(max(times), ctx)


def _changed_file_is_pending(rel: str, ctx: PendingContext) -> bool:
    parts = rel.split("/")
    src_time = _mtime(ctx.stat, os.path.join(ctx.source_root, *parts))
    if src_time is None or not _after(src_time, ctx):
        return False
    if ctx.dest_root:
        dst_time = _mtime(ctx.stat, os.path.join(ctx.dest_root, *parts))
        # the same version on both sides yet other bytes: the copy is damaged
        if dst_time is not None and abs(dst_time - src_time) <= ctx.modify_window_seconds:
            return False
    return True


def _removed_file_is_pending(rel: str, ctx: PendingContext) -> bool:
    parts = rel.split("/")[:-1]
    while True:
        folder = os.path.join(ctx.source_root, *parts) if parts else ctx.source_root
        try:
            t = ctx.stat(folder).st_mtime
        except OSError:
            if not parts:
                return False
            parts = parts[:-1]          # the folder is gone too: look at its parent
            continue
        return _after(t, ctx)


def classify(added: list[str], modified: list[str], removed: list[str],
             ctx: PendingContext) -> Classification:
    """Sort the raw differences (added = missing from the copy, modified = content differs,
    removed = only in the copy) into PENDING and REAL."""
    out = Classification()
    checks = (("new", added, _new_file_is_pending),
              ("changed", modified, _changed_file_is_pending),
              ("removed", removed, _removed_file_is_pending))
    for kind, paths, is_pending in checks:
        for path in paths:
            rel = _safe_relative(path) if ctx.reference is not None else None
            try:
                waiting = rel is not None and is_pending(rel, ctx)
            except Exception:  # noqa: BLE001 - any doubt is REAL
                waiting = False
            (out.pending if waiting else out.real).append((kind, path))

    waiting_deletes = sum(1 for k, _ in out.pending if k == "removed")
    percent = (100.0 * waiting_deletes / ctx.files_checked) if ctx.files_checked else 0.0
    if waiting_deletes > ctx.max_delete_files or percent > ctx.max_delete_percent:
        out.tripwire = (f"unusually many files deleted since the last backup ({waiting_deletes:,}; "
                        f"limits {ctx.max_delete_files:,} files / {ctx.max_delete_percent}%)")
        out.real.extend(out.pending)
        out.pending.clear()
    return out
