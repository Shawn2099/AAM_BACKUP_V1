"""Batch-1 harness tests — DRY-RUN ONLY. No flow triggers, no fault
injection into live datasets, no destructive execution.

Fault record/restore is exercised on small fixtures under chaos-owned
scratch space (C:\\ChaosTest\\harness_scratch), created and removed here.
"""

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from chaos_harness import assertions, faults, runner, safety, scenarios
from chaos_harness.evidence import EvidencePack

SCRATCH = Path(r"C:\ChaosTest\harness_scratch")


@pytest.fixture
def scratch():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    yield SCRATCH
    for p in sorted(SCRATCH.rglob("*"), reverse=True):
        with contextlib.suppress(OSError):
            p.unlink() if p.is_file() else p.rmdir()
    with contextlib.suppress(OSError):
        SCRATCH.rmdir()


class TestSafety:
    def test_chaos_paths_accepted(self):
        for p in (r"C:\ChaosTest\source", r"C:\ChaosRuntime\manifest.db",
                  r"\\127.0.0.1\aam_test\CHAOS01",
                  r"C:\ChaosTest\evidence\X"):
            assert safety.assert_chaos_path(p)

    @pytest.mark.parametrize("p", [
        r"C:\AAMBackup", r"C:\AAMBackup\x", r"C:\BackupAgent\manifest.db",
        r"E:\data", r"F:\share", r"\\10.10.186.231\share", r"C:\Windows",
    ])
    def test_forbidden_paths_refused(self, p):
        with pytest.raises(safety.SafetyError):
            safety.assert_chaos_path(p)

    def test_prod_bucket_refused(self):
        with pytest.raises(safety.SafetyError):
            safety.assert_chaos_bucket("aam-backup-demo-innovizta")
        with pytest.raises(safety.SafetyError):
            safety.assert_chaos_bucket("some-other-bucket")
        assert safety.assert_chaos_bucket("aam-chaos-67q1zs") == "aam-chaos-67q1zs"

    def test_non_chaos_prefect_refused(self):
        with pytest.raises(safety.SafetyError):
            safety.assert_chaos_prefect("http://prod:4200/api")

    def test_shutdown_host_must_be_loopback(self):
        safety.assert_no_shutdown_target("127.0.0.1")
        with pytest.raises(safety.SafetyError):
            safety.assert_no_shutdown_target("10.10.186.231")


class TestScenarios:
    def test_five_scenarios_registered(self):
        assert [s.sid for s in scenarios.ALL] == ["H1", "H2", "H3", "H4", "H5"]
        for s in scenarios.ALL:
            assert s.real_entrypoints and s.fault_tool and s.expected
            assert s.evidence and s.cleanup and s.preconditions

    def test_entrypoints_link_to_real_code(self):
        import flow
        from core import cloud_verify, integrity, lan_sync, manifest
        assert callable(flow.backup) and callable(flow.integrity_audit_flow)
        assert callable(integrity.audit_lan) and callable(integrity.audit_cloud)
        assert callable(lan_sync.run_lan_sync)
        assert callable(cloud_verify.verify_cloud_integrity)
        assert hasattr(manifest.ManifestDB, "record_audit")

    def test_unknown_scenario_rejected(self):
        with pytest.raises(KeyError):
            scenarios.get("H99")


class TestRunner:
    def test_resolve_targets_are_chaos_only(self):
        for sid in ("H1", "H2", "H3", "H4", "H5"):
            t = runner.resolve_targets(sid)
            assert t["bucket"] == "aam-chaos-67q1zs"
            assert t["scenario"] == sid

    def test_dry_run_proves_evidence(self, scratch, monkeypatch):
        import chaos_harness.evidence as ev
        monkeypatch.setattr(ev, "EVIDENCE_ROOT", scratch / "ev")
        out = runner.dry_run("H4")
        assert out["verdict"] == "DRYRUN_OK"
        manifest = json.loads(Path(out["manifest"]).read_text(encoding="utf-8"))
        assert manifest["scenario"] == "H4"
        assert any(a["name"] == "targets" for a in manifest["artifacts"])

    def test_live_requires_flag_and_pin(self):
        with pytest.raises(RuntimeError):
            runner.run("H1", app_commit="7bf54fdedaaa88f5", live=False)
        with pytest.raises(RuntimeError):
            runner.run("H1", app_commit=None, live=True)
        with pytest.raises(RuntimeError):
            runner.run("H1", app_commit="x", live=True)


