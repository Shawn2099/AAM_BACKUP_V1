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

MOUNT_MARKER = ".AAM_TARGET_MOUNTED"

# Names G1 never counts as backup content (any depth): OS-managed entries
# that exist even on an empty NTFS volume, plus the destination mount marker.
SOURCE_IGNORE_NAMES = frozenset({
    "system volume information", "$recycle.bin", "recycler", "recycled",
    "desktop.ini", "thumbs.db", MOUNT_MARKER.lower(),
})


def count_backup_files(relative_paths) -> int:
    """Destination files that are backup content (G3 denominator).

    The mount marker is excluded at any depth, exactly like robocopy's
    `/XF .AAM_TARGET_MOUNTED`, which never copies or deletes it.
    """
    marker = MOUNT_MARKER.lower()
    return sum(1 for p in relative_paths
               if p.replace("\\", "/").rsplit("/", 1)[-1].lower() != marker)


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
                   max_delete_files: int, max_delete_percent: float,
                   already_deleted: int = 0) -> GuardResult:
    """already_deleted (XR-1/XR-2): files an earlier attempt of THIS run
    deleted. The limits apply to the whole run, so they are added to the
    planned deletions and to the destination size the run started from."""
    name = "G3_MASS_DELETE"
    extra = {"already_deleted": already_deleted} if already_deleted else {}
    if planned_deletes is None or destination_files is None:
        return _block(name, "The number of files this backup would delete could not be "
                            "measured - refusing to mirror blindly.",
                      planned_deletes=planned_deletes, destination_files=destination_files,
                      **extra)
    planned_deletes += already_deleted
    destination_files += already_deleted
    pct = 100.0 * planned_deletes / destination_files if destination_files > 0 else 0.0
    metrics = dict(planned_deletes=planned_deletes, destination_files=destination_files,
                   delete_percent=round(pct, 2), limit_files=max_delete_files,
                   limit_percent=max_delete_percent, **extra)
    earlier = (f" ({already_deleted:,} of them already deleted by an earlier attempt of "
               "this run)" if already_deleted else "")
    if planned_deletes > max_delete_files or pct > max_delete_percent:
        return _block(
            name,
            f"This backup would delete {planned_deletes:,} of {destination_files:,} "
            f"backup files{earlier} ({pct:.1f}%; limits {max_delete_files:,} files / "
            f"{max_delete_percent}%).",
            **metrics,
        )
    return _pass(name, f"{planned_deletes:,} planned deletion(s) within limits{earlier}",
                 **metrics)


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


# ── G6: FY rollover only in the normal case (owner DEC-4) ─────────────────

def g6_fy_rollover(from_fy: str, to_fy: str, kind: str, final_status: dict) -> GuardResult:
    """Automatic rollover only for exactly the next FY with every enabled
    leg's final backup of the closing FY complete; anything else asks a
    human. (A clock that is BEHIND is never a rollover - the caller refuses
    it before this guard: fix the clock, there is nothing to approve.)"""
    from core.fy_rollover import FORWARD
    from core.statuses import is_success

    name = "G6_FY_ROLLOVER"
    metrics = dict(from_fy=from_fy, to_fy=to_fy, kind=kind, final_status=dict(final_status))
    if kind != FORWARD:
        return _block(
            name,
            f"The computer date says the current FY is {to_fy}, but the backups are set up "
            f"for {from_fy} - more than one FY later. Check the date and the FY folders; "
            f"approve only if moving straight to {to_fy} is intended.",
            **metrics,
        )
    failed = sorted(leg for leg, status in final_status.items() if not is_success(status))
    if failed:
        return _block(
            name,
            f"The final backup of {from_fy} did not complete for: {', '.join(failed)} "
            f"({', '.join(f'{leg}={final_status[leg]}' for leg in failed)}). Rolling over now "
            f"would leave {from_fy}'s last changes on that leg unprotected. Fix it and the "
            "daily check retries, or approve to roll over anyway.",
            **metrics,
        )
    return _pass(name, f"{from_fy} -> {to_fy}: next FY and every final backup complete", **metrics)


# ── Enforcement (shared by both legs) ─────────────────────────────────────

