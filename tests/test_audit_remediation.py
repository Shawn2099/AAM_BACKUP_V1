"""Unit & integration tests verifying fixes for BUG-01 through BUG-11.

These tests run without any live hardware Robocopy or SMB network operations.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from core.integrity import (
    _parse_combined_file,
    _run_rclone_check,
)
from core.lan_sync import decide_lan_result
import flow
from flow import _run_cloud_pipeline, integrity_audit_flow


def _make_mock_config(tmp_path=None):
    cfg = MagicMock()
    cfg.firm_name = "TestFirm"
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "C:\\src"
    cfg.paths.lan_destination = "\\\\nas\\backup"
    if tmp_path:
        cfg.paths.backup_lock_path = tmp_path / "backup.lock"
    else:
        import tempfile
        cfg.paths.backup_lock_path = Path(tempfile.gettempdir()) / "test_backup.lock"
    cfg.paths.gcs_key_path = "/tmp/key.json"
    cfg.maintenance.sqlite_busy_timeout_ms = 30000
    cfg.maintenance.sqlite_vacuum_freelist_threshold = 10000
    cfg.maintenance.sqlite_synchronous = "normal"
    cfg.maintenance.log_retention_days = 30
    cfg.cloud.enabled = True
    cfg.cloud.bucket = "test-bucket"
    cfg.cloud.location = "asia-south1"
    cfg.cloud.project_number = "12345"
    cfg.cloud.storage_class = "COLDLINE"
    cfg.cloud.verify_timeout_seconds = 600
    cfg.cloud.max_attempts = 2
    cfg.cloud.retry_delay_seconds = 60
    cfg.lan.enabled = True
    cfg.lan.subprocess_timeout_seconds = 7200
    cfg.lan.shutdown_after_backup = True
    cfg.wol.enabled = True
    cfg.wol.server_ip = "192.168.1.100"
    return cfg


# ── BUG-01: Premature cloud_record_task ──────────────────────────────────────

class TestBug01CloudRecordPostGate:
    @patch("flow.send_failure_alert")
    @patch("flow.cloud_record_task")
    @patch("flow.cloud_verify_and_report_task")
    @patch("flow.cloud_sync_task")
    @patch("flow.cloud_preflight_task")
    @patch("flow.health_check_task")
    @patch("flow.ManifestDB")
    def test_failed_evidence_gate_never_calls_cloud_record_task(
        self, mock_db, mock_health, mock_preflight, mock_sync,
        mock_verify, mock_record, mock_alert,
    ):
        mock_db_inst = MagicMock()
        mock_db_inst.get_cloud_synced_entries.return_value = {}
        mock_db.return_value = mock_db_inst

        mock_sync.with_options.return_value.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0}
        # Verify reports 1 modified difference — evidence_ok fails!
        mock_verify.with_options.return_value.return_value = {
            "verified": False,
            "verify_termination": "normal",
            "diff": {"added": [], "modified": ["corrupt.bin"], "removed": []},
            "size": {"source_bytes": 100, "cloud_bytes": 90},
            "manifest": [{"Path": "corrupt.bin", "Size": 90, "ModTime": 1234}],
        }

        with pytest.raises(RuntimeError, match="Cloud integrity verification FAILED"):
            _run_cloud_pipeline(_make_mock_config(), "test-run", "2026-09-07T10:00:00")

        # CRITICAL ASSERTION: cloud_record_task MUST NOT be called!
        mock_record.assert_not_called()

    @patch("flow.cloud_publish_artifact_task")
    @patch("flow.cloud_record_task")
    @patch("flow.cloud_verify_and_report_task")
    @patch("flow.cloud_sync_task")
    @patch("flow.cloud_preflight_task")
    @patch("flow.health_check_task")
    @patch("flow.ManifestDB")
    def test_passing_evidence_gate_calls_cloud_record_task(
        self, mock_db, mock_health, mock_preflight, mock_sync,
        mock_verify, mock_record, mock_artifact,
    ):
        mock_db_inst = MagicMock()
        mock_db_inst.get_cloud_synced_entries.return_value = {}
        mock_db.return_value = mock_db_inst

        mock_sync.with_options.return_value.return_value = {"status": "CLOUD_COMPLETE", "exit_code": 0}
        mock_verify.with_options.return_value.return_value = {
            "verified": True,
            "verify_termination": "normal",
            "diff": {"added": [], "modified": [], "removed": []},
            "size": {"source_bytes": 100, "cloud_bytes": 100},
            "manifest": [{"Path": "clean.bin", "Size": 100, "ModTime": 1234}],
        }

        result = _run_cloud_pipeline(_make_mock_config(), "test-run", "2026-09-07T10:00:00")
        assert result["status"] == "CLOUD_COMPLETE"
        mock_record.assert_called_once()


# ── BUG-02 & BUG-03 & BUG-08: integrity_audit_flow slot + WoL + clamp ────────

class TestBug02And03AuditOrchestration:
    @patch("flow.send_failure_alert")
    @patch("flow.lan_shutdown_task")
    @patch("flow.wol_check_task")
    @patch("flow.temp_rclone_config")
    @patch("core.integrity.audit_cloud")
    @patch("core.integrity.audit_lan")
    @patch("flow.ManifestDB")
    @patch("flow._backup_slot")
    def test_audit_uses_backup_slot_wol_and_shutdown(
        self, mock_slot, mock_db, mock_audit_lan, mock_audit_cloud,
        mock_temp_cfg, mock_wol, mock_shutdown, mock_alert,
    ):
        mock_db_inst = MagicMock()
        mock_db.return_value = mock_db_inst
        mock_audit_cloud.return_value = {
            "status": "VERIFIED", "scope": "cloud/full", "files_checked": 50,
            "bytes_checked": 1000, "mismatches": 0, "detail": "cloud verified",
        }
        mock_audit_lan.return_value = {
            "status": "VERIFIED", "scope": "lan/full", "files_checked": None,
            "bytes_checked": None, "mismatches": 0, "detail": "lan verified",
        }

        cfg = _make_mock_config()
        with patch("flow.load_config", return_value=cfg):
            integrity_audit_flow(mode="all")

        # 1. Enclosed in _backup_slot
        mock_slot.assert_called_once_with(cfg)
        # 2. WoL check task called before audit_lan
        mock_wol.assert_called_once_with(cfg)
        # 3. Timeout passed to audit_lan
        mock_audit_lan.assert_called_once_with(
            cfg.paths.source_drive, cfg.paths.lan_destination,
            timeout=cfg.lan.subprocess_timeout_seconds,
        )
        # 4. LAN shutdown task called in finally
        mock_shutdown.assert_called_once_with(cfg)
        # 5. BUG-08/F-T4-3: unknown counts persist as NULL (never a
        # false zero); legacy negative sentinels still clamp to 0.
        lan_record_call = [
            c for c in mock_db_inst.record_audit.call_args_list
            if c[0][0]["mode"] == "lan"
        ][0][0][0]
        assert lan_record_call["files_checked"] is None
        assert lan_record_call["bytes_checked"] is None
        assert lan_record_call["mismatches"] == 0
        from flow import _nullable_audit_count
        assert _nullable_audit_count(-1) == 0


# ── BUG-04: False-positive VERIFIED on read errors ───────────────────────────

class TestBug04AuditFileReadErrors:
    def test_parse_combined_captures_error_markers(self, tmp_path):
        diff_file = tmp_path / "combined.txt"
        diff_file.write_text(
            "= clean.txt\n"
            "! locked.xlsx\n"
            "+ missing.pdf\n"
            "- extra.doc\n"
            "* modified.txt\n",
            encoding="utf-8",
        )
        added, removed, modified, unchanged, errors, present = _parse_combined_file(str(diff_file))
        assert present is True
        assert errors == ["locked.xlsx"]
        assert added == ["missing.pdf"]
        assert removed == ["extra.doc"]
        assert modified == ["modified.txt"]
        assert unchanged == ["clean.txt"]

    @patch("core.integrity.subprocess.run")
    def test_run_rclone_check_fails_on_read_errors(self, mock_run, tmp_path):
        # rclone reports 0 differences found, but 1 error while checking (exit 1)
        mock_res = MagicMock()
        mock_res.returncode = 1
        mock_res.stdout = "2026/09/07 10:00:00 NOTICE: 0 differences found\n"
        mock_res.stderr = (
            "2026/09/07 10:00:00 ERROR : locked.xlsx: permission denied\n"
            "2026/09/07 10:00:00 NOTICE: 1 errors while checking\n"
        )
        mock_run.return_value = mock_res

        with patch("core.integrity.tempfile.mkstemp") as mock_mkstemp:
            mock_diff = tmp_path / "diff.txt"
            mock_diff.write_text("! locked.xlsx\n", encoding="utf-8")
            import os
            fd = os.open(str(mock_diff), os.O_RDONLY)
            mock_mkstemp.return_value = (fd, str(mock_diff))

            result = _run_rclone_check("C:\\src", "\\\\nas\\dst", "test", timeout=60)
            assert result["status"] == "VERIFICATION_FAILED"
            assert "file read or check errors occurred" in result["detail"]
            assert result["errors"] == ["locked.xlsx"]


# ── BUG-05: Robocopy Exit Codes 4–7 ──────────────────────────────────────────

class TestBug05RobocopyExitCodes:
    def test_exit_code_4_with_zero_failed_files_is_complete(self):
        log_text = (
            "------------------------------------------------------------------------------\n"
            "               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
            "    Dirs :         3         0         3         0         0         0\n"
            "   Files :        10         0         8         2         0         0\n"
            "   Bytes :      1.0k         0      800        200         0         0\n"
            "   Times :   0:00:01   0:00:00                       0:00:00   0:00:00\n"
            "   Ended : Monday, September 07, 2026 10:00:00 AM\n"
        )
        res = decide_lan_result(4, log_text)
        assert res["status"] == "LAN_COMPLETE"
        assert res["termination"] == "normal"
        assert "robocopy mismatches/extras" in res["reason"]

    def test_exit_code_4_with_failed_files_contradiction_is_suspect(self):
        log_text = (
            "------------------------------------------------------------------------------\n"
            "               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
            "   Files :        10         0         8         2         1         0\n"
            "   Ended : Monday, September 07, 2026 10:00:00 AM\n"
        )
        res = decide_lan_result(4, log_text)
        assert res["status"] == "LAN_SUSPECT"


# ── BUG-11: launch.py reconciliation of integrity-audit ──────────────────────

class TestBug11LaunchReconciliation:
    @patch("prefect.client.orchestration.get_client")
    def test_reconcile_disabled_legs_includes_integrity_audit(self, mock_get_client):
        from launch import _reconcile_disabled_legs

        cfg = _make_mock_config()
        cfg.lan.enabled = False
        cfg.cloud.enabled = False

        # If both are false, integrity-audit should be marked enabled=False
        legs_captured = {}

        class DummyClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def read_deployment_by_name(self, name):
                m = MagicMock()
                m.paused = False
                m.id = "dep-123"
                legs_captured[name] = True
                return m
            async def pause_deployment(self, dep_id):
                pass

        mock_get_client.return_value = DummyClient()
        res = _reconcile_disabled_legs(cfg)
        assert "integrity-audit" in res
        assert "PAUSED" in res["integrity-audit"]
