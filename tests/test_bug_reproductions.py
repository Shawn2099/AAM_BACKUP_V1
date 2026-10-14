"""Bug reproduction tests — each test reproduces a specific issue found
in the test suite or production audit.

These tests document the bugs and will guide fixes.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.cloud_verify import verify_cloud_integrity
from core.lan_sync import classify_exit_code, decide_lan_result


class TestBugTimeoutMismatch:
    """BUG: Timeout defaults are inconsistent between code, docstring, and tests.

    Code says 18000, docstring says 14400, some tests expect 14400/21600/900.
    """

    def test_verify_cloud_integrity_default_timeout(self):
        """verify_cloud_integrity default timeout is 18000 but docstring says 14400."""
        with patch("core.cloud_verify.resolve_binary", return_value="rclone"), \
             patch("core.cloud_verify.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            verify_cloud_integrity(
                source="E:\\",
                bucket="bucket",
                fy_prefix="FY26-27",
                config_path="/tmp/config",
            )
            # The actual default is 18000
            assert mock_run.call_args[1]["timeout"] == 18000

    def test_get_cloud_diff_default_timeout(self):
        """get_cloud_diff default timeout is 18000."""
        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.subprocess.run") as mock_run, \
             patch("core.cloud_reporter.tempfile.mkstemp", return_value=(0, "/tmp/diff.txt")):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            from core.cloud_reporter import get_cloud_diff
            get_cloud_diff(
                source="E:\\",
                bucket="bucket",
                fy_prefix="FY26-27",
                config_path="/tmp/config",
            )
            assert mock_run.call_args[1]["timeout"] == 18000


class TestBugClassifyExitCodeBit2:
    """Reported "classify_exit_code doesn't check bit 2" — ANALYZED AND REJECTED.

    The original report expected classify_exit_code(4..7) == LAN_PARTIAL.
    The shipped contract (documented in classify_exit_code's docstring and
    in decide_lan_result's BUG-05 analysis) is the opposite and is correct:

      * Robocopy exit codes are a bitmask. Bit 2 (4) means *mismatched/
        extra files detected, no copy errors* — the destination copy is
        intact; nothing failed to transfer.
      * classify_exit_code is only the NO-LOG fallback (decide_lan_result is
        authoritative whenever a job log exists), so it must not invent
        severity: codes 0-7 -> LAN_COMPLETE, 8-15 (bit 3) -> LAN_PARTIAL,
        16+ (bit 4) -> LAN_FAILED.
      * Live adversarial evidence (campaign T1) proved RC 4-7 unreachable in
        a real /MIR run, so re-mapping the band to PARTIAL would fire alerts
        for a passing copy.

    These tests now pin the REAL contract so the bit-2 band can never be
    silently promoted to a failure.
    """

    def test_classify_exit_code_4_is_complete(self):
        """Exit code 4 (bit 2: mismatched files, no copy errors) → LAN_COMPLETE."""
        assert classify_exit_code(4) == "LAN_COMPLETE"

    def test_classify_exit_code_5_is_complete(self):
        """Exit code 5 (bits 0+2) → LAN_COMPLETE."""
        assert classify_exit_code(5) == "LAN_COMPLETE"

    def test_classify_exit_code_6_is_complete(self):
        """Exit code 6 (bits 1+2) → LAN_COMPLETE."""
        assert classify_exit_code(6) == "LAN_COMPLETE"

    def test_classify_exit_code_7_is_complete(self):
        """Exit code 7 (bits 0+1+2) → LAN_COMPLETE."""
        assert classify_exit_code(7) == "LAN_COMPLETE"

    def test_classify_exit_code_0_complete(self):
        """Exit code 0 → LAN_COMPLETE."""
        assert classify_exit_code(0) == "LAN_COMPLETE"

    def test_classify_exit_code_1_complete(self):
        """Exit code 1 (bit 0: files copied) → LAN_COMPLETE."""
        assert classify_exit_code(1) == "LAN_COMPLETE"

    def test_classify_exit_code_2_complete(self):
        """Exit code 2 (bit 1: extras) → LAN_COMPLETE."""
        assert classify_exit_code(2) == "LAN_COMPLETE"

    def test_classify_exit_code_3_complete(self):
        """Exit code 3 (bits 0+1) → LAN_COMPLETE."""
        assert classify_exit_code(3) == "LAN_COMPLETE"

    def test_classify_exit_code_8_partial(self):
        """Exit code 8 (bit 3: copy errors) → LAN_PARTIAL."""
        assert classify_exit_code(8) == "LAN_PARTIAL"

    def test_classify_exit_code_16_failed(self):
        """Exit code 16 (bit 4: fatal) → LAN_FAILED."""
        assert classify_exit_code(16) == "LAN_FAILED"


class TestBugDecideLanResultBit2:
    """Reported "decide_lan_result doesn't check bit 2" — ANALYZED AND REJECTED.

    The original report expected decide_lan_result(4..7, complete_log) ==
    LAN_PARTIAL. The shipped contract (decide_lan_result docstring, BUG-05
    analysis) says the opposite, and it is deliberate:

      * With a COMPLETED job summary, FAILED=0 and no bit 3, exit 4-7 is a
        finished /MIR pass with a mismatch/extras ANOMALY — not a failed
        transfer: status LAN_COMPLETE, termination "normal", reason
        "robocopy mismatches/extras (exit N)".
      * Bit 3 (copy errors) is what demotes a run to LAN_PARTIAL; bit 4
        (fatal) to LAN_FAILED; a log without a completed summary to
        LAN_SUSPECT; and contradictory evidence (FAILED>0 / markers) fails
        closed to LAN_SUSPECT even inside the 0-7 band.
      * Live adversarial evidence (campaign T1) showed RC 4-7 is
        unreachable in a real /MIR run, so a PARTIAL mapping would fire
        alerts (PartialRun + optional NAS shutdown) for a passing copy.

    These tests pin the REAL contract for the bit-2 band.
    """

    def _make_complete_log(self):
        """Create a log with completed summary."""
        return (
            "Files :        10        10        0        0        0        0\n"
            "Ended : Mon Jan 01 00:00:00 2026\n"
        )

    def test_exit_4_with_complete_log_is_complete(self):
        """Exit code 4 + complete log → LAN_COMPLETE with the anomaly reason."""
        log = self._make_complete_log()
        result = decide_lan_result(4, log)
        assert result["status"] == "LAN_COMPLETE"
        assert result["termination"] == "normal"
        assert result["log_complete"] is True
        assert result["reason"] == "robocopy mismatches/extras (exit 4)"

    def test_exit_5_with_complete_log_is_complete(self):
        """Exit code 5 + complete log → LAN_COMPLETE with the anomaly reason."""
        log = self._make_complete_log()
        result = decide_lan_result(5, log)
        assert result["status"] == "LAN_COMPLETE"
        assert result["reason"] == "robocopy mismatches/extras (exit 5)"

    def test_exit_6_with_complete_log_is_complete(self):
        """Exit code 6 + complete log → LAN_COMPLETE with the anomaly reason."""
        log = self._make_complete_log()
        result = decide_lan_result(6, log)
        assert result["status"] == "LAN_COMPLETE"
        assert result["reason"] == "robocopy mismatches/extras (exit 6)"

    def test_exit_7_with_complete_log_is_complete(self):
        """Exit code 7 + complete log → LAN_COMPLETE with the anomaly reason."""
        log = self._make_complete_log()
        result = decide_lan_result(7, log)
        assert result["status"] == "LAN_COMPLETE"
        assert result["reason"] == "robocopy mismatches/extras (exit 7)"


class TestBugFlowNoneGuard:
    """BUG: flow.py line 722 — diff.get("added", []) returns None if key exists with None value.

    This would cause TypeError: object of type 'NoneType' has no len().
    """

    def test_diff_with_none_added_causes_type_error(self):
        """Simulate the bug: diff dict with added=None."""
        diff = {"added": None, "modified": [], "unchanged": []}
        # This is what flow.py does:
        with pytest.raises(TypeError):
            len(diff.get("added", []))

    def test_diff_with_none_added_safe_guard(self):
        """The fix: use `or []` instead of default."""
        diff = {"added": None, "modified": [], "unchanged": []}
        # Safe version:
        result = len(diff.get("added") or [])
        assert result == 0


class TestBugVerifyTimeoutMessage:
    """BUG: Timeout message says 14400 but actual timeout is 18000."""

    def test_timeout_message_matches_actual_timeout(self):
        """The timeout error message should match the actual timeout value."""
        with patch("core.cloud_verify.resolve_binary", return_value="rclone"), \
             patch("core.cloud_verify.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=18000)
            result = verify_cloud_integrity(
                source="E:\\",
                bucket="bucket",
                fy_prefix="FY26-27",
                config_path="/tmp/config",
            )
            # The error message should mention 18000, not 14400
            assert "18000" in result.get("error", "")
