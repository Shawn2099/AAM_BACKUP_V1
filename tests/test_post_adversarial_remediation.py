"""Post-pull adversarial campaign remediation regression tests.

Covers the live-confirmed findings against commit 7bf54fde:
  F-T4-2  audit misclassifies genuine content mismatch as read/check error
  F-T4-1  LAN VERIFIED unreachable while the mount canary is extra-in-dest
  F-T4-3  files_checked/bytes_checked never populated
  F-T3-2  cloud gate message mis-attributes abnormal termination
  BUG-05  RC 4-7 -> LAN_COMPLETE contract decision (T1 INVALID, preserved)
  BUG-01  failed verification persists no unverified file_entries (T3B shape)
  Opt-B   shutdown_on_all_retries_exhausted default false (T2 preserved)

All checker-output tests use mocked subprocess runs with realistic
rclone v1.74.2 log shapes (see T4 targeted_check evidence); the live
rclone leg is already covered by tests/test_integrity_audit.py.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core import integrity as integ_mod
from core.integrity import _parse_combined_file, _run_rclone_check
from core.lan_sync import decide_lan_result
from flow import _cloud_gate_failure_message, _nullable_audit_count


def _mock_proc(returncode=1, stdout="", stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


def _run_with_combined(combined_text, proc):
    """Run _run_rclone_check with a fake rclone that writes combined_text."""
    def fake_run(cmd, **kwargs):
        idx = list(cmd).index("--combined")
        Path(cmd[idx + 1]).write_text(combined_text, encoding="utf-8")
        return proc
    with patch.object(integ_mod.subprocess, "run", side_effect=fake_run):
        return _run_rclone_check("/src", "/dst", "test", timeout=60)


MISMATCH_STDERR = (
    "2026/09/07 19:40:00 ERROR : file_1.dat: md5 differ\n"
    "2026/09/07 19:40:00 NOTICE: Local file system at //?/C:/dst: 1 differences found\n"
    "2026/09/07 19:40:00 NOTICE: Local file system at //?/C:/dst: 1 errors while checking\n"
    "2026/09/07 19:40:00 NOTICE: Failed to check: 1 differences found\n"
)
CLEAN_STDERR = (
    "2026/09/07 19:40:00 NOTICE: Local file system at //?/C:/dst: 0 differences found\n"
)


class TestMismatchVsReadError:
    def test_live_t4_shape_is_content_mismatch_not_read_error(self):
        result = _run_with_combined(
            "* file_1.dat\n", _mock_proc(1, stderr=MISMATCH_STDERR))
        assert result["status"] == "VERIFICATION_FAILED"
        assert "content mismatch" in result["detail"]
        assert "file read or check errors" not in result["detail"]
        assert result["missing"] == []
        assert "file_1.dat" in result["detail"]

    def test_mismatch_markers_do_not_imply_read_error(self):
        # Every diverged run carries these strings; none is a read error.
        result = _run_with_combined(
            "* a.bin\n* b.bin\n", _mock_proc(1, stderr=MISMATCH_STDERR))
        assert result["status"] == "VERIFICATION_FAILED"
        assert "content mismatch" in result["detail"]
        assert result["mismatches"] == 2

    def test_multiple_mismatches_all_paths_listed(self):
        stderr = MISMATCH_STDERR.replace("1 differences", "3 differences").replace(
            "1 errors", "3 errors")
        result = _run_with_combined(
            "* a.bin\n+ gone.txt\n- junk.txt\n", _mock_proc(1, stderr=stderr))
        assert result["status"] == "VERIFICATION_FAILED"
        assert "content mismatch" in result["detail"]
        assert result["mismatches"] == 3
        for p in ("a.bin", "gone.txt", "junk.txt"):
            assert p in result["detail"]

    def test_bang_marker_is_read_error(self):
        stderr = (
            "2026/09/07 19:40:00 ERROR : locked.xlsx: permission denied\n"
            "2026/09/07 19:40:00 NOTICE: Local file system: 0 differences found\n"
            "2026/09/07 19:40:00 NOTICE: Local file system: 1 errors while checking\n"
        )
        result = _run_with_combined(
            "! locked.xlsx\n= ok.txt\n", _mock_proc(1, stderr=stderr))
        assert result["status"] == "VERIFICATION_FAILED"
        assert "file read or check errors occurred" in result["detail"]
        assert "content mismatch" not in result["detail"]
        assert result["errors"] == ["locked.xlsx"]

    def test_io_error_line_without_bang_marker_is_read_error(self):
        stderr = (
            "2026/09/07 19:40:00 ERROR : deep/dir/x.dat: permission denied\n"
            "2026/09/07 19:40:00 NOTICE: Local file system: 0 differences found\n"
        )
        result = _run_with_combined("= ok.txt\n", _mock_proc(1, stderr=stderr))
        assert result["status"] == "VERIFICATION_FAILED"
        assert "file read or check errors occurred" in result["detail"]

    def test_clean_run_verifies(self):
        result = _run_with_combined("= a.txt\n= b.txt\n", _mock_proc(0, stderr=CLEAN_STDERR))
        assert result["status"] == "VERIFIED"
        assert result["mismatches"] == 0

    def test_missing_summary_fails_closed(self):
        result = _run_with_combined(
            "* a.bin\n", _mock_proc(1, stderr="2026/09/07 19:40:00 NOTICE: checking\n"))
        assert result["status"] == "VERIFICATION_FAILED"
        assert result["termination"] == "abnormal"
        assert "completion summary" in result["detail"]

    def test_abnormal_termination_fails_closed(self):
        result = _run_with_combined("", _mock_proc(2, stderr="fatal error, no summary"))
        assert result["status"] == "VERIFICATION_FAILED"
        assert result["termination"] == "abnormal"
        assert "process error" in result["detail"]

    def test_malformed_combined_contradicts_summary(self):
        result = _run_with_combined(
            "garbage line with no marker\n",
            _mock_proc(1, stderr=MISMATCH_STDERR))
        assert result["status"] == "VERIFICATION_FAILED"
        assert "contradictory" in result["detail"]

    def test_error_word_in_filename_is_not_an_error_line(self):
        # Anchored log-level regex: a filename containing "error:" never
        # counts as contradictory evidence.
        stderr = (
            "2026/09/07 19:40:00 NOTICE: Local file system: 0 differences found\n"
        )
        result = _run_with_combined("= my error: file.txt\n", _mock_proc(0, stderr=stderr))
        assert result["status"] == "VERIFIED"


class TestPathIdentification:
    def test_single_mismatch_path_recorded(self):
        result = _run_with_combined(
            "* file_1.dat\n", _mock_proc(1, stderr=MISMATCH_STDERR))
        assert "file_1.dat" in result["detail"]

    def test_multiple_mismatch_paths_recorded(self):
        stderr = MISMATCH_STDERR.replace("1 differences", "2 differences").replace(
            "1 errors", "2 errors")
        result = _run_with_combined(
            "* a.bin\n- extra.txt\n", _mock_proc(1, stderr=stderr))
        assert "a.bin" in result["detail"] and "extra.txt" in result["detail"]

    def test_no_extractable_path_stays_failed_without_invented_path(self):
        result = _run_with_combined(
            "not a combined line\n",
            _mock_proc(1, stderr=MISMATCH_STDERR))
        assert result["status"] == "VERIFICATION_FAILED"
        assert "contradictory" in result["detail"]
        assert "not a combined line" not in result["detail"]

    def test_parse_combined_markers(self, tmp_path):
        f = tmp_path / "c.txt"
        f.write_text("= ok.txt\n+ missing.txt\n- extra.txt\n* changed.txt\n! bad.txt\n",
                     encoding="utf-8")
        added, removed, modified, unchanged, errors, present = _parse_combined_file(str(f))
        assert present is True
        assert (added, removed, modified, unchanged, errors) == (
            ["missing.txt"], ["extra.txt"], ["changed.txt"], ["ok.txt"], ["bad.txt"])


class TestAuditCounters:
    def test_files_checked_counts_compared_files(self):
        result = _run_with_combined("= a.txt\n= b.txt\n", _mock_proc(0, stderr=CLEAN_STDERR))
        assert result["files_checked"] == 2

    def test_files_checked_includes_mismatches(self):
        result = _run_with_combined(
            "* a.bin\n= ok.txt\n", _mock_proc(1, stderr=MISMATCH_STDERR))
        assert result["files_checked"] == 2

    def test_bytes_checked_unknown_not_zero(self):
        result = _run_with_combined("= a.txt\n", _mock_proc(0, stderr=CLEAN_STDERR))
        assert result["bytes_checked"] is None

    def test_incomplete_run_counts_unknown(self):
        result = _run_with_combined("", _mock_proc(2, stderr="boom"))
        assert result["files_checked"] is None
        assert result["bytes_checked"] is None

    def test_nullable_count_helper(self):
        assert _nullable_audit_count(None) is None
        assert _nullable_audit_count(5) == 5
        assert _nullable_audit_count(0) == 0
        assert _nullable_audit_count(-1) == 0


class TestCanaryExclusion:
    CANARY_SUMMARY = (
        "2026/09/07 19:40:00 NOTICE: Local file system: 1 differences found\n"
        "2026/09/07 19:40:00 NOTICE: Local file system: 1 errors while checking\n"
        "2026/09/07 19:40:00 NOTICE: Failed to check: 1 differences found\n"
    )

    def test_canary_only_reaches_verified(self):
        result = _run_with_combined(
            "- .AAM_TARGET_MOUNTED\n= a.txt\n", _mock_proc(1, stderr=self.CANARY_SUMMARY))
        assert result["status"] == "VERIFIED"
        assert result["mismatches"] == 0

    def test_clean_without_canary_verifies(self):
        result = _run_with_combined("= a.txt\n", _mock_proc(0, stderr=CLEAN_STDERR))
        assert result["status"] == "VERIFIED"

    def test_canary_plus_real_mismatch_still_fails(self):
        stderr = self.CANARY_SUMMARY.replace("1 differences", "2 differences").replace(
            "1 errors", "2 errors")
        result = _run_with_combined(
            "- .AAM_TARGET_MOUNTED\n* real.bin\n", _mock_proc(1, stderr=stderr))
        assert result["status"] == "VERIFICATION_FAILED"
        assert "content mismatch" in result["detail"]
        assert "real.bin" in result["detail"]
        assert result["mismatches"] == 1

    def test_real_mismatch_without_canary_fails(self):
        result = _run_with_combined(
            "* file_1.dat\n", _mock_proc(1, stderr=MISMATCH_STDERR))
        assert result["status"] == "VERIFICATION_FAILED"
        assert result["mismatches"] == 1

    def test_canary_never_excluded_from_added_or_modified(self):
        stderr = self.CANARY_SUMMARY
        result = _run_with_combined(
            "* .AAM_TARGET_MOUNTED\n", _mock_proc(1, stderr=stderr))
        assert result["status"] == "VERIFICATION_FAILED"
        assert result["mismatches"] == 1

    def test_dotfiles_in_general_are_not_excluded(self):
        stderr = self.CANARY_SUMMARY
        result = _run_with_combined(
            "- .other_marker\n", _mock_proc(1, stderr=stderr))
        assert result["status"] == "VERIFICATION_FAILED"
        assert result["mismatches"] == 1


class TestBug01AbnormalTerminationShape:
    @patch("flow.send_failure_alert")
    @patch("flow.cloud_record_task")
    @patch("flow.cloud_verify_and_report_task")
    @patch("flow.cloud_sync_task")
    @patch("flow.cloud_preflight_task")
    @patch("flow.health_check_task")
    @patch("flow.ManifestDB")
    def test_killed_verify_persists_no_entries(
        self, mock_db, mock_health, mock_preflight, mock_sync,
        mock_verify, mock_record, mock_alert,
    ):
        from flow import _run_cloud_pipeline

        mock_db_inst = MagicMock()
        mock_db_inst.get_cloud_synced_entries.return_value = {}
        mock_db.return_value = mock_db_inst
        mock_sync.with_options.return_value.return_value = {
            "status": "CLOUD_COMPLETE", "exit_code": 0}
        # T3B live shape: sync moved 4000 files, verify killed mid-check.
        mock_verify.with_options.return_value.return_value = {
            "verified": False,
            "verify_termination": "abnormal",
            "verify_completion": False,
            "verify_differences": None,
            "verify_reason": "no rclone completion summary",
            "diff": {"added": [], "modified": [], "removed": []},
            "size": {"count": 4004, "bytes": 1000},
            "manifest": [{"Path": f"f{i}.dat", "Size": 1, "ModTime": 1234}
                         for i in range(10)],
        }
        cfg = MagicMock()
        cfg.firm_name = "TestFirm"
        cfg.paths.database_path = ":memory:"
        cfg.paths.source_drive = "C:\\src"
        cfg.paths.backup_lock_path = Path(tempfile.gettempdir()) / "t3b-shape.lock"
        cfg.paths.gcs_key_path = "/tmp/key.json"
        cfg.maintenance.sqlite_busy_timeout_ms = 30000
        cfg.maintenance.sqlite_vacuum_freelist_threshold = 10000
        cfg.maintenance.sqlite_synchronous = "normal"
        cfg.maintenance.log_retention_days = 30
        cfg.cloud.max_attempts = 2
        cfg.cloud.retry_delay_seconds = 60
        with pytest.raises(RuntimeError, match="Cloud integrity verification FAILED"):
            _run_cloud_pipeline(cfg, "t3b-shape", "2026-09-07T10:00:00")
        mock_record.assert_not_called()


class TestGateMessageAccuracy:
    def _base(self, **over):
        d = {
            "verified": False, "verify_termination": "normal",
            "verify_completion": True, "verify_differences": 1,
            "verify_reason": "rclone reported 1 difference(s) (exit 1)",
        }
        d.update(over)
        return d

    def test_abnormal_termination_names_termination_not_differences(self):
        msg = _cloud_gate_failure_message(
            self._base(verify_termination="abnormal", verify_completion=False,
                       verify_differences=None,
                       verify_reason="no rclone completion summary"),
            {"added": [], "removed": [], "modified": []}, {"count": 1})
        assert "did not complete normally" in msg
        assert "termination=abnormal" in msg
        assert "no rclone completion summary" in msg
        assert "rclone check found differences vs source" not in msg

    def test_content_mismatch_names_differences(self):
        msg = _cloud_gate_failure_message(
            self._base(),
            {"added": ["a.bin"], "removed": [], "modified": []}, {"count": 1})
        assert "found differences vs source" in msg
        assert "missing-from-cloud=1" in msg

    def test_missing_summary_names_missing_summary(self):
        msg = _cloud_gate_failure_message(
            self._base(verify_completion=False,
                       verify_reason="no rclone completion summary"),
            {"added": [], "removed": [], "modified": []}, {"count": 1})
        assert "did not complete normally" in msg

    def test_manifest_error_names_evidence_error(self):
        v = self._base(verify_reason=None)
        v["manifest_error"] = "lsjson timeout"
        msg = _cloud_gate_failure_message(
            v, {"added": [], "removed": [], "modified": []}, {"count": 1})
        assert "manifest/evidence error" in msg
        assert "lsjson timeout" in msg

    def test_gate_prefix_preserved(self):
        msg = _cloud_gate_failure_message(
            self._base(), {"added": ["x"], "removed": [], "modified": []}, {})
        assert msg.startswith("Cloud integrity verification FAILED")


class TestOptionBDefault:
    def test_model_default_false(self):
        from models.config import LanConfig
        assert LanConfig().shutdown_on_all_retries_exhausted is False

    def test_absent_key_defaults_false(self):
        from models.config import LanConfig
        cfg = LanConfig.model_validate({"enabled": True})
        assert cfg.shutdown_on_all_retries_exhausted is False


_RC45_LOG = (
    "------------------------------------------------------------------------------\n"
    "               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
    "    Dirs :         3         0         3         0         0         0\n"
    "   Files :        10         0         8         2         0         0\n"
    "   Bytes :      1.0k         0      800        200         0         0\n"
    "   Times :   0:00:01   0:00:00                       0:00:00   0:00:00\n"
    "   Ended : Monday, September 07, 2026 10:00:00 AM\n"
)


class TestLanRc47Decision:
    @pytest.mark.parametrize("code", [4, 5, 6, 7])
    def test_rc_4_to_7_with_clean_summary_is_complete(self, code):
        res = decide_lan_result(code, _RC45_LOG)
        assert res["status"] == "LAN_COMPLETE"
        assert res["termination"] == "normal"
        assert res["log_complete"] is True

    def test_rc_4_with_failed_files_is_suspect(self):
        log = _RC45_LOG.replace(
            "   Files :        10         0         8         2         0         0",
            "   Files :        10         0         8         2         1         0")
        res = decide_lan_result(4, log)
        assert res["status"] == "LAN_SUSPECT"

    def test_existing_valid_rc_behavior_retained(self):
        clean = _RC45_LOG.replace(
            "   Files :        10         0         8         2         0         0",
            "   Files :        10        10         0         0         0         0")
        assert decide_lan_result(1, clean)["status"] == "LAN_COMPLETE"
        assert decide_lan_result(1, "truncated, no summary")["status"] == "LAN_SUSPECT"
        assert decide_lan_result(16, clean)["status"] == "LAN_FAILED"
