"""Tests for chaos_harness/ — safety guards, evidence packaging, contract assertions."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from chaos_harness import assertions, evidence, safety


class TestSafetyGuards:
    """chaos_harness/safety.py — chaos boundary enforcement."""

    def test_denied_prefixes_exist(self):
        """DENIED_PREFIXES contains expected paths."""
        assert any("C:\\AAMBackup" in p for p in safety.DENIED_PREFIXES)
        assert any("C:\\BackupAgent" in p for p in safety.DENIED_PREFIXES)

    def test_allowed_prefixes_exist(self):
        assert any("C:\\ChaosTest" in p for p in safety.ALLOWED_PREFIXES)
        assert any(r"\\127.0.0.1\aam_test" in p for p in safety.ALLOWED_PREFIXES)

    def test_allowed_path_returns_normalized(self):
        result = safety.assert_chaos_path(r"C:\ChaosTest\evidence\x")
        assert isinstance(result, str)
        assert result  # normalized, non-empty

    def test_unc_loopback_allowed(self):
        result = safety.assert_chaos_path(r"\\127.0.0.1\aam_test\FY26-27")
        assert "127.0.0.1" in result

    def test_denied_path_blocked(self):
        with pytest.raises(safety.SafetyError, match="production/forbidden"):
            safety.assert_chaos_path(r"C:\AAMBackup\test.txt")

    def test_prod_backup_agent_denied(self):
        with pytest.raises(safety.SafetyError):
            safety.assert_chaos_path(r"C:\BackupAgent\test.txt")

    def test_outside_boundary_blocked(self):
        import tempfile
        with pytest.raises(safety.SafetyError, match="outside chaos boundary"):
            safety.assert_chaos_path(str(Path(tempfile.gettempdir()) / "test.txt"))

    def test_assert_chaos_bucket_ok(self):
        assert safety.assert_chaos_bucket("aam-chaos-67q1zs") == "aam-chaos-67q1zs"

    def test_prod_bucket_refused(self):
        with pytest.raises(safety.SafetyError, match="production bucket"):
            safety.assert_chaos_bucket("aam-backup-demo-innovizta")

    def test_unknown_bucket_refused(self):
        with pytest.raises(safety.SafetyError, match="unknown bucket"):
            safety.assert_chaos_bucket("some-other-bucket")

    def test_assert_chaos_prefect_ok(self):
        assert safety.assert_chaos_prefect("http://127.0.0.1:4201/api") == "http://127.0.0.1:4201/api"

    def test_non_chaos_prefect_refused(self):
        with pytest.raises(safety.SafetyError):
            safety.assert_chaos_prefect("http://127.0.0.1:4200/api")

    def test_loopback_shutdown_ok(self):
        assert safety.assert_no_shutdown_target("127.0.0.1") == "127.0.0.1"

    def test_remote_shutdown_refused(self):
        with pytest.raises(safety.SafetyError):
            safety.assert_no_shutdown_target("10.10.151.53")


@pytest.fixture
def chaos_root(tmp_path, monkeypatch):
    """Point the chaos boundary and evidence root at a temp dir."""
    monkeypatch.setattr(safety, "ALLOWED_PREFIXES", (str(tmp_path),))
    monkeypatch.setattr(evidence, "EVIDENCE_ROOT", tmp_path / "evidence")
    return tmp_path


class TestEvidenceCollection:
    """chaos_harness/evidence.py — evidence pack."""

    def test_note_writes_json(self, chaos_root):
        pack = evidence.EvidencePack("S1", "run1").open()
        p = pack.note("meta", {"a": 1})
        assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}

    def test_capture_file(self, chaos_root):
        pack = evidence.EvidencePack("S1", "run1").open()
        src = chaos_root / "source.txt"
        src.write_text("payload")
        dest = pack.capture_file(str(src))
        assert dest.read_text(encoding="utf-8") == "payload"
        assert pack._manifest[-1]["type"] == "file"

    def test_capture_file_custom_name(self, chaos_root):
        pack = evidence.EvidencePack("S1", "run1").open()
        src = chaos_root / "source.txt"
        src.write_text("payload")
        dest = pack.capture_file(str(src), dest_name="renamed.txt")
        assert dest.name == "renamed.txt"

    def test_capture_nonexistent_file(self, chaos_root):
        pack = evidence.EvidencePack("S1", "run1").open()
        with pytest.raises(FileNotFoundError):
            pack.capture_file(str(chaos_root / "nonexistent.txt"))

    def test_capture_file_outside_boundary_refused(self, chaos_root):
        pack = evidence.EvidencePack("S1", "run1").open()
        # Boundary is now chaos_root — C:\AAMBackup is outside it.
        with pytest.raises(safety.SafetyError):
            pack.capture_file(r"C:\AAMBackup\file.txt")

    def test_close_writes_manifest(self, chaos_root):
        pack = evidence.EvidencePack("S1", "run1").open()
        p = pack.close("PASS")
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["scenario"] == "S1"
        assert data["verdict"] == "PASS"
        assert data["artifacts"] == []

    def test_capture_command(self, chaos_root):
        pack = evidence.EvidencePack("S1", "run1").open()
        p = pack.capture_command("whoami", ["python", "-c", "print('ok')"], timeout=60)
        assert "rc=0" in p.read_text(encoding="utf-8")


class TestAssertions:
    """chaos_harness/assertions.py — contract assertions over observed state."""

    def test_check_pass(self):
        r = assertions.check(True, "label")
        assert r == {"label": "label", "pass": True, "detail": ""}

    def test_check_fail_carries_detail(self):
        r = assertions.check(False, "label", "why")
        assert r["pass"] is False
        assert r["detail"] == "why"

    def test_assert_status_match(self):
        r = assertions.assert_status({"status": "LAN_COMPLETE"}, "LAN_COMPLETE", "lan done")
        assert r["pass"] is True

    def test_assert_status_mismatch(self):
        r = assertions.assert_status({"status": "LAN_FAILED"}, "LAN_COMPLETE", "lan done")
        assert r["pass"] is False
        assert "LAN_FAILED" in r["detail"]

    def test_assert_status_none_row(self):
        r = assertions.assert_status(None, "VERIFIED", "verified")
        assert r["pass"] is False

    def test_assert_unchanged(self):
        assert assertions.assert_unchanged(5, 5, "files")["pass"] is True
        assert assertions.assert_unchanged(5, 6, "files")["pass"] is False

    def test_assert_contains(self):
        assert assertions.assert_contains("hello world", "world", "l")["pass"] is True
        assert assertions.assert_contains(None, "world", "l")["pass"] is False

    def test_assert_not_contains(self):
        assert assertions.assert_not_contains("clean", "ERROR", "l")["pass"] is True
        assert assertions.assert_not_contains("has ERROR", "ERROR", "l")["pass"] is False

    def test_summarize_pass(self):
        results = [assertions.check(True, "a"), assertions.check(True, "b")]
        s = assertions.summarize(results)
        assert s == {"total": 2, "passed": 2, "failed": 0, "failures": [], "verdict": "PASS"}

    def test_summarize_fail(self):
        results = [assertions.check(True, "a"), assertions.check(False, "b", "why")]
        s = assertions.summarize(results)
        assert s["verdict"] == "FAIL"
        assert s["failed"] == 1
        assert len(s["failures"]) == 1

    def test_db_readers(self, chaos_root, monkeypatch):
        """latest_run / count_table / latest_audit read the chaos DB read-only."""
        db_path = chaos_root / "manifest.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE run_history (mode TEXT, started_at TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO run_history VALUES ('lan', '2026-01-01T00:00:00', 'LAN_COMPLETE')"
        )
        conn.execute(
            "CREATE TABLE integrity_audits (mode TEXT, started_at TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO integrity_audits VALUES ('lan', '2026-01-01T00:00:00', 'VERIFIED')"
        )
        conn.commit()
        conn.close()

        row = assertions.latest_run("lan", db_path=str(db_path))
        assert row["status"] == "LAN_COMPLETE"
        assert assertions.count_table("run_history", db_path=str(db_path)) == 1
        assert assertions.latest_audit("lan", db_path=str(db_path))["status"] == "VERIFIED"

        # Read-only: the connection must not be able to write.
        with patch.object(sqlite3, "connect", wraps=sqlite3.connect) as spy:
            try:
                assertions.latest_run("lan", db_path=str(db_path))
            except sqlite3.OperationalError:
                pass
            assert "mode=ro" in spy.call_args[0][0]
