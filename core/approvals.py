"""Human approval of a blocked run (plan §6.2, owner DEC-3) — decision logic.

Flow: a guard BLOCKs -> the run is recorded *_BLOCKED with the measured
numbers -> attention email points to the dashboard -> an operator approves
ONCE on the dashboard (typing the confirmation phrase) -> the dashboard
re-runs the deployment with `approval_id` -> the pipeline re-measures and
calls `evaluate_approval()`; it proceeds only if the approval is valid for
exactly this leg + guard and the fresh numbers are not worse than what was
approved (within `tolerance_percent`). Approvals are single-use and expire.

Never auto-approves; a scheduled run never consumes an approval.
This module holds only pure decisions; storage lives in ManifestDB.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import pendulum

# Metrics that must not get WORSE between approval and use, per guard.
# (higher value = more dangerous)
_WORSE_IF_HIGHER: dict[str, tuple[str, ...]] = {
    "G3_MASS_DELETE": ("planned_deletes", "delete_percent"),
    "G2_SOURCE_SHRINK": ("drop_files", "drop_percent"),
}
# Metrics that must be IDENTICAL (approval was for this exact situation).
_MUST_MATCH: dict[str, tuple[str, ...]] = {
    "G4_DESTINATION_IDENTITY": ("destination",),
    "G5_CANARY": ("destination", "destination_has_files"),
    "G1_SOURCE_HAS_FILES": ("source",),
    "G6_FY_ROLLOVER": ("from_fy", "to_fy"),
}


# Request lifecycle (stored in ManifestDB.approvals). Expiry is derived from
# expires_at, never a stored status.
PENDING = "PENDING"        # blocked run waiting for a human
APPROVED = "APPROVED"      # approved once on the dashboard, not used yet
DISMISSED = "DISMISSED"    # human declined; nothing will run on it
USED = "USED"              # consumed by exactly one run
SUPERSEDED = "SUPERSEDED"  # a newer block of the same leg replaced it


@dataclass(frozen=True)
class ApprovalDecision:
    allowed: bool
    reason: str


def new_approval_id() -> str:
    return secrets.token_hex(16)


# Owner decision 2026-10-19: one word confirms every approval. The card already shows the reason
# and the numbers; the person reads them, then types this. Requests stored with an older, longer
# phrase are confirmed with the same word.
APPROVE_PHRASE = "APPROVE"


def confirmation_phrase(guard: str = "", metrics: dict | None = None) -> str:
    """What the operator types to approve (the same for every guard)."""
    return APPROVE_PHRASE


def request_phrase(blocked: list[dict] | None = None) -> str:
    """Phrase for a request covering one or several blocked guards."""
    return APPROVE_PHRASE


def evaluate_request(request: dict | None, *, leg: str, blocked: list, now_iso: str,
                     tolerance_percent: float) -> ApprovalDecision:
    """May this run proceed past ALL its blocked guards on `request`?

    `blocked` are the fresh GuardResults that blocked. Every one must be
    covered by the request and pass `evaluate_approval` (same leg, approved,
    unused, unexpired, numbers not worse than approved).
    """
    if not request:
        return ApprovalDecision(False, "no approval supplied")
    approved = {g["guard"]: g.get("metrics") or {} for g in request.get("guards") or []}
    for result in blocked:
        if result.guard not in approved:
            return ApprovalDecision(
                False, f"{result.guard} blocked but is not covered by approval "
                       f"{request.get('approval_id')} - a new approval is needed")
        decision = evaluate_approval(
            {**request, "guard": result.guard, "approved_metrics": approved[result.guard]},
            leg=leg, guard=result.guard, fresh_metrics=result.metrics,
            now_iso=now_iso, tolerance_percent=tolerance_percent)
        if not decision.allowed:
            return ApprovalDecision(False, f"{result.guard}: {decision.reason}")
    return ApprovalDecision(True, "approval covers every blocked guard")


def phrase_matches(expected: str, typed: str) -> bool:
    return " ".join((typed or "").split()).upper() == expected.upper()


def expires_at(now_iso: str, expiry_hours: float) -> str:
    return pendulum.parse(now_iso).add(seconds=int(expiry_hours * 3600)).isoformat()


def evaluate_approval(approval: dict | None, *, leg: str, guard: str,
                      fresh_metrics: dict, now_iso: str,
                      tolerance_percent: float) -> ApprovalDecision:
    """May this run proceed past `guard` on the strength of `approval`?"""
    if not approval:
        return ApprovalDecision(False, "no approval supplied")
    if approval.get("used_at"):
        return ApprovalDecision(False, f"approval already used at {approval['used_at']}")
    if approval.get("dismissed_at"):
        return ApprovalDecision(False, "approval request was dismissed")
    if approval.get("leg") != leg or approval.get("guard") != guard:
        return ApprovalDecision(
            False, f"approval is for {approval.get('leg')}/{approval.get('guard')}, "
                   f"not {leg}/{guard}")
    if not approval.get("approved_at"):
        return ApprovalDecision(False, "approval request has not been approved")
    try:
        if pendulum.parse(now_iso) >= pendulum.parse(approval["expires_at"]):
            return ApprovalDecision(False, f"approval expired at {approval['expires_at']}")
    except Exception:  # noqa: BLE001 - unparsable expiry is never valid
        return ApprovalDecision(False, "approval has no valid expiry")

    approved = approval.get("approved_metrics") or {}
    for key in _MUST_MATCH.get(guard, ()):
        if str(fresh_metrics.get(key, "")).lower() != str(approved.get(key, "")).lower():
            return ApprovalDecision(
                False, f"situation changed since approval: {key} was "
                       f"{approved.get(key)!r}, now {fresh_metrics.get(key)!r}")
    factor = 1.0 + max(float(tolerance_percent), 0.0) / 100.0
    for key in _WORSE_IF_HIGHER.get(guard, ()):
        was, now = approved.get(key), fresh_metrics.get(key)
        if was is None or now is None:
            return ApprovalDecision(False, f"cannot compare {key} (approved={was}, now={now})")
        if float(now) > float(was) * factor + 1e-9:
            return ApprovalDecision(
                False, f"worse than approved: {key} approved {was}, now {now} "
                       f"(tolerance {tolerance_percent}%) - a new approval is needed")
    return ApprovalDecision(True, "approval valid for this run")


def due_reminder_rung(
    created_at_iso: str,
    now_iso: str,
    reminder_days: list[int] | tuple[int, ...] = (1, 2, 4, 7),
    sent_rungs: set[int] = frozenset(),
) -> int | None:
    """Return the reminder rung day due now, or None on day 0 / quiet days / already-sent rungs."""
    try:
        age_days = max(0, int((pendulum.parse(now_iso) - pendulum.parse(created_at_iso)).total_seconds() // 86400))
    except Exception:  # noqa: BLE001
        return None
    if age_days == 0:
        return None
    rungs = sorted({int(d) for d in (reminder_days or (1, 2, 4, 7)) if int(d) > 0})
    last = rungs[-1]
    if age_days in rungs and age_days not in sent_rungs:
        return age_days
    if age_days > last and (age_days - last) % 7 == 0 and age_days not in sent_rungs:
        return age_days
    return None


def waiting_summary(req: dict, now_iso: str | None = None) -> dict:
    """Dashboard summary for a pending approval: deletions_waiting, waiting_days, waiting_label."""
    items = [r for r in (req.get("guards") or req.get("results") or ()) if isinstance(r, dict)]
    planned = 0
    has_g3 = False
    for r in items:
        if r.get("guard") == "G3_MASS_DELETE":
            has_g3 = True
            planned = int((r.get("metrics") or {}).get("planned_deletes") or 0)
            break
    if not planned and not has_g3:
        planned = int((req.get("metrics") or {}).get("planned_deletes") or 0)
    days = 0
    if req.get("created_at"):
        try:
            now_dt = pendulum.parse(now_iso) if now_iso else pendulum.now("UTC")
            days = max(0, int((now_dt - pendulum.parse(req["created_at"])).total_seconds() // 86400))
        except Exception:  # noqa: BLE001
            days = 0
    if has_g3 or planned > 0 or not items:
        label = f"{planned} deletions waiting, {days} days"
    else:
        guards_str = ", ".join(str(r.get("guard") or "guard") for r in items)
        label = f"{guards_str} approval waiting, {days} days"
    return {
        "deletions_waiting": planned,
        "waiting_days": days,
        "waiting_label": label,
    }
