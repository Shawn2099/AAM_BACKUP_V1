"""Cloud evidence-gate + terminal-policy regression tests.

C-DK-001: sync killed (exit 0) + verify killed (exit 0, verified=True) over
a grossly incomplete destination must NOT record CLOUD_COMPLETE. The gate
consumes the diff/size evidence collected in the same task.
"""

from unittest.mock import MagicMock, patch

import pytest

import flow
from flow import PartialRun


def _make_config():
    cfg = MagicMock()
    cfg.firm_name = "TestCA"
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.paths.gcs_key_path = "/tmp/key.json"
    cfg.maintenance.sqlite_busy_timeout_ms = 30000
    cfg.maintenance.sqlite_vacuum_freelist_threshold = 10000
    cfg.cloud.max_attempts = 2
    cfg.cloud.retry_delay_seconds = 60
    cfg.lan.max_attempts = 2
    cfg.lan.retry_delay_seconds = 600
    return cfg


def _patch_pipeline():
    return (
        patch("flow.send_failure_alert", return_value=True),
        patch("flow.cloud_publish_artifact_task"),
        patch("flow.cloud_record_task"),
        patch("flow.cloud_verify_and_report_task"),
        patch("flow.cloud_sync_task"),
        patch("flow.cloud_preflight_task"),
        patch("flow.health_check_task"),
        patch("flow.get_fy_prefix", return_value="FY26-27"),
        patch("flow.ManifestDB"),
        patch("flow._record_run"),
    )


def _run_with(sync_result, verify_data):
    patches = _patch_pipeline()
    mocks = [p.start() for p in patches]
    mock_alert, mock_artifact, mock_record_task, mock_verify, mock_sync, _, _, _, mock_db, mock_record = mocks
    mock_sync.with_options.return_value.return_value = sync_result
    mock_verify.with_options.return_value.return_value = verify_data
    mock_db.return_value.get_cloud_synced_entries.return_value = {}
    try:
        result = flow._run_cloud_pipeline(_make_config(), "run-x", "2026-09-06T18:00:00")
        return result, mock_record, mock_alert
    except Exception as e:
        return e, mock_record, mock_alert
    finally:
        for p in patches:
            p.stop()


def _clean_verify():
    return {
        "verified": True, "verify_exit_code": 0,
        "size": {"count": 5, "bytes": 5000},
        "manifest": [{"Path": "a.txt", "Size": 100, "ModTime": "2026-09-06T10:00:00Z"}],
        "diff": {"added": [], "removed": [], "modified": [], "unchanged": ["a.txt"]},
        "manifest_error": None,
    }


class TestDoubleKillGate:
    def test_verified_true_with_missing_objects_is_verify_failed(self):
        # C-DK-001 shape: both processes killed (exits 0), diff shows the
        # destination holds a fraction of source.
        out, mock_record, mock_alert = _run_with(
            {"status": "CLOUD_COMPLETE", "exit_code": 0, "error": None},
            {
                "verified": True, "verify_exit_code": 0,
                "size": {"count": 374, "bytes": 379984},
                "manifest": [],
                "diff": {"added": [f"f{i}.dat" for i in range(4627)],
                         "removed": [], "modified": [], "unchanged": []},
                "manifest_error": None,
            },
        )
        assert isinstance(out, RuntimeError)
        assert mock_record.call_args.args[4] == "CLOUD_VERIFY_FAILED"
        mock_alert.assert_called()

    def test_partial_diff_evidence_fails_gate(self):
        verify = _clean_verify()
        verify["diff"] = {"added": [], "removed": [], "modified": [],
                          "unchanged": [], "_partial": True,
                          "_error": "rclone check exited 2"}
        out, mock_record, _ = _run_with(
            {"status": "CLOUD_COMPLETE", "exit_code": 0, "error": None}, verify)
        assert isinstance(out, RuntimeError)
        assert mock_record.call_args.args[4] == "CLOUD_VERIFY_FAILED"

    def test_manifest_error_fails_gate(self):
        verify = _clean_verify()
        verify["manifest_error"] = "lsjson timeout"
        out, mock_record, _ = _run_with(
            {"status": "CLOUD_COMPLETE", "exit_code": 0, "error": None}, verify)
        assert isinstance(out, RuntimeError)
        assert mock_record.call_args.args[4] == "CLOUD_VERIFY_FAILED"

    def test_clean_run_still_completes(self):
        out, mock_record, mock_alert = _run_with(
            {"status": "CLOUD_COMPLETE", "exit_code": 0, "error": None},
            _clean_verify())
        assert isinstance(out, dict) and out["status"] == "CLOUD_COMPLETE"
        mock_alert.assert_not_called()

    def test_verify_kill_lucky_pass_flagged_in_metrics(self):
        import json
        verify = _clean_verify()
        verify["verify_exit_code"] = -1  # check process died; evidence clean
        out, mock_record, _ = _run_with(
            {"status": "CLOUD_COMPLETE", "exit_code": 0, "error": None}, verify)
        assert isinstance(out, dict) and out["status"] == "CLOUD_COMPLETE"
        metrics = json.loads(mock_record.call_args.kwargs.get("extended_metrics"))
        assert metrics["verify_liveness"] is False


