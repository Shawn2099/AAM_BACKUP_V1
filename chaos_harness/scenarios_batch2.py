"""Batch-2 scenarios B2-1..B2-5 — metadata only (see scenarios.py).

Selected from CHAOS_HARNESS_COVERAGE_MATRIX.md for greatest NEW
business-contract coverage with existing mechanisms. None requires the
blocked H5 DB-fault capability.
"""

from chaos_harness.scenarios import Scenario

B2_1_KILL_MATRIX = Scenario(
    sid="B2-1",
    title="T04/A1 kill-timing matrix + LAN network-chaos leg (phases 6+5)",
    family="Abnormal LAN termination across progress points",
    real_entrypoints=(
        "flow.backup(mode='lan') [chaos deployment backup-lan]",
        "flow._run_lan_pipeline",
        "core.lan_sync.run_lan_sync",
        "core.lan_sync.decide_lan_result",
        "ManifestDB._record_run",
    ),
    fault_tool="C:\\ChaosTest\\tools\\killrob.py (taskkill/wmi/nt × "
               "progress points ~5/25/50/75/99%); network leg: share "
               "stop/start on chaos host + C:\\ChaosTest\\tools\\inv.py",
    preconditions=("mirror converged", "large-file progress observable",
                   "lock absent", "production checkpoints clean"),
    expected=("NO killed run yields LAN_COMPLETE or a success row; "
              "SUSPECT (+alert once, Prefect FAILED, log retained); "
              "share-loss leg yields PARTIAL exit 11; successor heals; "
              "final source==dest SHA match"),
    evidence=("killer logs", "agent log slices", "run_history rows",
              "Prefect states (pfx.py run)", "inv.py sha diffs",
              "hashpair.py samples", "lock states"),
    cleanup=("G8 reaps forensic logs", "share restored", "mirror reconverged",
             "lock absent"),
    harness_only=("kill matrix sequencing", "evidence capture", "assertions"),
)

B2_2_CDK_MATRIX = Scenario(
    sid="B2-2",
    title="C-DK-001 matrix completion: main + sync-only + control (phase 8)",
    family="Cloud evidence-gate falsification",
    real_entrypoints=(
        "flow.backup(mode='cloud') [chaos deployment backup-cloud]",
        "flow._run_cloud_pipeline",
        "core.cloud_sync.run_cloud_sync",
        "core.cloud_verify.verify_cloud_integrity",
        "core.cloud_verify.decide_cloud_verify_result",
        "cloud evidence gate (F-08, 7 clauses)",
        "ManifestDB cloud record (must NOT run on gate failure)",
    ),
    fault_tool="C:\\ChaosTest\\tools\\killrcl.py (sync verb + --check verb); "
               "pattern: dblkill_postrem.py (reference, not forked)",
    preconditions=("chaos bucket aam-chaos-67q1zs only", "4-object dataset "
                   "converged", "file_entries baseline", "lock absent"),
    expected=("main/sync-only: CLOUD_VERIFY_FAILED + alert + Prefect FAILED "
              "+ truthful missing labels; control: CLOUD_COMPLETE; NEVER "
              "CLOUD_COMPLETE + verified=true over partial/dirty GCS"),
    evidence=("killer logs", "verify excerpts", "run_history rows",
              "file_entries before/after", "bucket censuses", "Prefect states"),
    cleanup=("bucket reconverged byte-exact", "successor verified run"),
    harness_only=("variant sequencing", "before/after exports", "assertions"),
)

B2_3_AUDIT_CLASSES = Scenario(
    sid="B2-3",
    title="Live LAN audit divergence-class matrix (phase 13-part2)",
    family="Audit classification across divergence classes",
    real_entrypoints=(
        "flow.integrity_audit_flow(mode='lan') [deployment integrity-audit]",
        "core.integrity.audit_lan",
        "core.integrity._run_rclone_check",
        "ManifestDB.record_audit",
    ),
    fault_tool="chaos_harness.faults.dest_variant "
               "(missing/extra/size-diff/mtime-only/same-size-diff) + restore",
    preconditions=("mirror converged except canary", "baselines of "
                   "integrity_audits + run_history", "lock absent"),
    expected=("missing/extra/size/same-size-diff → VERIFICATION_FAILED with "
              "paths; mtime-only → VERIFIED; run_history unchanged; read-only; "
              "successor VERIFIED after each restore"),
    evidence=("variant records", "audit rows", "agent log slices",
              "hash proofs", "Prefect states"),
    cleanup=("each variant restored byte-exact + successor VERIFIED"),
    harness_only=("variant inject/restore", "row comparison", "assertions"),
)

B2_4_SHARD_SCOPE = Scenario(
    sid="B2-4",
    title="Sharded audit scope isolation (phase 16)",
    family="Audit scope correctness",
    real_entrypoints=(
        "core.integrity.audit_lan(scope_prefixes=[...])",
        "flow.integrity_audit_flow (scope recording)",
        "ManifestDB.record_audit / latest_audit",
    ),
    fault_tool="chaos_harness.faults.same_size_corrupt (one shard dirty, "
               "one shard clean)",
    preconditions=("shard layout understood", "full baseline VERIFIED",
                   "lock absent"),
    expected=("dirty shard → VERIFICATION_FAILED for its scope; clean shard "
              "→ VERIFIED for its scope; no false global VERIFIED; scope "
              "recorded on rows"),
    evidence=("shard audit rows", "scope fields", "hash proofs"),
    cleanup=("shard restored + full VERIFIED"),
    harness_only=("scope sequencing", "row comparison", "assertions"),
)

B2_5_STATE_MODEL = Scenario(
    sid="B2-5",
    title="Integrity state-model observability (phase 17 + P26 fold-in)",
    family="Backup vs integrity separation",
    real_entrypoints=(
        "ManifestDB.last_run / latest_audit",
        "ui._integrity_summary",
        "core.report.generate_report_html",
        "flow.backup + flow.integrity_audit_flow (observed, not driven)",
    ),
    fault_tool="none (observational; reuses states produced by H4/B2-3 runs)",
    preconditions=("at least one COMPLETE backup row", "at least one audit "
                   "row per leg or documented NOT_VERIFIED"),
    expected=("COMPLETE without audit → NOT_VERIFIED surfaced; failed audit "
              "stays authoritative until a later pass; history never "
              "rewritten; report shows integrity section; COMPLETE visually "
              "distinct from VERIFIED"),
    evidence=("manifest queries", "ui summaries", "report HTML excerpts"),
    cleanup=("none required (read-only)"),
    harness_only=("queries", "rendering checks", "assertions"),
)

ALL2 = (B2_1_KILL_MATRIX, B2_2_CDK_MATRIX, B2_3_AUDIT_CLASSES,
        B2_4_SHARD_SCOPE, B2_5_STATE_MODEL)


def get2(sid: str) -> Scenario:
    for s in ALL2:
        if s.sid == sid:
            return s
    raise KeyError(f"unknown batch-2 scenario {sid!r}")