class TestFaultsOnScratch:
    def test_same_size_corrupt_and_restore(self, scratch):
        src = scratch / "a.bin"
        dst = scratch / "b.bin"
        src.write_bytes(b"A" * 8192)
        dst.write_bytes(b"A" * 8192)
        rec_path = str(scratch / "rec.json")
        rec = faults.same_size_corrupt(str(src), str(dst), rec_path)
        assert rec["size_unchanged"] is True
        assert rec["content_diverged"] is True
        assert src.read_bytes() == b"A" * 8192  # source untouched
        assert dst.read_bytes() != b"A" * 8192
        res = faults.restore(rec_path)
        assert res["restored_ok"] is True
        assert dst.read_bytes() == b"A" * 8192

    def test_corrupt_refuses_prod_paths(self):
        with pytest.raises(safety.SafetyError):
            faults.same_size_corrupt(r"C:\AAMBackup\f", r"C:\ChaosTest\x",
                                     r"C:\ChaosTest\harness_scratch\r.json")

    def test_tool_wrappers_target_existing_tools(self):
        assert Path(faults._tool("killrcl.py")).exists()
        assert Path(faults._tool("killrob.py")).exists()
        assert Path(faults._tool("pfx.py")).exists()
        assert Path(faults._tool("hashpair.py")).exists()
        assert Path(faults._tool("inv.py")).exists()


class TestEvidencePack:
    def test_pack_lifecycle(self, scratch, monkeypatch):
        import chaos_harness.evidence as ev
        monkeypatch.setattr(ev, "EVIDENCE_ROOT", scratch / "ev")
        pack = EvidencePack("HX", run_name="t1").open()
        pack.note("n", {"a": 1})
        src = scratch / "s.txt"
        src.write_text("hello", encoding="utf-8")
        pack.capture_file(str(src))
        manifest = pack.close("DRYRUN_OK")
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
        assert data["verdict"] == "DRYRUN_OK"
        assert any(a.get("file") == "s.txt" for a in data["artifacts"])

    def test_pack_refuses_prod_source(self, scratch, monkeypatch):
        import chaos_harness.evidence as ev
        monkeypatch.setattr(ev, "EVIDENCE_ROOT", scratch / "ev")
        pack = EvidencePack("HX", run_name="t2").open()
        with pytest.raises(safety.SafetyError):
            pack.capture_file(r"C:\BackupAgent\manifest.db")


class TestAssertions:
    def test_pure_checks(self):
        assert assertions.assert_status({"status": "X"}, "X", "l")["pass"]
        assert not assertions.assert_status({"status": "Y"}, "X", "l")["pass"]
        assert assertions.assert_unchanged(5, 5, "l")["pass"]
        assert not assertions.assert_unchanged(5, 6, "l")["pass"]
        s = assertions.summarize([{"label": "a", "pass": True, "detail": ""}])
        assert s["verdict"] == "PASS"

    def test_ro_conn_refuses_prod_db(self):
        with pytest.raises(safety.SafetyError):
            assertions._ro_conn(r"C:\BackupAgent\manifest.db")

    def test_live_readers_hit_chaos_db_only(self, monkeypatch):
        seen = {}

        class FakeConn:
            def __init__(self, *a, **k):
                seen["uri"] = a[0] if a else k
            row_factory = None

            def execute(self, q, params=()):
                class R:
                    def fetchone(self):
                        return (0,) if q.strip().upper().startswith("SELECT COUNT") else None
                return R()

            def close(self):
                pass

        monkeypatch.setattr(sqlite3, "connect", FakeConn)
        assert assertions.latest_run("lan") is None
        assert "C:\\ChaosRuntime\\manifest.db" in seen["uri"]
        assert assertions.latest_audit("lan") is None
        assert assertions.count_table("run_history") == 0
