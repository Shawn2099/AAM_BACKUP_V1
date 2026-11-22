"""Card U14 (agent_runs/TEST_DESIGN_RELEASE.md): what rclone's log says about auth and throttling.

The rclone JSON log is the only evidence of WHY a cloud sync failed. These cases pin the verdict
for four hand-written logs. The oracle is the table below, written by hand from the rclone behaviour
the code documents (rclone's own --retries loop, the final stats line, exit codes 0/1/7), never
computed from the code under test.

  (a) 429 throttling inside an attempt rclone retried, final attempt clean, stats errors=0, exit 0
      -> CLOUD_COMPLETE (the earlier 429s belong to an attempt that was already retried)
  (b) the same, but the final stats report errors=2, exit 0 -> CLOUD_SUSPECT
  (c) 401 Invalid Credentials, stats fatalError=true, exit 1 -> CLOUD_FAILED, not retried
  (d) 403 storage.objects.create denied, exit 7 -> CLOUD_FAILED, not retried, cause names permission

Runs go through run_cloud_sync with the real rclone process faked by fake_run_tool, so the real
decision code runs unchanged. Nothing reaches the network.
"""

from __future__ import annotations

import json

import pytest

from core.cloud_sync import (classify_rclone_exit, rclone_retryable, run_cloud_sync,
                             scan_rclone_log_for_errors)
from core.run_artefacts import RunArtefacts
from test_support.fake_tools import fake_run_tool


def _line(level: str, msg: str, **extra) -> str:
    return json.dumps({"level": level, "msg": msg, **extra})


def _stats(errors: int, fatal: bool) -> str:
    return _line("info", "stats", stats={"transfers": 1, "deletes": 0, "deletedDirs": 0,
                                         "renames": 0, "errors": errors, "fatalError": fatal})


THROTTLED_429 = "googleapi: Error 429: rateLimitExceeded"
CASE_A_LOG = "\n".join([
    _line("error", f"Failed to copy: {THROTTLED_429}", object="a.xlsx"),
    _line("error", f"Failed to copy: {THROTTLED_429}", object="b.xlsx"),
    _line("error", f"Failed to copy: {THROTTLED_429}", object="c.xlsx"),
    _line("notice", "Attempt 1/3 failed with 3 errors and: " + THROTTLED_429),
    _line("info", "Copied (new)", object="a.xlsx"),
    _line("notice", "Attempt 2/3 succeeded"),
    _stats(errors=0, fatal=False),
])
CASE_B_LOG = CASE_A_LOG.replace(_stats(0, False), _stats(2, False))
CASE_C_LOG = "\n".join([
    _line("error", "Failed to create file system: googleapi: Error 401: Invalid Credentials, authError"),
    _stats(errors=1, fatal=True),
])
CASE_D_LOG = "\n".join([
    _line("error", "Failed to copy: googleapi: Error 403: forbidden - the service account "
                   "does not have storage.objects.create access to the bucket"),
    _stats(errors=1, fatal=True),
])
# the stats dicts as the rclone log states them (hand-written, not parsed by the code under test)
STATS_C = {"transfers": 0, "deletes": 0, "deletedDirs": 0, "renames": 0, "errors": 1,
           "fatalError": True}
STATS_D = dict(STATS_C)


def _run(tmp_path, exit_code: int, log: str) -> dict:
    src = tmp_path / "source" / "FY26-27"
    src.mkdir(parents=True, exist_ok=True)
    (src / "a.xlsx").write_bytes(b"x")
    art = RunArtefacts(tmp_path / "rt" / "run_logs", "run-u14-cloud", day="2026-11-22")
    with fake_run_tool("core.cloud_sync", exit_code=exit_code, log=log) as calls:
        result = run_cloud_sync(
            source=str(src), bucket="aam-test-sandbox-u14", fy_prefix="FY26-27",
            gcs_key_path="sandbox-key.json", project_number="0", storage_class="COLDLINE",
            artefacts=art,
        )
    assert len(calls) == 1
    return result


def test_case_a_throttled_then_clean_final_attempt_is_complete(tmp_path):
    result = _run(tmp_path, 0, CASE_A_LOG)
    assert result["status"] == "CLOUD_COMPLETE"
    assert result["suspect_reason"] is None
    assert result["rclone_failed_attempts"] == 1          # the one "Attempt 1/3 failed" line
    has_error, tail = scan_rclone_log_for_errors(CASE_A_LOG)
    assert has_error is False and tail == ""
    assert classify_rclone_exit(0) == "CLOUD_COMPLETE"


def test_case_b_clean_exit_with_final_stats_errors_is_suspect(tmp_path):
    result = _run(tmp_path, 0, CASE_B_LOG)
    assert result["status"] == "CLOUD_SUSPECT"
    assert "final stats report 2 error(s)" in result["error"]
    assert result["exit_code"] == 0


def test_case_c_invalid_credentials_fatal_is_failed_not_retried(tmp_path):
    has_error, tail = scan_rclone_log_for_errors(CASE_C_LOG)
    assert has_error is True and "Error 401: Invalid Credentials" in tail
    result = _run(tmp_path, 1, CASE_C_LOG)
    assert result["status"] == "CLOUD_FAILED"
    assert result["retryable"] is False
    assert result["transport_category"] == "AUTH"
    assert "Invalid Credentials" in result["error"]
    # the same exit 1 WITHOUT rclone's fatal flag may be repeated: the flag is what blocks the retry
    assert rclone_retryable(1, STATS_C, max_delete_reached=False) is False
    assert rclone_retryable(1, {**STATS_C, "fatalError": False}, max_delete_reached=False) is True


def test_case_d_permission_denied_exit_7_is_failed_not_retried(tmp_path):
    has_error, tail = scan_rclone_log_for_errors(CASE_D_LOG)
    assert has_error is True and "storage.objects.create" in tail
    result = _run(tmp_path, 7, CASE_D_LOG)
    assert classify_rclone_exit(7) == "CLOUD_FAILED"
    assert result["status"] == "CLOUD_FAILED"
    assert result["retryable"] is False
    assert result["transport_category"] == "AUTH"
    assert "storage.objects.create" in result["error"]
    assert rclone_retryable(7, STATS_D, max_delete_reached=False) is False


@pytest.mark.parametrize("code, status", [
    (0, "CLOUD_COMPLETE"), (1, "CLOUD_FAILED"), (4, "CLOUD_PARTIAL"), (5, "CLOUD_PARTIAL"),
    (7, "CLOUD_FAILED"), (9, "CLOUD_NO_CHANGES_COMPLETE"),
])
def test_exit_code_table_hand_values(code, status):
    assert classify_rclone_exit(code) == status