class TestSuspectAndPartialPolicy:
    def test_suspect_sync_fails_loud_with_alert(self):
        out, mock_record, mock_alert = _run_with(
            {"status": "CLOUD_SUSPECT", "exit_code": 0,
             "error": "exit 0 with ERROR signals", "suspect_reason": "x"},
            _clean_verify())
        assert isinstance(out, RuntimeError)
        # Sync-phase raise preserves the SUSPECT verdict (not FAILED).
        assert mock_record.call_args.args[4] == "CLOUD_SUSPECT"
        mock_alert.assert_called()

    def test_partial_does_not_return_success(self):
        out, mock_record, mock_alert = _run_with(
            {"status": "CLOUD_PARTIAL", "exit_code": 5, "error": None},
            _clean_verify())
        assert isinstance(out, PartialRun)
        assert mock_record.call_args.args[4] == "CLOUD_PARTIAL"
        # Cloud PARTIAL has its own alert (LAN-F3 equivalent); the generic
        # tail alert is skipped for PartialRun — exactly one alert fires.
        mock_alert.assert_called_once()


class TestLanSuspectPipeline:
    def _run_lan(self, sync_result):
        patches = (
            patch("flow.send_failure_alert", return_value=True),
            patch("flow.lan_publish_artifact_task"),
            patch("flow.lan_record_task"),
            patch("flow.lan_snapshot_after_task"),
            patch("flow.lan_sync_task"),
            patch("flow.lan_snapshot_before_task"),
            patch("flow.lan_preflight_task"),
            patch("flow.wol_check_task"),
            patch("flow.health_check_task"),
            patch("flow._record_run"),
        )
        mocks = [p.start() for p in patches]
        (mock_alert, mock_artifact, mock_record_task, mock_after,
         mock_sync, mock_before, _, _, _, mock_record) = mocks
        mock_sync.with_options.return_value.return_value = sync_result
        # Snapshot tasks are called directly (no with_options) in the LAN
        # pipeline — return real dicts so diff_snapshots runs for real.
        mock_before.return_value = {"f.txt": (10, 1000.0)}
        mock_after.return_value = {"f.txt": (10, 1000.0)}
        try:
            out = flow._run_lan_pipeline(_make_config(), "run-lan", "2026-09-06T01:00:00")
            return out, mock_record, mock_alert
        except Exception as e:
            return e, mock_record, mock_alert
        finally:
            for p in patches:
                p.stop()

    def test_suspect_sync_never_completes(self):
        # T04/A1 pipeline shape: success-band exit with no log evidence.
        out, mock_record, mock_alert = self._run_lan(
            {"status": "LAN_SUSPECT", "exit_code": 1, "termination": "abnormal",
             "log_complete": False, "counts": None,
             "error": "no completed job summary", "files_failed": 0})
        assert isinstance(out, Exception)
        assert not isinstance(out, dict)
        assert mock_record.call_args.args[4] == "LAN_SUSPECT"
        mock_alert.assert_called()  # suspect alert fires; NAS never shuts down

    def test_partial_lan_fails_not_completes(self):
        out, mock_record, mock_alert = self._run_lan(
            {"status": "LAN_PARTIAL", "exit_code": 8, "termination": "normal",
             "log_complete": True, "counts": {"failed": 1},
             "error": "copy errors", "files_failed": 1})
        assert isinstance(out, PartialRun)
        assert mock_record.call_args.args[4] == "LAN_PARTIAL"
        mock_alert.assert_called()  # F3 partial alert
