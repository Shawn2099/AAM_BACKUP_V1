"""Batch-2 harness tests — DRY-RUN ONLY. No triggers, no live faults.

Variant inject/restore runs on chaos-owned scratch fixtures only.
"""

import contextlib
from pathlib import Path

import pytest

from chaos_harness import faults, runner, safety, scenarios
from chaos_harness import scenarios_batch2 as b2

SCRATCH = Path(r"C:\ChaosTest\harness_scratch2")


@pytest.fixture
def scratch():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    yield SCRATCH
    for p in sorted(SCRATCH.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            p.unlink() if p.is_file() else p.rmdir()
    with contextlib.suppress(OSError):
        SCRATCH.rmdir()


class TestBatch2Registry:
    def test_five_scenarios(self):
        assert [s.sid for s in b2.ALL2] == ["B2-1", "B2-2", "B2-3", "B2-4", "B2-5"]
        for s in b2.ALL2:
            assert s.real_entrypoints and s.fault_tool and s.expected
            assert s.evidence and s.cleanup and s.preconditions

    def test_unified_lookup(self):
        assert scenarios.get("B2-3") is b2.B2_3_AUDIT_CLASSES
        assert scenarios.get("H1").sid == "H1"
        with pytest.raises(KeyError):
            scenarios.get("B9")

    def test_batch1_registry_unchanged(self):
        assert [s.sid for s in scenarios.ALL] == ["H1", "H2", "H3", "H4", "H5"]

    def test_no_db_fault_dependency(self):
        for s in b2.ALL2:
            blob = f"{s.fault_tool} {s.title} {s.family}".lower()
            assert "db fault" not in blob and "database fault" not in blob


class TestBatch2Entrypoints:
    def test_tools_referenced_exist(self):
        for name in ("killrob.py", "killrcl.py", "inv.py", "hashpair.py",
                     "pfx.py", "dblkill_postrem.py"):
            roots = [Path(r"C:\ChaosTest\tools") / name,
                     Path(r"C:\ChaosTest\evidence\POST_REMEDIATION\tools") / name]
            assert any(p.exists() for p in roots), name

    def test_app_symbols_exist(self):
        import flow
        from core import integrity
        from core import report as report_mod
        from ui import _integrity_summary
        assert callable(flow.backup) and callable(flow.integrity_audit_flow)
        assert callable(integrity.audit_lan)
        assert callable(_integrity_summary)
        assert callable(report_mod.generate_report_html)

    def test_scope_prefixes_supported(self):
        import inspect

        from core.integrity import audit_lan
        assert "scope_prefixes" in inspect.signature(audit_lan).parameters


class TestVariantFaults:
    @pytest.mark.parametrize("kind", ["missing", "size-diff", "mtime-only"])
    def test_mutating_variants_engage_and_restore(self, scratch, kind):
        dst = scratch / "v.bin"
        dst.write_bytes(b"Z" * 4096)
        rec_path = str(scratch / f"{kind}.json")
        rec = faults.dest_variant(str(dst), kind, rec_path)
        assert rec["engaged"] is True
        if kind == "mtime-only":
            assert dst.read_bytes() == b"Z" * 4096
        res = faults.restore_variant(rec_path)
        assert res["restored_ok"] is True
        assert dst.read_bytes() == b"Z" * 4096

    def test_extra_variant_engage_and_restore(self, scratch):
        dst = scratch / "new_extra.bin"
        rec_path = str(scratch / "extra.json")
        rec = faults.dest_variant(str(dst), "extra", rec_path)
        assert rec["engaged"] is True and dst.exists()
        res = faults.restore_variant(rec_path)
        assert res["restored_ok"] is True and not dst.exists()

    def test_variants_refuse_prod(self, scratch):
        with pytest.raises(safety.SafetyError):
            faults.dest_variant(r"C:\AAMBackup\f", "missing",
                                str(scratch / "r.json"))
        with pytest.raises(ValueError):
            faults.dest_variant(str(scratch / "x"), "nonsense",
                                str(scratch / "r.json"))


class TestBatch2DryRun:
    @pytest.mark.parametrize("sid", ["B2-1", "B2-2", "B2-3", "B2-4", "B2-5"])
    def test_dry_run_ok(self, scratch, monkeypatch, sid):
        import chaos_harness.evidence as ev
        monkeypatch.setattr(ev, "EVIDENCE_ROOT", scratch / "ev")
        out = runner.dry_run(sid)
        assert out["verdict"] == "DRYRUN_OK"
        assert out["scenario"] == sid

    def test_live_still_gated(self):
        with pytest.raises(RuntimeError):
            runner.run("B2-1", app_commit="7bf54fdedaaa88f5", live=False)
