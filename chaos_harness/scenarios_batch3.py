"""Batch-3 scenarios B3-1..B3-4 — metadata only.

Genuinely uncovered items from the reconciliation that need harness
support. P19/P20/P21 stay deferred (reviews pending), P15 declined
(low value), H5 stays blocked. Fewer than five by design — no invented
work.
"""

from chaos_harness.scenarios import Scenario

B3_1_CLOUD_MATRIX = Scenario(
    sid="B3-1",
    title="Cloud audit divergence matrix (P14)",
    family="Cloud audit classification across divergence classes",
    real_entrypoints=(
        "flow.integrity_audit_flow(mode='cloud')",
        "core.integrity.audit_cloud",
        "flow.backup(mode='cloud') [reconvergence leg]",
        "ManifestDB.record_audit",
    ),
    fault_tool="chaos_harness.faults.cloud_plant/cloud_remove "
               "(parameterized T3/P10 procedure via rclone + chaos conf)",
    preconditions=("chaos bucket converged to source truth", "hashes recorded",
                   "file_entries baseline", "lock absent"),
    expected=("missing/size-changed → VERIFICATION_FAILED or gate failure "
              "with truthful labels; same-size-diff → daily blind, weekly "
              "audit detects; restore + re-sync → VERIFIED"),
    evidence=("plant records", "bucket censuses + hashes", "audit rows",
              "run_history rows", "Prefect states"),
    cleanup=("planted objects removed", "bucket reconverged byte-exact",
             "successor VERIFIED"),
    harness_only=("plant/remove", "census capture", "assertions"),
)

B3_2_XBACKEND = Scenario(
    sid="B3-2",
    title="Cross-backend concurrency serialization (P22)",
    family="Slot serialization under near-simultaneous triggers",
    real_entrypoints=(
        "flow.backup(mode='lan') + flow.backup(mode='cloud')",
        "flow._backup_slot (concurrency limit)",
        "ManifestDB lock + run rows",
    ),
    fault_tool="C:\\ChaosTest\\evidence\\POST_REMEDIATION\\tools\\ph22_xbackend.py "
               "(existing; reused directly)",
    preconditions=("both legs converged", "slot limit known", "lock absent"),
    expected=("no simultaneous conflicting backup; handoff truthful; "
              "no orphans; manifests truthful under trigger pressure"),
    evidence=("trigger log", "concurrency-limit census (pfx.py conc)",
              "run_history rows", "lock lineage", "Prefect states"),
    cleanup=("both legs reconverged", "no stale lock/slots"),
    harness_only=("trigger sequencing", "census capture", "assertions"),
)

B3_3_PRESSURE = Scenario(
    sid="B3-3",
    title="Bounded pressure: retries, faults, no leaks (P23)",
    family="Resource boundedness under fault pressure",
    real_entrypoints=(
        "flow.backup (lan + cloud legs)",
        "config-driven retry profiles",
        "flow.integrity_audit_flow (audit pressure leg)",
    ),
    fault_tool="Batch-1/2 primitives reused (killrob/killrcl/cfgfault "
               "class); no new fault code",
    preconditions=("baselines: processes, lock/slot, manifest size",
                   "config retry bounds recorded"),
    expected=("all retries bounded per config; no lock/slot leak; no "
              "permanent RUNNING; truthful manifest; recovery intact"),
    evidence=("retry timing logs", "process censuses", "manifest growth "
              "notes", "run_history terminal states"),
    cleanup=("rig reconverged; pressure artifacts removed"),
    harness_only=("loop sequencing via runner", "census capture", "assertions"),
)

B3_4_ENDURANCE = Scenario(
    sid="B3-4",
    title="Endurance: consecutive normal runs (P24)",
    family="Stability over repeated success",
    real_entrypoints=(
        "flow.backup(mode='all') via chaos deployments",
        "flow.integrity_audit_flow (periodic audit leg)",
        "ManifestDB growth + maintenance paths",
    ),
    fault_tool="none (repeated clean triggers via pfx.py)",
    preconditions=("fully converged rig", "retention bounds recorded"),
    expected=("30+ runs: no lock/slot/process leaks; no memory trend; "
              "manifest/log growth bounded by retention; audit stable"),
    evidence=("per-run census log", "growth series", "service health notes"),
    cleanup=("retention steady-state confirmed"),
    harness_only=("loop sequencing via runner", "census capture", "assertions"),
)

ALL3 = (B3_1_CLOUD_MATRIX, B3_2_XBACKEND, B3_3_PRESSURE, B3_4_ENDURANCE)


def get3(sid: str) -> Scenario:
    for s in ALL3:
        if s.sid == sid:
            return s
    raise KeyError(f"unknown batch-3 scenario {sid!r}")
