"""Single source of truth for run / audit status vocabulary (plan §5.3).

Every consumer that needs to know whether a status is a success, how to
label it, or how to describe it — the flow terminal policy, ManifestDB
queries, reports, the dashboard API and the dashboard JS — goes through
this module. Re-deriving categories from string suffixes elsewhere is how
SUSPECT ended up shown as "CLOUD_SUSP" on the dashboard while the report
counted it as a failure.
"""

from enum import StrEnum


class RunStatus(StrEnum):
    """Terminal (and in-progress) states of one backup run (one leg)."""

    CLOUD_COMPLETE = "CLOUD_COMPLETE"
    CLOUD_NO_CHANGES_COMPLETE = "CLOUD_NO_CHANGES_COMPLETE"
    CLOUD_PARTIAL = "CLOUD_PARTIAL"
    CLOUD_SUSPECT = "CLOUD_SUSPECT"
    CLOUD_FAILED = "CLOUD_FAILED"
    CLOUD_VERIFY_FAILED = "CLOUD_VERIFY_FAILED"
    CLOUD_SKIPPED = "CLOUD_SKIPPED"
    CLOUD_BLOCKED = "CLOUD_BLOCKED"
    CLOUD_SLOT_TIMEOUT = "CLOUD_SLOT_TIMEOUT"
    CLOUD_ABANDONED = "CLOUD_ABANDONED"
    CLOUD_MISSED = "CLOUD_MISSED"        # started so late that its run budget was already used up
    CLOUD_RUNNING = "CLOUD_RUNNING"

    LAN_COMPLETE = "LAN_COMPLETE"
    LAN_PARTIAL = "LAN_PARTIAL"
    LAN_SUSPECT = "LAN_SUSPECT"
    LAN_FAILED = "LAN_FAILED"
    LAN_SKIPPED = "LAN_SKIPPED"
    LAN_BLOCKED = "LAN_BLOCKED"
    LAN_SLOT_TIMEOUT = "LAN_SLOT_TIMEOUT"
    LAN_ABANDONED = "LAN_ABANDONED"
    LAN_MISSED = "LAN_MISSED"
    LAN_RUNNING = "LAN_RUNNING"

    ROLLOVER_COMPLETE = "ROLLOVER_COMPLETE"
    ROLLOVER_BLOCKED = "ROLLOVER_BLOCKED"
    ROLLOVER_FAILED = "ROLLOVER_FAILED"      # a step before the switch failed; resumed next morning
    ROLLOVER_RUNNING = "ROLLOVER_RUNNING"
    ROLLOVER_ABANDONED = "ROLLOVER_ABANDONED"   # the process died mid-rollover; resumed next morning


class AuditStatus(StrEnum):
    """Integrity-audit verdicts (separate fact from the backup result)."""

    VERIFIED = "VERIFIED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    # Every file that was already backed up matches; some newer files wait for the next backup
    # (core/audit_pending). Not a success verdict (it never reads "every file matches") and not
    # a failure (no email, no red banner).
    PENDING_BACKUP = "PENDING_BACKUP"
    # The audit could not reach a verdict (network/listing/process error).
    # Never shown as divergence.
    AUDIT_INCOMPLETE = "AUDIT_INCOMPLETE"
    # Display-only: no audit row exists for the leg.
    NOT_VERIFIED = "NOT_VERIFIED"


# The audits that reached a verdict about the data (A1). AUDIT_INCOMPLETE is not one: it only says the
# check could not run, so it never replaces an unresolved VERIFICATION_FAILED.
AUDIT_VERDICT_STATUSES: frozenset[str] = frozenset({
    AuditStatus.VERIFIED, AuditStatus.VERIFICATION_FAILED, AuditStatus.PENDING_BACKUP,
})

# Legacy values that may exist in old run_history rows. Never produced now.
_LEGACY_SUCCESS = frozenset({"LAN_NO_CHANGES_COMPLETE", "SUCCESS"})
_LEGACY_NO_CHANGES = frozenset({"LAN_NO_CHANGES_COMPLETE"})

SUCCESS_STATUSES: frozenset[str] = frozenset({
    RunStatus.CLOUD_COMPLETE,
    RunStatus.CLOUD_NO_CHANGES_COMPLETE,
    RunStatus.LAN_COMPLETE,
    RunStatus.ROLLOVER_COMPLETE,
}) | _LEGACY_SUCCESS

NO_CHANGES_STATUSES: frozenset[str] = frozenset({
    RunStatus.CLOUD_NO_CHANGES_COMPLETE,
}) | _LEGACY_NO_CHANGES

PARTIAL_STATUSES: frozenset[str] = frozenset({
    RunStatus.CLOUD_PARTIAL, RunStatus.LAN_PARTIAL,
})

