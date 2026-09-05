"""Weekly integrity-audit tests: rclone-based read-only verification.

Probes the REAL rclone binary (v1.74.2 measured) on controlled datasets:
identical / missing / extra / size-diff / same-size-diff / mtime-only /
empty. Also: manifest traceability, size-only-can-never-verify policy,
and Backup-vs-Integrity reporting.
"""

import shutil

import pytest

from core.integrity import (
    CAPABILITY_HASH_LOCAL,
    audit_cloud,
    audit_lan,
    new_audit_id,
)
from core.manifest import ManifestDB
from core import report as report_mod
from ui import _integrity_summary

RCLONE = shutil.which("rclone")
needs_rclone = pytest.mark.skipif(not RCLONE, reason="rclone binary not available")


def _write(path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _snapshot_tree(root):
    from pathlib import Path as _P
    root = _P(root)
    return {
        str(p.relative_to(root)): (p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_audit_ids_are_unique_and_traceable():
    a, b = new_audit_id("lan", "full"), new_audit_id("lan", "full")
    assert a != b and "lan" in a and "full" in a


@needs_rclone
class TestLanAuditRclone:
    def test_matching_trees_verify(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        _write(src / "d1" / "a.pdf", b"%PDF-1.4 data" * 500)
        _write(src / "b.xlsx", b"PK\x03\x04" + b"x" * 3000)
        _write(dst / "d1" / "a.pdf", b"%PDF-1.4 data" * 500)
        _write(dst / "b.xlsx", b"PK\x03\x04" + b"x" * 3000)
        before = (_snapshot_tree(src), _snapshot_tree(dst))
        result = audit_lan(str(src), str(dst), timeout=300)
        assert result["status"] == "VERIFIED"
        assert result["mismatches"] == 0
        assert result["capability"] == CAPABILITY_HASH_LOCAL
        # Read-only: nothing on either side changed.
        assert (_snapshot_tree(src), _snapshot_tree(dst)) == before

    def test_same_size_content_change_detected(self, tmp_path):
        # C-V2 class: identical size, different bytes must fail — this is
        # what proves content (not size) was actually compared.
        src, dst = tmp_path / "src", tmp_path / "dst"
        _write(src / "doc.pdf", b"A" * 4096)
        _write(dst / "doc.pdf", b"B" * 4096)
        result = audit_lan(str(src), str(dst), timeout=300)
        assert result["status"] == "VERIFICATION_FAILED"
        assert result["mismatches"] == 1

    def test_missing_and_extra_detected(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        _write(src / "gone.txt", b"gone")
        _write(src / "same.txt", b"same")
        _write(dst / "same.txt", b"same")
        _write(dst / "junk.txt", b"junk")
        result = audit_lan(str(src), str(dst), timeout=300)
        assert result["status"] == "VERIFICATION_FAILED"
        assert result["missing"] == ["gone.txt"]
        assert result["extra"] == ["junk.txt"]

    def test_timestamp_only_difference_still_verifies(self, tmp_path):
        import os
        import time
        src, dst = tmp_path / "src", tmp_path / "dst"
        _write(src / "t.txt", b"same-content")
        _write(dst / "t.txt", b"same-content")
        old = time.time() - 100000
        os.utime(dst / "t.txt", (old, old))
        result = audit_lan(str(src), str(dst), timeout=300)
        assert result["status"] == "VERIFIED"

    def test_empty_dataset_verifies(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        result = audit_lan(str(src), str(dst), timeout=300)
        assert result["status"] == "VERIFIED"

    def test_scope_sharding_limits_checked_set(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        _write(src / "FY25" / "old.pdf", b"old")
        _write(src / "FY26" / "new.pdf", b"new")
        _write(dst / "FY25" / "old.pdf", b"old!")
        _write(dst / "FY26" / "new.pdf", b"new")
        scoped = audit_lan(str(src), str(dst), scope_prefixes=["FY26"], timeout=300)
        assert "shard:" in scoped["scope"]
        assert scoped["status"] == "VERIFIED", scoped["detail"]
        full = audit_lan(str(src), str(dst), timeout=300)
        assert full["status"] == "VERIFICATION_FAILED"

    def test_unavailable_source_fails_closed(self, tmp_path):
        result = audit_lan(
            str(tmp_path / "no-such-src"), str(tmp_path / "no-such-dst"), timeout=60)
        assert result["status"] == "VERIFICATION_FAILED"


class TestAuditManifest:
    def test_record_and_latest_audit(self, tmp_path):
        db = ManifestDB(tmp_path / "m.db")
        try:
            assert db.latest_audit("lan") is None  # NOT_VERIFIED default
            db.record_audit({
                "audit_id": "a1", "mode": "lan", "scope": "full",
                "started_at": "2026-09-06T03:00:00",
                "ended_at": "2026-09-06T03:10:00",
                "status": "VERIFIED", "files_checked": 10,
                "bytes_checked": 1000, "mismatches": 0,
                "detail": "capability=HASH_MD5_RCLONE_LOCAL clean",
                "created_at": "2026-09-06T03:10:00",
            })
            row = db.latest_audit("lan")
            assert row["status"] == "VERIFIED"
            # A later failure stays authoritative until a later pass.
            db.record_audit({
                "audit_id": "a2", "mode": "lan", "scope": "full",
                "started_at": "2026-09-13T03:00:00",
                "ended_at": "2026-09-13T03:10:00",
                "status": "VERIFICATION_FAILED", "files_checked": 10,
                "bytes_checked": 1000, "mismatches": 2,
                "detail": "diverged", "created_at": "2026-09-13T03:10:00",
            })
            assert db.latest_audit("lan")["status"] == "VERIFICATION_FAILED"
            # Backup rows untouched by audits.
            assert db.last_run("lan") is None
        finally:
            db.close()

    def test_invalid_audit_status_rejected(self, tmp_path):
        db = ManifestDB(tmp_path / "m.db")
        try:
            with pytest.raises(ValueError):
                db.record_audit({
                    "audit_id": "bad", "mode": "lan",
                    "started_at": "2026-09-06T03:00:00", "status": "COMPLETE",
                })
        finally:
            db.close()


class TestIntegrityReporting:
    def test_report_distinguishes_backup_from_integrity(self, tmp_path):
        db = ManifestDB(tmp_path / "m.db")
        try:
            db.insert_run({
                "run_id": "r1", "mode": "cloud",
                "started_at": "2026-09-06T18:00:00",
                "ended_at": "2026-09-06T18:10:00",
                "status": "CLOUD_COMPLETE", "exit_code": 0,
            })
            db.record_audit({
                "audit_id": "r1", "mode": "cloud", "scope": "full",
                "started_at": "2026-09-06T03:00:00",
                "ended_at": "2026-09-06T03:10:00",
                "status": "VERIFIED", "files_checked": 3,
                "bytes_checked": 300, "mismatches": 0,
                "detail": "clean", "created_at": "2026-09-06T03:10:00",
            })
            html_body = report_mod.generate_report_html(db, "TestCA", 7, "Weekly", is_email=False)
            assert "Integrity Verification" in html_body
            assert "VERIFIED" in html_body
            assert "NOT VERIFIED" in html_body  # lan leg has no audit yet
            assert "does not imply independent content verification" in html_body
        finally:
            db.close()

    def test_ui_summary_defaults_to_not_verified(self):
        from unittest.mock import MagicMock
        db = MagicMock()
        db.latest_audit.return_value = None
        assert _integrity_summary(db, "lan")["status"] == "NOT_VERIFIED"
        db.latest_audit.return_value = {
            "status": "VERIFICATION_FAILED", "ended_at": "2026-09-06T03:10:00",
            "detail": "diverged",
        }
        summary = _integrity_summary(db, "lan")
        assert summary["status"] == "VERIFICATION_FAILED"
        assert summary["checked_at"] == "2026-09-06T03:10:00"
