"""Transfer-result contract tests: normal vs abnormal robocopy termination.

T04/A1 regression core: a killed robocopy inherits exit 0-3 with a
truncated log. The contract must classify that SUSPECT (never COMPLETE)
by requiring the log's own completed job summary.
"""

from core.lan_sync import (
    classify_exit_code,
    decide_lan_result,
    log_shows_completed_summary,
    summarize_log_counts,
)

CLEAN_LOG = """\
          New File            1234    D:\\src\\a.txt
          New File            5678    D:\\src\\b.txt
 100%        New File            1234    D:\\src\\a.txt
------------------------------------------------------------------------------
               Total    Copied   Skipped  Mismatch    FAILED    Extras
    Dirs :         3         1         2         0         0         0
   Files :         2         2         0         0         0         0
   Bytes :      6.75 k         0         0         0         0         0
   Times :   0:00:01   0:00:00                       0:00:00   0:00:00
   Ended : Saturday, September 06, 2026 1:00:01 AM
"""

# Simulates external kill mid-transfer: per-file lines, NO summary table.
TRUNCATED_LOG = """\
          New File            1234    D:\\src\\a.txt
  16%        New File        8589934592    D:\\src\\big.bin
"""

ERROR_LOG = CLEAN_LOG.replace(
    "   Files :         2         2         0         0         0         0",
    "   Files :         3         1         1         0         1         0",
) + "  ** FAILED: D:\\src\\c.txt\n"


class TestLogCompletionSignal:
    def test_clean_log_shows_completed_summary(self):
        assert log_shows_completed_summary(CLEAN_LOG) is True

    def test_truncated_log_has_no_summary(self):
        assert log_shows_completed_summary(TRUNCATED_LOG) is False

    def test_empty_log_has_no_summary(self):
        assert log_shows_completed_summary("") is False

    def test_summary_counts_parsed_positionally(self):
        counts = summarize_log_counts(CLEAN_LOG)
        assert counts == {
            "total": 2, "copied": 2, "skipped": 0,
            "mismatch": 0, "failed": 0, "extras": 0,
        }

    def test_truncated_log_has_no_counts(self):
        assert summarize_log_counts(TRUNCATED_LOG) is None


class TestDecideLanResult:
    def test_clean_exit1_is_complete(self):
        r = decide_lan_result(1, CLEAN_LOG)
        assert r["status"] == "LAN_COMPLETE"
        assert r["termination"] == "normal"
        assert r["log_complete"] is True
        assert r["counts"]["copied"] == 2

    def test_clean_exit0_is_complete(self):
        assert decide_lan_result(0, CLEAN_LOG)["status"] == "LAN_COMPLETE"

    def test_killed_exit1_truncated_log_is_suspect(self):
        # T04/A1: THE regression — exit 1 must not mean success.
        for code in (0, 1, 2, 3):
            r = decide_lan_result(code, TRUNCATED_LOG)
            assert r["status"] == "LAN_SUSPECT", f"exit {code}"
            assert r["termination"] == "abnormal"

    def test_killed_wmi_exit0_truncated_log_is_suspect(self):
        r = decide_lan_result(0, TRUNCATED_LOG)
        assert r["status"] == "LAN_SUSPECT"

    def test_hard_failure_is_failed(self):
        r = decide_lan_result(16, CLEAN_LOG)
        assert r["status"] == "LAN_FAILED"
        assert r["termination"] == "abnormal"

    def test_timeout_sentinel_is_failed(self):
        r = decide_lan_result(-1, "")
        assert r["status"] == "LAN_FAILED"

    def test_copy_errors_are_partial(self):
        r = decide_lan_result(11, ERROR_LOG)
        assert r["status"] == "LAN_PARTIAL"
        assert r["termination"] == "normal"

    def test_contradictory_success_is_suspect(self):
        # Exit says success but the log's own counters show failures.
        r = decide_lan_result(1, ERROR_LOG)
        assert r["status"] == "LAN_SUSPECT"

    def test_pure_exit_classifier_unchanged(self):
        # classify_exit_code keeps its documented bitmask mapping (used by
        # fy_rollover); the contract layer adds the log evidence on top.
        assert classify_exit_code(1) == "LAN_COMPLETE"
        assert classify_exit_code(8) == "LAN_PARTIAL"
        assert classify_exit_code(16) == "LAN_FAILED"
