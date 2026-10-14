"""Data-safety guards (plan §6.3, WP3) — pure decisions, no side effects.

Each guard takes MEASURED numbers plus configured limits and returns a
GuardResult. A BLOCK stops the run before any destructive step and asks a
human (approval workflow, plan §6). Rules shared by every guard:

* Unknown is never safe: a metric that could not be measured BLOCKS.
* The reason is written for the operator, with the actual numbers.
* No I/O here except the explicit measurement helpers at the bottom
  (`first_regular_file`, `local_addresses`, `resolve_host`) and the
  decision log lines in `enforce`, so the decisions are exhaustively
  unit/property testable.
"""

from __future__ import annotations

import ipaddress
import math
import os
import socket
from dataclasses import asdict, dataclass, field
from fractions import Fraction

from loguru import logger

PASS = "PASS"
BLOCK = "BLOCK"

# Names G1 never counts as backup content (any depth): OS-managed entries
# that exist even on an empty NTFS volume, plus the destination mount marker.
SOURCE_IGNORE_NAMES = frozenset({
    "system volume information", "$recycle.bin", "recycler", "recycled",
    "desktop.ini", "thumbs.db", ".aam_target_mounted",
})


@dataclass(frozen=True)
class GuardResult:
    guard: str
    verdict: str          # PASS | BLOCK
    reason: str
    metrics: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == PASS


def _pass(guard: str, reason: str, **metrics) -> GuardResult:
    return GuardResult(guard, PASS, reason, metrics)


def _block(guard: str, reason: str, **metrics) -> GuardResult:
    return GuardResult(guard, BLOCK, reason, metrics)


# ── G1: source must contain at least one real file ─────────────────────────

def g1_source_has_files(first_file: str | None, source: str) -> GuardResult:
    """A source with no regular files (empty, or only empty folders) would
    make /MIR and rclone sync delete every destination copy (finding C-1)."""
    if first_file:
        return _pass("G1_SOURCE_HAS_FILES", f"source contains files (e.g. {first_file})",
                     source=source)
    return _block(
        "G1_SOURCE_HAS_FILES",
        f"Source {source} contains no files (it is empty or holds only empty "
        "folders). Mirroring it would delete every backup copy. This usually "
        "means the drive is not mounted or the path is wrong.",
        source=source,
    )


# ── G2: source must not have shrunk drastically ────────────────────────────

def g2_source_shrink(current_files: int | None, baseline_files: int | None,
                     max_shrink_percent: float) -> GuardResult:
    name = "G2_SOURCE_SHRINK"
    if current_files is None:
        return _block(name, "Source file count could not be measured - cannot prove "
                            "the source is complete.", current_files=None,
                      baseline_files=baseline_files)
    if baseline_files is None or baseline_files <= 0:
        return _pass(name, "no previous successful run to compare with (first run)",
                     current_files=current_files, baseline_files=baseline_files)
    drop = baseline_files - current_files
    pct = 100.0 * drop / baseline_files if drop > 0 else 0.0
    metrics = dict(current_files=current_files, baseline_files=baseline_files,
                   drop_files=max(drop, 0), drop_percent=round(pct, 2),
                   limit_percent=max_shrink_percent)
    if pct > max_shrink_percent:
        return _block(
            name,
            f"Source has {current_files:,} files but had {baseline_files:,} at the last "
            f"successful backup ({pct:.1f}% fewer; limit {max_shrink_percent}%). "
            "Files may have been deleted or the wrong folder is mounted.",
            **metrics,
        )
    return _pass(name, f"source file count change within limit ({pct:.1f}% fewer)", **metrics)


# ── G3: planned deletions must stay within limits ──────────────────────────

def g3_mass_delete(planned_deletes: int | None, destination_files: int | None,
                   max_delete_files: int, max_delete_percent: float) -> GuardResult:
    name = "G3_MASS_DELETE"
    if planned_deletes is None or destination_files is None:
        return _block(name, "The number of files this backup would delete could not be "
                            "measured - refusing to mirror blindly.",
                      planned_deletes=planned_deletes, destination_files=destination_files)
    pct = 100.0 * planned_deletes / destination_files if destination_files > 0 else 0.0
    metrics = dict(planned_deletes=planned_deletes, destination_files=destination_files,
                   delete_percent=round(pct, 2), limit_files=max_delete_files,
                   limit_percent=max_delete_percent)
    if planned_deletes > max_delete_files or pct > max_delete_percent:
        return _block(
            name,
            f"This backup would delete {planned_deletes:,} of {destination_files:,} "
            f"backup files ({pct:.1f}%; limits {max_delete_files:,} files / "
            f"{max_delete_percent}%).",
            **metrics,
        )
    return _pass(name, f"{planned_deletes:,} planned deletion(s) within limits", **metrics)


# ── G4: destination must be the expected, off-host destination ─────────────

def _norm_dest(dest: str) -> str:
    return dest.replace("/", "\\").rstrip("\\").lower()


def unc_host(dest: str) -> str | None:
    clean = dest.replace("/", "\\")
    if not clean.startswith("\\\\"):
        return None
    parts = [p for p in clean.lstrip("\\").split("\\") if p]
    return parts[0] if parts else None