BLOCKED_STATUSES: frozenset[str] = frozenset({
    RunStatus.CLOUD_BLOCKED, RunStatus.LAN_BLOCKED, RunStatus.ROLLOVER_BLOCKED,
})

RUNNING_STATUSES: frozenset[str] = frozenset({
    RunStatus.CLOUD_RUNNING, RunStatus.LAN_RUNNING, RunStatus.ROLLOVER_RUNNING,
})

SKIPPED_STATUSES: frozenset[str] = frozenset({
    RunStatus.CLOUD_SKIPPED, RunStatus.LAN_SKIPPED,
})

FAILED_STATUSES: frozenset[str] = frozenset({
    RunStatus.CLOUD_SUSPECT, RunStatus.CLOUD_FAILED, RunStatus.CLOUD_VERIFY_FAILED,
    RunStatus.CLOUD_SLOT_TIMEOUT, RunStatus.CLOUD_ABANDONED, RunStatus.CLOUD_MISSED,
    RunStatus.LAN_SUSPECT, RunStatus.LAN_FAILED,
    RunStatus.LAN_SLOT_TIMEOUT, RunStatus.LAN_ABANDONED, RunStatus.LAN_MISSED,
    RunStatus.ROLLOVER_FAILED, RunStatus.ROLLOVER_ABANDONED,
})

CLOUD_SUCCESS_STATUSES = frozenset(s for s in SUCCESS_STATUSES if s.startswith("CLOUD_"))
LAN_SUCCESS_STATUSES = frozenset(s for s in SUCCESS_STATUSES if s.startswith("LAN_"))

# Category names double as the dashboard CSS classes.
CATEGORY_SUCCESS = "success"
CATEGORY_NO_CHANGES = "no-changes"
CATEGORY_PARTIAL = "partial"
CATEGORY_FAILED = "failed"
CATEGORY_BLOCKED = "blocked"
CATEGORY_RUNNING = "running"
CATEGORY_SKIPPED = "skipped"
CATEGORY_UNKNOWN = "unknown"

_LABELS: dict[str, str] = {
    RunStatus.CLOUD_COMPLETE: "Completed",
    RunStatus.LAN_COMPLETE: "Completed",
    RunStatus.CLOUD_NO_CHANGES_COMPLETE: "No Changes",
    "LAN_NO_CHANGES_COMPLETE": "No Changes",
    "SUCCESS": "Completed",
    RunStatus.CLOUD_PARTIAL: "Partial",
    RunStatus.LAN_PARTIAL: "Partial",
    RunStatus.CLOUD_SUSPECT: "Suspect",
    RunStatus.LAN_SUSPECT: "Suspect",
    RunStatus.CLOUD_FAILED: "Failed",
    RunStatus.LAN_FAILED: "Failed",
    RunStatus.CLOUD_VERIFY_FAILED: "Size check failed",
    RunStatus.CLOUD_SKIPPED: "Skipped",
    RunStatus.LAN_SKIPPED: "Skipped",
    RunStatus.CLOUD_BLOCKED: "Blocked - approval needed",
    RunStatus.LAN_BLOCKED: "Blocked - approval needed",
    RunStatus.CLOUD_SLOT_TIMEOUT: "Could not start",
    RunStatus.LAN_SLOT_TIMEOUT: "Could not start",
    RunStatus.CLOUD_ABANDONED: "Abandoned",
    RunStatus.LAN_ABANDONED: "Abandoned",
    RunStatus.CLOUD_MISSED: "Missed",
    RunStatus.LAN_MISSED: "Missed",
    RunStatus.CLOUD_RUNNING: "Running",
    RunStatus.LAN_RUNNING: "Running",
    RunStatus.ROLLOVER_COMPLETE: "FY rolled over",
    RunStatus.ROLLOVER_BLOCKED: "Blocked - approval needed",
    RunStatus.ROLLOVER_FAILED: "FY rollover not finished",
    RunStatus.ROLLOVER_RUNNING: "FY rollover running",
    RunStatus.ROLLOVER_ABANDONED: "Abandoned",
}


def _norm(status) -> str:
    return "" if status is None else str(status)


def is_success(status) -> bool:
    return _norm(status) in SUCCESS_STATUSES


def category(status) -> str:
    """Return the display/reporting category (also the CSS class)."""
    s = _norm(status)
    if s in NO_CHANGES_STATUSES:
        return CATEGORY_NO_CHANGES
    if s in SUCCESS_STATUSES:
        return CATEGORY_SUCCESS
    if s in PARTIAL_STATUSES:
        return CATEGORY_PARTIAL
    if s in BLOCKED_STATUSES:
        return CATEGORY_BLOCKED
    if s in RUNNING_STATUSES:
        return CATEGORY_RUNNING
    if s in SKIPPED_STATUSES:
        return CATEGORY_SKIPPED
    if s in FAILED_STATUSES:
        return CATEGORY_FAILED
    return CATEGORY_UNKNOWN


