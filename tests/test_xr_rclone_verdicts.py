"""XR-3 / XR-4 / XR-6: rclone verdicts judged on rclone's own evidence.

The logs in test_support/rclone_logs/ are REAL rclone v1.74.3 JSON logs
(local -> local syncs, paths scrubbed), see
docs/audit/EXIT_CODES_AND_RETRIES_2026-10-16.md section 1:
  deletes_only_exit9      - 2 files deleted, nothing uploaded, exit 9 (F3)
  max_delete_exit7        - --max-delete 2 tripped with 6 planned, exit 7 (F2)
  recovered_retry_exit0   - attempt 1 failed (locked file), attempt 2 succeeded, exit 0 (F4)
  locked_file_exit1       - a file locked all run, both attempts failed, exit 1 (F5)
  max_duration_exit10     - --max-duration stop, exit 10 (F6)
"""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from core import cloud_sync as cs

LOGS = Path(__file__).resolve().parent.parent / "test_support" / "rclone_logs"


def real_log(name: str) -> str:
    return (LOGS / f"{name}.jsonl").read_text(encoding="utf-8")


def _line(level, msg, **extra):
    return json.dumps({"level": level, "msg": msg, **extra})


def _stats(**stats):
    base = {"transfers": 0, "deletes": 0, "deletedDirs": 0, "renames": 0, "errors": 0,
            "fatalError": False, "retryError": False, "checks": 1, "bytes": 0}
    return _line("info", "stats", stats={**base, **stats})


@contextmanager
def _mock_temp_config(*args, **kwargs):
    yield os.path.join(tempfile.gettempdir(), "rclone_test.conf")


@pytest.fixture
def sync(tmp_path):
    from core.run_artefacts import RunArtefacts
    from test_support.fake_tools import fake_run_tool

    art = RunArtefacts(tmp_path / "run_logs", "run-xr-cloud", day="2026-10-16")

    def run(*, exit_code, log, **kwargs):
        params = dict(source="/src", bucket="bucket", fy_prefix="FY", gcs_key_path="/key",
                      project_number="123", storage_class="COLDLINE", artefacts=art)
        params.update(kwargs)
        with patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config), \
                patch("core.cloud_sync.resolve_binary", return_value="rclone"), \
                fake_run_tool("core.cloud_sync", exit_code=exit_code, log=log):
            return cs.run_cloud_sync(**params)
    return run


# ── F1: the documented meaning of exit 1 and 2 ────────────────────────────

def test_exit_code_meanings_follow_rclone_source():
    assert cs.RCLONE_EXIT_MEANING[1] == "error not otherwise categorised"
    assert cs.RCLONE_EXIT_MEANING[2] == "syntax or usage error"
    assert set(cs.RCLONE_EXIT_MEANING) == set(range(11))


# ── XR-4: errors from attempts rclone already retried ─────────────────────

def test_recovered_retry_is_complete_not_suspect(sync):
    result = sync(exit_code=0, log=real_log("recovered_retry_exit0"))
    assert result["status"] == "CLOUD_COMPLETE"
    assert result["suspect_reason"] is None and result["error"] is None
    assert result["rclone_failed_attempts"] == 1


def test_errors_in_the_final_attempt_still_make_exit_0_suspect(sync):
    # C-DK-001 is kept for the attempt whose result counts
    log = "\n".join([
        _line("error", "Failed to copy: x"),
        _line("error", "Attempt 1/3 failed with 1 errors and: x"),
        _line("error", "Failed to copy: googleapi: Error 403: forbidden"),
        _line("error", "Attempt 2/3 succeeded"),
        _stats(transfers=1),
    ])
    result = sync(exit_code=0, log=log)
    assert result["status"] == "CLOUD_SUSPECT"
    assert "Error 403" in result["suspect_reason"]
    assert "Failed to copy: x" not in result["suspect_reason"]
    assert "succeeded" not in result["suspect_reason"]


def test_failed_marker_without_success_notice_fails_closed(sync):
    # rclone always logs "Attempt k/N succeeded" after a recovered attempt
    # (cmd.go:263); without it the log is not understood - keep everything.
    log = "\n".join([
        _line("error", "Failed to copy: x"),
        _line("error", "Attempt 1/3 failed with 1 errors and: x"),
        _stats(transfers=1),
    ])
    assert sync(exit_code=0, log=log)["status"] == "CLOUD_SUSPECT"


def test_final_stats_errors_make_exit_0_suspect_even_without_error_lines(sync):
    result = sync(exit_code=0, log=_stats(transfers=1, errors=2))
    assert result["status"] == "CLOUD_SUSPECT"
    assert "2 error" in result["suspect_reason"]