def g4_destination_identity(destination: str, last_success_destination: str | None,
                            destination_ips: set[str] | None, local_ips: set[str],
                            forbid_local: bool) -> GuardResult:
    """Block when the destination silently changed since the last successful
    backup, or (when forbidden) points back at this machine — production
    logs showed the LAN copy going to \\\\127.0.0.1\\...\\CHAOS01 for a month."""
    name = "G4_DESTINATION_IDENTITY"
    metrics = dict(destination=destination, last_success_destination=last_success_destination,
                   destination_ips=sorted(destination_ips or []))
    if forbid_local:
        if destination_ips is None:
            return _block(name, f"Could not resolve the destination host of {destination} "
                                "to check it is not this machine.", **metrics)
        local_hit = destination_ips & local_ips or {
            ip for ip in destination_ips if ipaddress.ip_address(ip).is_loopback}
        if local_hit:
            return _block(
                name,
                f"Destination {destination} is on THIS machine ({', '.join(sorted(local_hit))}). "
                "A backup on the same machine does not protect against its loss.",
                **metrics,
            )
    if last_success_destination and _norm_dest(last_success_destination) != _norm_dest(destination):
        return _block(
            name,
            f"Destination changed since the last successful backup: was "
            f"{last_success_destination}, now {destination}. Confirm the change is intended.",
            **metrics,
        )
    return _pass(name, "destination unchanged and off-host", **metrics)


# ── G5: mount canary ───────────────────────────────────────────────────────

def g5_canary(canary_present: bool | None, destination_has_files: bool | None,
              destination: str) -> GuardResult:
    name = "G5_CANARY"
    metrics = dict(destination=destination, canary_present=canary_present,
                   destination_has_files=destination_has_files)
    if canary_present:
        return _pass(name, "destination mount marker present", **metrics)
    if canary_present is None:
        return _block(name, f"Could not check the mount marker on {destination}.", **metrics)
    if destination_has_files:
        return _block(
            name,
            f"Mount marker .AAM_TARGET_MOUNTED is missing on {destination}, which already "
            "holds files. Someone may have changed the share. Approve only if you have "
            "confirmed this is the correct backup location (the marker will be recreated).",
            **metrics,
        )
    return _block(
        name,
        f"Mount marker .AAM_TARGET_MOUNTED is missing on the empty destination "
        f"{destination}. Approve to initialise this location as the backup target.",
        **metrics,
    )


# ── Enforcement (shared by both legs) ─────────────────────────────────────

class GuardBlocked(RuntimeError):
    """One or more guards blocked the run before any transfer (plan §6.1).

    `record` is the run's guard record (JSON-safe) stored in the run row's
    extended_metrics["guards"]; the approval workflow reads it from there.
    """

    def __init__(self, leg: str, record: dict):
        self.leg = leg
        self.record = record
        reasons = [r["reason"] for r in record.get("results", []) if r.get("verdict") == BLOCK]
        super().__init__(f"{leg.upper()} backup BLOCKED - human approval needed: "
                         + " | ".join(reasons))


def enforce(leg: str, results: list[GuardResult], **record) -> dict:
    """Log every guard decision (rule §2.6) and return the run's guard record.

    Raises GuardBlocked when any guard blocked; all results are kept so the
    operator sees every reason at once.
    """
    for r in results:
        if r.passed:
            logger.info(f"Guard {r.guard} PASS ({leg}): {r.reason} {r.metrics}")
        else:
            logger.error(f"Guard {r.guard} BLOCK ({leg}): {r.reason} {r.metrics}")
    record = {**record, "results": [asdict(r) for r in results]}
    if any(not r.passed for r in results):
        raise GuardBlocked(leg, record)
    return record


def max_delete_allowance(destination_files: int, max_delete_files: int,
                         max_delete_percent: float) -> int:
    """Most deletions G3 still passes for this destination size.

    Used as rclone's own `--max-delete` backstop on the real sync, so a
    change between the dry-run and the sync cannot exceed the G3 limits.
    Exact arithmetic on the configured percent (never above what G3 passes).
    """
    by_percent = math.floor(Fraction(destination_files) * Fraction(max_delete_percent) / 100)
    return max(0, min(max_delete_files, by_percent))


# ── Measurement helpers (the only I/O in this module) ─────────────────────

def first_regular_file(root: str, ignore_names: frozenset[str]) -> str | None:
    """Return the first regular file under `root`, or None.

    Stops at the first hit (cheap on a populated drive, exact on an empty
    one). Never follows symlinks/junctions — robocopy /XJ excludes them, so
    a file reachable only through a junction is not backup content.
    Raises OSError if `root` itself cannot be listed (caller fails closed).
    """
    stack = [root]
    first = True
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            if first:
                raise
            continue
        first = False
        with it:
            for entry in it:
                if entry.name.lower() in ignore_names:
                    continue
                try:
                    if entry.is_symlink() or _is_junction(entry):
                        continue
                    if entry.is_file(follow_symlinks=False):
                        return entry.path
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                except OSError:
                    continue
    return None


def _is_junction(entry: os.DirEntry) -> bool:
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
    return bool(is_junction()) if callable(is_junction) else False


def local_addresses() -> set[str]:
    """Every IP address of this machine (plus loopback)."""
    import psutil

    ips = {"127.0.0.1", "::1"}
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family in (socket.AF_INET, socket.AF_INET6):
                ips.add(a.address.split("%", 1)[0])
    return ips


def resolve_host(host: str | None) -> set[str] | None:
    """Resolve a host name/IP to its addresses; None if it cannot be resolved."""
    if not host:
        return None
    try:
        return {info[4][0].split("%", 1)[0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return None
