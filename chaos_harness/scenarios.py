"""H1–H5 scenario definitions — metadata only. Each scenario names the
real application entrypoint exercised, the existing fault mechanism,
the expected business-contract outcome, required evidence, and cleanup.
No business logic lives here.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scenario:
    sid: str
    title: str
    family: str
    real_entrypoints: tuple[str, ...]
    fault_tool: str
    preconditions: tuple[str, ...]
    expected: tuple[str, ...]
    evidence: tuple[str, ...]
    cleanup: tuple[str, ...]
    harness_only: tuple[str, ...] = field(default_factory=tuple)


H1_ROBOCOPY_KILL = Scenario(
    sid="H1",
    title="Process termination / abnormal execution (robocopy kill mid-/MIR)",
    family="Process termination / abnormal execution",
    real_entrypoints=(
        "flow.backup(mode='lan') [chaos deployment backup-lan]",
        "flow._run_lan_pipeline",
        "core.lan_sync.run_lan_sync",
        "core.lan_sync.decide_lan_result",
        "ManifestDB._record_run",
    ),
    fault_tool="C:\\ChaosTest\\tools\\killrob.py (taskkill/wmi/nt/ctrl)",
    preconditions=("chaos LAN source+dest converged", "lock absent",
                   "no active worker", "production checkpoints clean"),
    expected=("LAN_SUSPECT or LAN_FAILED (never COMPLETE on truncated log)",
              "Prefect FAILED", "lock released", "forensic log retained"),
    evidence=("killer log", "agent log slice", "run_history row",
              "Prefect run states (pfx.py run)", "lock state"),
    cleanup=("orphaned robocopy log reaped by G8", "lock absent", "mirror reconverged"),
    harness_only=("killer arming", "evidence capture", "status assertions"),
)

H2_VERIFY_KILL = Scenario(
    sid="H2",
    title="Cloud verification kill (rclone check terminated mid-verify)",
    family="Cloud verification kill",
    real_entrypoints=(
        "flow.backup(mode='cloud') [chaos deployment backup-cloud]",
        "flow._run_cloud_pipeline",
        "core.cloud_sync.run_cloud_sync",
        "core.cloud_verify.verify_cloud_integrity",
        "core.cloud_verify.decide_cloud_verify_result",
        "cloud evidence gate (F-08)",
        "ManifestDB cloud record (must NOT run)",
    ),
    fault_tool="C:\\ChaosTest\\tools\\killrcl.py --check/--verb check (T3B mechanism)",
    preconditions=("chaos bucket aam-chaos-67q1zs only", "file_entries baseline exported",
                   "lock absent", "production checkpoints clean"),
    expected=("CLOUD_VERIFY_FAILED", "verified=False, termination=abnormal",
              "zero new file_entries", "Prefect FAILED", "lock released"),
    evidence=("killer log", "verify log excerpt", "run_history row",
              "file_entries before/after export", "Prefect run states"),
    cleanup=("bucket reconverged to source truth", "successor verified run",
             "lock absent"),
    harness_only=("killer arming", "before/after DB export", "assertions"),
)

H3_SAMESIZE_CLOUD = Scenario(
    sid="H3",
    title="Same-size cloud corruption (size-only blindness documentation)",
    family="Same-size cloud corruption",
    real_entrypoints=(
        "flow.backup(mode='cloud')",
        "core.cloud_verify.verify_cloud_integrity (--size-only path)",
        "weekly core.integrity.audit_cloud (hash-capable detector)",
    ),
    fault_tool="chaos_harness.faults.same_size_corrupt (adapted plant_fault logic)",
    preconditions=("4-file chaos dataset hashes recorded", "bucket == source",
                   "lock absent"),
    expected=("daily size-only verify BLIND by design (documents F-T3-1); "
              "hash-capable weekly audit detects"),
    evidence=("plant record (sizes+hashes)", "daily verify result",
              "audit result", "bucket hashes"),
    cleanup=("object restored to source truth", "hashes re-verified"),
    harness_only=("plant/restore", "evidence capture", "assertions"),
)

H4_AUDIT_DIVERGENCE = Scenario(
    sid="H4",
    title="Weekly integrity divergence audit (LAN content mismatch)",
    family="Weekly integrity divergence audit",
    real_entrypoints=(
        "flow.integrity_audit_flow(mode='lan') [deployment integrity-audit]",
        "core.integrity.audit_lan",
        "core.integrity._run_rclone_check",
        "ManifestDB.record_audit (integrity_audits)",
    ),
    fault_tool="chaos_harness.faults.same_size_corrupt (adapted inject_t4 logic)",
    preconditions=("mirror converged except canary", "canary state recorded",
                   "integrity_audits + run_history baselines", "lock absent"),
    expected=("T4-A canary-only VERIFIED; T4-B VERIFICATION_FAILED with "
              "'content mismatch' + divergent path + truthful files_checked "
              "+ NULL bytes_checked; run_history unchanged; read-only; "
              "T4-C successor VERIFIED after restore"),
    evidence=("injection record", "audit rows before/after", "agent log slices",
              "Prefect states", "hash proofs", "read-only proof"),
    cleanup=("dest file restored byte-exact", "successor VERIFIED",
             "lock absent"),
    harness_only=("inject/restore", "row comparison", "assertions"),
)

H5_PERSISTENCE_FAULT = Scenario(
    sid="H5",
    title="Persistence / state-integrity failure (manifest DB fault)",
    family="Persistence/state-integrity failure",
    real_entrypoints=(
        "flow.backup (any leg)",
        "core.manifest.ManifestDB (record paths)",
        "failure/alert paths on persistence error",
    ),
    fault_tool="C:\\ChaosTest\\tools\\cfgfault.py pattern (config-fault class); "
               "DB-specific fault TBD from Batch-2 review",
    preconditions=("manifest baseline exported", "lock absent",
                   "fault mechanism reviewed for blast radius"),
    expected=("loud failure (no silent record loss); F-08 gate unaffected; "
              "exact contract TBD after mechanism review"),
    evidence=("baseline + post exports", "app log error", "Prefect state"),
    cleanup=("config/DB restored from backup", "integrity_check clean"),
    harness_only=("fault arm/restore", "exports", "assertions"),
)

ALL = (H1_ROBOCOPY_KILL, H2_VERIFY_KILL, H3_SAMESIZE_CLOUD,
       H4_AUDIT_DIVERGENCE, H5_PERSISTENCE_FAULT)


def get(sid: str) -> Scenario:
    for s in ALL:
        if s.sid == sid:
            return s
    # Batch-2 registry (deferred import: scenarios_batch2 imports
    # Scenario from this module, so a top-level import would cycle).
    # Blast radius: runner.get_scenario + tests only; ALL unchanged.
    try:
        from chaos_harness import scenarios_batch2 as _b2
        _extra = _b2.ALL2
    except ImportError:
        _extra = ()
    for s in _extra:
        if s.sid == sid:
            return s
    raise KeyError(f"unknown scenario {sid!r}")
