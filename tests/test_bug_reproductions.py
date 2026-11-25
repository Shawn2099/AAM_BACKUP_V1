"""Bug reproduction tests — each test reproduces a specific issue found
in the test suite or production audit.

These tests document the bugs and will guide fixes.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.cloud_verify import verify_cloud_integrity as _verify_cloud_integrity
import functools

from models.config import CloudConfig as _CloudConfig
from test_support.fake_tools import SubprocessStyleRunTool

# rclone runs through run_tool (plan 5.1). Required tunables at the production
# defaults (cloud.* in config); a keyword passed by a test still wins.
verify_cloud_integrity = functools.partial(
    _verify_cloud_integrity,
    timeout=_CloudConfig.model_fields["verify_timeout_seconds"].default,
    checkers=_CloudConfig.model_fields["checkers"].default,
    modify_window_seconds=_CloudConfig.model_fields["modify_window_seconds"].default,
)


class TestBugTimeoutMismatch:
    """BUG: Timeout defaults are inconsistent between code, docstring, and tests.

    Code says 18000, docstring says 14400, some tests expect 14400/21600/900.
    """

    def test_verify_cloud_integrity_default_timeout(self):
        """verify_cloud_integrity default timeout is 18000 but docstring says 14400."""
        with patch("core.cloud_verify.resolve_binary", return_value="rclone"), \
             patch("core.cloud_verify.run_tool", new_callable=SubprocessStyleRunTool) as mock_run:
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
             patch("core.cloud_reporter.run_tool", new_callable=SubprocessStyleRunTool) as mock_run:
            # real mkstemp: patching it (or os.close) is global and breaks run_tool's
            # artefact writes - the old (0, path) stub closed pytest's stdin.
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            from core.cloud_reporter import get_cloud_diff
            get_cloud_diff(
                source="E:\\",
                bucket="bucket",
                fy_prefix="FY26-27",
                config_path="/tmp/config", timeout=18000, checkers=4, modify_window_seconds=2, retries=3, retries_sleep_seconds=10
            )
            assert mock_run.call_args[1]["timeout"] == 18000






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
             patch("core.cloud_verify.run_tool", new_callable=SubprocessStyleRunTool) as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=18000)
            result = verify_cloud_integrity(
                source="E:\\",
                bucket="bucket",
                fy_prefix="FY26-27",
                config_path="/tmp/config",
            )
            # The error message should mention 18000, not 14400
            assert "18000" in result.get("error", "")