def label(status) -> str:
    """Short human label. Unknown statuses are shown in full, never cut."""
    s = _norm(status)
    return _LABELS.get(s, s or "Unknown")


def describe(status, files_copied: int | None = 0, files_failed: int | None = 0) -> str:
    """One-sentence, truthful description for the dashboard card."""
    s = _norm(status)
    files = int(files_copied or 0)
    failed = int(files_failed or 0)
    cat = category(s)
    if s == RunStatus.ROLLOVER_FAILED:
        return ("FY rollover could not be finished - your data is safe and it is tried "
                "again every morning, see issue below")
    if s == RunStatus.ROLLOVER_RUNNING:
        return "FY rollover in progress"
    if s == RunStatus.ROLLOVER_ABANDONED:
        return ("FY rollover was interrupted (service stopped) - it continues at the next "
                "morning check")
    if cat == CATEGORY_NO_CHANGES:
        return "Backup complete - no changes detected"
    if cat == CATEGORY_SUCCESS:
        # A COMPLETE status never carries failed files (decide_lan_result
        # fails those closed); if it ever does, say so instead of hiding it.
        if failed:
            return (f"Backup reported complete but {failed} file(s) failed - "
                    "treat as needing attention")
        return f"Backup complete - {files} file(s) transferred"
    if cat == CATEGORY_PARTIAL:
        return (f"Backup partial - {files} file(s) transferred, "
                f"{failed} could not be copied")
    if cat == CATEGORY_BLOCKED:
        return "Backup blocked before any change - review and approve on the dashboard"
    if cat == CATEGORY_RUNNING:
        return "Backup in progress"
    if cat == CATEGORY_SKIPPED:
        return "Backup skipped"
    if s in (RunStatus.CLOUD_SUSPECT, RunStatus.LAN_SUSPECT):
        return "Backup did not finish normally - completion unproven, see issue below"
    if s in (RunStatus.CLOUD_SLOT_TIMEOUT, RunStatus.LAN_SLOT_TIMEOUT):
        return "Backup could not start - another job held the backup slot too long"
    if s in (RunStatus.CLOUD_MISSED, RunStatus.LAN_MISSED):
        return ("Backup did not run - the server was off or busy at the scheduled time; "
                "the next run continues")
    if s in (RunStatus.CLOUD_ABANDONED, RunStatus.LAN_ABANDONED):
        return "Backup was interrupted (service stopped) - outcome unknown"
    if s == RunStatus.CLOUD_VERIFY_FAILED:
        return "Backup transferred but the post-sync size check failed - see issue below"
    if cat == CATEGORY_FAILED:
        return "Backup failed - see issue below"
    return f"Unrecognised status {s or '(none)'}"


# ── Terminal status of a pipeline (plan WP5) ──────────────────────────────
# Kinds of exception a pipeline can end with (flow._exception_kind maps them).
EXC_ERROR = "error"        # anything unexpected
EXC_BLOCKED = "blocked"    # a guard asked for a human decision
EXC_PARTIAL = "partial"    # terminal policy: the run ended with a non-success status
EXC_INTERRUPTED = "interrupted"   # the flow was cancelled / stopped (a BaseException, A2)


def decide_terminal_status(leg: str, phase: str, current: str, exc_kind: str | None) -> str:
    """The ONE place that decides a run's final status.

    leg: "cloud" | "lan"; phase: "pre" | "sync" | "verify" | "post" (where
    the pipeline was); current: the status the pipeline had set so far.
    Rule: an exception never leaves a success status behind - a verify-phase
    exception is CLOUD_VERIFY_FAILED (probe P1), a post-phase exception is
    *_FAILED (probe P2); the sync's own verdict is preserved by the caller in
    extended_metrics.sync_status. An interrupted run (EXC_INTERRUPTED) is decided
    like an unexpected error in the same phase: the work was cut short, so it is
    never SKIPPED (data may be half copied) and never a success.
    """
    prefix = leg.upper()
    failed = f"{prefix}_FAILED"
    if exc_kind is None:
        return current
    if exc_kind == EXC_BLOCKED:
        return f"{prefix}_BLOCKED"
    unsettled = is_success(current) or category(current) in (
        CATEGORY_SKIPPED, CATEGORY_RUNNING, CATEGORY_UNKNOWN)
    if exc_kind == EXC_PARTIAL:
        return failed if unsettled else current
    if phase == "sync":
        return current if current in (failed, f"{prefix}_SUSPECT") else failed
    if phase == "verify":
        return "CLOUD_VERIFY_FAILED" if leg == "cloud" else failed
    if phase == "post":
        return failed if unsettled else current
    return failed
