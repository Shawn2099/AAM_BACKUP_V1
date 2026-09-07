"""Batch-3 harness tests — DRY-RUN ONLY. No triggers, no live faults,
no rclone execution (cloud helpers tested for safety-gating only)."""

from pathlib import Path

import pytest

from chaos_harness import faults, runner, safety, scenarios
from chaos_harness import scenarios_batch3 as b3


class TestBatch3Registry:
    def test_four_scenarios(self):
        assert [s.sid for s in b3.ALL3] == ["B3-1", "B3-2", "B3-3", "B3-4"]
        for s in b3.ALL3:
            assert s.real_entrypoints and s.fault_tool and s.expected
            assert s.evidence and s.cleanup and s.preconditions

    def test_unified_lookup(self):
        assert scenarios.get("B3-1") is b3.B3_1_CLOUD_MATRIX
        assert scenarios.get("H1").sid == "H1"
        assert scenarios.get("B2-4").sid == "B2-4"
        with pytest.raises(KeyError):
            scenarios.get("B3-9")

    def test_no_db_fault_dependency(self):
        for s in b3.ALL3:
            blob = f"{s.fault_tool} {s.title} {s.family}".lower()
            assert "db fault" not in blob and "database fault" not in blob

    def test_reuse_declared(self):
        assert "ph22_xbackend" in b3.B3_2_XBACKEND.fault_tool
        assert "killrob" in b3.B3_3_PRESSURE.fault_tool
        assert "pfx.py" in b3.B3_4_ENDURANCE.fault_tool


class TestCloudHelpers:
    def test_prod_bucket_refused(self, tmp_path):
        with pytest.raises(safety.SafetyError):
            faults.cloud_plant(str(tmp_path / "x"), "aam-backup-demo-innovizta",
                               "FY26-27", "small_1.dat", r"C:\ChaosTest\rclone_chaos.conf")
        with pytest.raises(safety.SafetyError):
            faults.cloud_remove("aam-backup-demo-innovizta", "FY26-27",
                                "small_1.dat", r"C:\ChaosTest\rclone_chaos.conf")

    def test_nonchaos_conf_refused(self, tmp_path):
        with pytest.raises(safety.SafetyError):
            faults.cloud_plant(str(tmp_path / "x"), "aam-chaos-67q1zs",
                               "FY26-27", "small_1.dat", r"C:\Windows\rclone.conf")

    def test_ph22_tool_exists(self):
        assert Path(r"C:\ChaosTest\evidence\POST_REMEDIATION\tools\ph22_xbackend.py").exists()

    def test_scope_and_report_symbols(self):
        import inspect

        from core.integrity import audit_lan
        from ui import _integrity_summary
        assert "scope_prefixes" in inspect.signature(audit_lan).parameters
        assert callable(_integrity_summary)


class TestBatch3DryRun:
    @pytest.fixture
    def evroot(self, monkeypatch):
        import chaos_harness.evidence as ev
        root = Path(r"C:\ChaosTest\harness_scratch3") / "ev"
        monkeypatch.setattr(ev, "EVIDENCE_ROOT", root)
        yield root
        import contextlib
        import shutil
        with contextlib.suppress(OSError):
            shutil.rmtree(Path(r"C:\ChaosTest\harness_scratch3"), ignore_errors=True)

    @pytest.mark.parametrize("sid", ["B3-1", "B3-2", "B3-3", "B3-4"])
    def test_dry_run_ok(self, sid, evroot):
        out = runner.dry_run(sid)
        assert out["verdict"] == "DRYRUN_OK"
        assert out["scenario"] == sid