class GuardBlocked(RuntimeError):
    """One or more guards blocked the run before any transfer (plan §6.1).

    `record` is the run's guard record (JSON-safe) stored in the run row's
    extended_metrics["guards"]; the approval workflow reads it from there.
    """

    def __init__(self, leg: str, record: dict):
        self.leg = leg
        self.record = record
        approved = set(record.get("approved_guards", []))
        reasons = [r["reason"] for r in record.get("results", [])
                   if r.get("verdict") == BLOCK and r.get("guard") not in approved]
        message = f"{leg.upper()} backup BLOCKED - human approval needed: " + " | ".join(reasons)
        if record.get("approval_rejected"):
            message += (f" | approval {record.get('approval_id')} not applied: "
                        f"{record['approval_rejected']}")
        super().__init__(message)


def enforce(leg: str, results: list[GuardResult],
            approved_guards: frozenset[str] = frozenset(), **record) -> dict:
    """Log every guard decision (rule §2.6) and return the run's guard record.

    Raises GuardBlocked when any guard blocked that is not in
    `approved_guards` (a valid human approval for THIS run, already checked
    by the caller); all results are kept so the operator sees every reason.
    """
    overridden = []
    for r in results:
        if r.passed:
            logger.info(f"Guard {r.guard} PASS ({leg}): {r.reason} {r.metrics}")
        elif r.guard in approved_guards:
            overridden.append(r.guard)
            logger.warning(f"Guard {r.guard} BLOCK overridden by human approval ({leg}): "
                           f"{r.reason} {r.metrics}")
        else:
            logger.error(f"Guard {r.guard} BLOCK ({leg}): {r.reason} {r.metrics}")
    record = {**record, "results": [asdict(r) for r in results]}
    if overridden:
        record["approved_guards"] = overridden
    if any(not r.passed and r.guard not in approved_guards for r in results):
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


def remaining_delete_allowance(destination_files: int, already_deleted: int,
                               max_delete_files: int, max_delete_percent: float) -> int:
    """XR-1: the --max-delete for a retry - what is left of the run's allowance.

    `destination_files` is the size now; the allowance is computed on the
    size the run started from, minus what this run already deleted, so all
    attempts together never delete more than G3 passes once.
    """
    allowance = max_delete_allowance(destination_files + already_deleted,
                                     max_delete_files, max_delete_percent)
    return max(0, allowance - already_deleted)


def backstop_blocked(leg: str, record: dict, deleted: int) -> GuardBlocked:
    """XR-1: the tool's own delete cap stopped the sync - always a human decision.

    `record` is the guard re-measure taken after the stop (deletions of this
    run included). Its G3 numbers are kept for the approval card, but G3 is
    BLOCK whatever the re-measure said: more files were to be deleted than
    the guard measured before the sync, so the measurement itself is no
    longer trusted. A G3 approval the run carried does not cover this.
    """
    results = [dict(r) for r in record.get("results", [])]
    g3 = next((r for r in results if r.get("guard") == "G3_MASS_DELETE"), None)
    metrics = {**((g3 or {}).get("metrics") or {}), "already_deleted": deleted,
               "backstop_tripped": True}
    # The approval phrase names the deletions; at least these are known.
    metrics["planned_deletes"] = max(metrics.get("planned_deletes") or 0, deleted)
    reason = (f"The {leg} sync stopped at its delete limit after deleting {deleted:,} "
              "file(s): it wanted to delete more than the safety check measured just "
              "before the sync. Nothing more is deleted until a person approves.")
    blocked = asdict(_block("G3_MASS_DELETE", reason, **metrics))
    results = [r for r in results if r.get("guard") != "G3_MASS_DELETE"] + [blocked]
    approved = [a for a in record.get("approved_guards", []) if a != "G3_MASS_DELETE"]
    new_record = {**record, "results": results, "approved_guards": approved}
    if not approved:
        new_record.pop("approved_guards")
    return GuardBlocked(leg, new_record)


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


def require_source_files(source: str) -> None:
    """Raise ValueError (G1 reason) if `source` lists fine but holds no real file.

    Last line of defence used by the mirror-command builders (robocopy,
    rclone): even a caller that bypasses the pipeline guards can never
    mirror an empty or folders-only source. A source that cannot be listed
    at all is left to the tool, which then deletes nothing: verified
    2026-10-14 — robocopy /MIR exits 16 ("ERROR 2 Accessing Source
    Directory"), rclone sync exits 3 ("not deleting files as there were IO
    errors"); the pre-backup health check fails it earlier anyway.
    """
    try:
        first = first_regular_file(source, SOURCE_IGNORE_NAMES)
    except OSError:
        return
    result = g1_source_has_files(first, source)
    if not result.passed:
        raise ValueError(result.reason)


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