def test_exit_9_after_recovered_attempt_is_not_failed(sync):
    log = "\n".join([
        _line("error", "Failed to copy: x"),
        _line("error", "Attempt 1/3 failed with 1 errors and: x"),
        _line("error", "Attempt 2/3 succeeded"),
        _stats(),
    ])
    result = sync(exit_code=9, log=log)
    assert result["status"] == "CLOUD_NO_CHANGES_COMPLETE"
    assert result["rclone_failed_attempts"] == 1


def test_plaintext_fatal_marker_still_counts():
    has, tail = cs.scan_rclone_log_for_errors("Failed to create file system: bucket not found")
    assert has and "bucket not found" in tail


def test_final_attempt_scan_ignores_only_retried_attempts():
    log = real_log("locked_file_exit1")            # both attempts failed
    has, tail = cs.scan_rclone_log_for_errors(log)
    assert has and "Failed to copy" in tail
    assert "Attempt 1/2" not in tail                # attempt 1's lines are history
    assert cs.count_failed_attempts(log) == 2


# ── XR-3: exit 9 is "no changes" only when rclone's stats prove it ────────

def test_deletes_only_exit_9_is_complete_not_no_changes(sync):
    result = sync(exit_code=9, log=real_log("deletes_only_exit9"))
    assert result["status"] == "CLOUD_COMPLETE"
    assert result["stats"]["deletes"] == 2


@pytest.mark.parametrize("field", ["transfers", "deletes", "deletedDirs", "renames"])
def test_any_change_in_stats_means_not_no_changes(sync, field):
    result = sync(exit_code=9, log=_stats(**{field: 1}))
    assert result["status"] == "CLOUD_COMPLETE"


def test_exit_9_without_stats_cannot_prove_no_changes(sync):
    result = sync(exit_code=9, log=_line("info", "There was nothing to transfer"))
    assert result["status"] == "CLOUD_COMPLETE"


def test_exit_9_with_zero_stats_is_no_changes(sync):
    result = sync(exit_code=9, log=_stats())
    assert result["status"] == "CLOUD_NO_CHANGES_COMPLETE"


# ── XR-1 evidence: the --max-delete stop is recognised ────────────────────

def test_max_delete_stop_is_recognised(sync):
    result = sync(exit_code=7, log=real_log("max_delete_exit7"), max_delete=2)
    assert result["status"] == "CLOUD_FAILED"
    assert result["max_delete_reached"] is True
    assert result["stats"]["deletes"] == 2
    assert result["retryable"] is False


def test_other_exit_7_is_not_a_max_delete_stop(sync):
    result = sync(exit_code=7, log=_line("error", "Failed to create file system: 401"))
    assert result["max_delete_reached"] is False


# ── XR-6: which failures a retry can change (rclone's own taxonomy) ───────

@pytest.mark.parametrize("code,stats,expected", [
    (1, {"fatalError": False}, True),       # uncategorised, e.g. a locked file (F5)
    (4, {"fatalError": False}, True),       # file vanished mid-run
    (5, {"fatalError": False}, True),       # temporary error
    (5, {"fatalError": True}, False),       # rclone says no retry can help
    (1, None, False),                       # deletions unknown -> fail closed
    (2, {"fatalError": False}, False),      # usage error
    (3, {"fatalError": False}, False),      # directory not found
    (6, {"fatalError": False}, False),      # no-retry error
    (7, {"fatalError": True}, False),       # fatal
    (8, {"fatalError": False}, False),      # --max-transfer
    (0, {"fatalError": False}, False),
    (9, {"fatalError": False}, False),
    (10, {"fatalError": True}, False),      # time limit: PARTIAL, not a retry
])
def test_retryable(code, stats, expected):
    assert cs.rclone_retryable(code, stats, max_delete_reached=False) is expected


def test_max_delete_stop_is_never_retryable():
    assert cs.rclone_retryable(5, {"fatalError": False}, max_delete_reached=True) is False


def test_real_locked_file_run_is_retryable(sync):
    result = sync(exit_code=1, log=real_log("locked_file_exit1"))
    assert result["status"] == "CLOUD_FAILED" and result["retryable"] is True


def test_timeout_is_not_retryable(sync):
    from test_support.fake_tools import fake_run_tool
    with patch("core.cloud_sync.temp_rclone_config", side_effect=_mock_temp_config), \
            fake_run_tool("core.cloud_sync", timed_out=True):
        result = cs.run_cloud_sync(source="/src", bucket="b", fy_prefix="FY", gcs_key_path="/k",
                                   project_number="1", storage_class="COLDLINE")
    assert result["retryable"] is False


def test_time_limit_stop_is_partial(sync):
    result = sync(exit_code=10, log=real_log("max_duration_exit10"))
    assert result["status"] == "CLOUD_PARTIAL" and result["retryable"] is False
