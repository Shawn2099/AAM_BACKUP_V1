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
    "G5_CANARY": ("destination",),
    "G1_SOURCE_HAS_FILES": ("source",),
    "G6_FY_ROLLOVER": ("from_fy", "to_fy"),
}


# Request lifecycle (stored in ManifestDB.approvals). Expiry is derived from
# expires_at, never a stored status.
PENDING = "PENDING"        # blocked run waiting for a human
APPROVED = "APPROVED"      # approved once on the dashboard, not used yet
DISMISSED = "DISMISSED"    # human declined; nothing will run on it
USED = "USED"              # consumed by exactly one run
SUPERSEDED = "SUPERSEDED"  # a newer block of the same leg AND FY replaced it
CLOSED = "CLOSED"          # its FY was closed by the April switch: nothing can run on it any more


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


def evaluate_request(request: dict | None, *, leg: str, blocked: list, fy: str, now_iso: str,
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
            leg=leg, guard=result.guard, fy=fy, fresh_metrics=result.metrics,
            now_iso=now_iso, tolerance_percent=tolerance_percent)
        if not decision.allowed:
            return ApprovalDecision(False, f"{result.guard}: {decision.reason}")
    return ApprovalDecision(True, "approval covers every blocked guard")


NOT_APPROVABLE_MESSAGE = (
    "This block is about the backup target itself (not mounted or unreachable, this same "
    "server, or the wrong folder), so it cannot be approved. Fix the cause (mount or restore "
    "the NAS share, check the path); the next backup clears this by itself.")


def entry_approvable(entry: dict) -> bool:
    """May a person approve this blocked guard? Entries stored before the flag existed are
    judged by the fault code, else by the measured G5 numbers; anything else stays approvable."""
    from core.guards import code_approvable
    if "approvable" in entry:
        return bool(entry["approvable"])
    if entry.get("fault_code"):
        return code_approvable(entry["fault_code"])
    if entry.get("guard") == "G5_CANARY":
        m = entry.get("metrics") or {}
        return not (m.get("canary_present") is None or
                    (m.get("canary_present") is False and m.get("destination_has_files")))
    return True


def request_approvable(guards: list[dict] | None) -> bool:
    """A request can be approved only when EVERY blocked guard in it can."""
    return all(entry_approvable(g) for g in guards or [])


def phrase_matches(expected: str, typed: str) -> bool:
    if not isinstance(typed, str):
        return False
    return " ".join((typed or "").split()).upper() == expected.upper()


def expires_at(now_iso: str, expiry_hours: float) -> str:
    return pendulum.parse(now_iso).add(seconds=int(expiry_hours * 3600)).isoformat()


def evaluate_approval(approval: dict | None, *, leg: str, guard: str, fy: str,
                      fresh_metrics: dict, now_iso: str,
                      tolerance_percent: float) -> ApprovalDecision:
    """May this run proceed past `guard` on the strength of `approval`?

    `fy` is the FY folder of THIS run (no default): an approval is for ONE folder, so the approval of
    FY26-27's mass delete can never let FY27-28 delete (plan R-1)."""
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
    if not approval.get("fy") or approval.get("fy") != fy:
        return ApprovalDecision(
            False, f"approval is for the {approval.get('fy') or 'unknown'} folder, not {fy}")
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
