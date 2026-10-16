"""Direct tests for core/cloud_reporter.py — get_cloud_diff().

Previously this function was always mocked. These tests exercise the
actual parsing logic, error handling, command construction, and temp file
management.

Mocking contract
----------------
get_cloud_diff() creates its OWN temp file (tempfile.mkstemp) and then
OS-closes the returned fd before rclone writes the combined diff into that
path. A test that wants to control the diff content therefore has to
either hand back a *live* descriptor or let the real mkstemp run and put
the content where rclone would.

The earlier revision of this file patched mkstemp to return
``(0, <path>)`` — fd 0 is pytest's stdin. ``os.close(0)`` succeeded for the
first test in the process (silently closing pytest's own stdin) and then
raised ``EBADF`` for every later test, so each subsequent call degraded to
the ``_partial`` error path and 8 of 11 tests failed (order-dependent, not
a production defect).

These tests keep the real mkstemp and emulate rclone through a mocked
subprocess.run side effect that writes the diff file at the path given by
``--combined`` — i.e. exactly where rclone writes it.
"""

import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.cloud_reporter import get_cloud_diff
from test_support.fake_tools import SubprocessStyleRunTool

SOURCE = "E:\\"
BUCKET = "bucket"
FY_PREFIX = "FY26-27"
CONFIG_PATH = "config/rclone.conf"


@contextmanager
def _mock_rclone_check(
    *,
    diff_content=None,
    exit_code=0,
    stderr="",
    drop_diff_file=False,
    exception=None,
):
    """Stub rclone for get_cloud_diff() and emulate `rclone check --combined`.

    The mocked subprocess.run writes `diff_content` into the file named by
    the command's ``--combined`` argument (exactly what rclone does), drops
    it instead when `drop_diff_file` is set (rclone died before writing),
    or raises `exception` before touching the file.
    """
    with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
         patch("core.cloud_reporter.run_tool", new_callable=SubprocessStyleRunTool) as mock_run:
        def _side_effect(cmd, *args, **kwargs):
            diff_path = Path(cmd[cmd.index("--combined") + 1])
            if exception is not None:
                raise exception
            if drop_diff_file:
                diff_path.unlink(missing_ok=True)
            elif diff_content is not None:
                diff_path.write_text(
                    diff_content, encoding="utf-8", errors="replace", newline=""
                )
            return MagicMock(returncode=exit_code, stdout="", stderr=stderr)

        mock_run.side_effect = _side_effect
        yield mock_run


def _call_diff(**kwargs):
    """Run get_cloud_diff with the shared test arguments."""
    params = dict(
        source=SOURCE,
        bucket=BUCKET,
        fy_prefix=FY_PREFIX,
        config_path=CONFIG_PATH,
        # rclone tunables at the cloud.* config defaults
        timeout=18000, checkers=4, modify_window_seconds=2,
        retries=3, retries_sleep_seconds=10,
    )
    params.update(kwargs)
    return get_cloud_diff(**params)


class TestGetCloudDiffParsing:
    """Test the diff file parsing logic directly."""

    def test_parses_all_prefixes(self):
        """+, -, *, = lines are classified correctly."""
        content = (
            "+ added_file.txt\n"
            "- removed_file.txt\n"
            "* modified_file.txt\n"
            "= unchanged_file.txt\n"
        )
        with _mock_rclone_check(diff_content=content):
            result = _call_diff()

        assert result["added"] == ["added_file.txt"]
        assert result["removed"] == ["removed_file.txt"]
        assert result["modified"] == ["modified_file.txt"]
        assert result["unchanged"] == ["unchanged_file.txt"]

    def test_empty_diff_file(self):
        """Empty diff file → all empty lists, and NOT flagged partial."""
        with _mock_rclone_check(diff_content=""):
            result = _call_diff()

        assert result["added"] == []
        assert result["removed"] == []
        assert result["modified"] == []
        assert result["unchanged"] == []
        assert "_partial" not in result

    def test_blank_lines_skipped(self):
        """Blank / whitespace-only lines in diff file are skipped."""
        content = "\n+ file1.txt\n   \n- file2.txt\n\n"
        with _mock_rclone_check(diff_content=content):
            result = _call_diff()

        assert result["added"] == ["file1.txt"]
        assert result["removed"] == ["file2.txt"]

    def test_dot_paths_filtered(self):
        """Paths that normalize to '.' are skipped; './x' still normalizes to 'x'."""
        content = "+ .\n- ./file.txt\n+ valid.txt\n"
        with _mock_rclone_check(diff_content=content):
            result = _call_diff()

        assert result["added"] == ["valid.txt"]
        assert result["removed"] == ["file.txt"]

    def test_short_lines_skipped(self):
        """Lines shorter than 3 chars (no '<marker> ' separator) are skipped."""
        content = "x\nxy\n+ valid.txt\n"
        with _mock_rclone_check(diff_content=content):
            result = _call_diff()

        assert result["added"] == ["valid.txt"]

    def test_unknown_marker_ignored(self):
        """Lines with unknown markers (not +,-,*,=) are ignored."""
        content = "? unknown.txt\n+ valid.txt\n"
        with _mock_rclone_check(diff_content=content):
            result = _call_diff()

        assert result["added"] == ["valid.txt"]

    def test_leading_slash_stripped(self):
        """rclone sometimes emits a rooted relative path — the '/' is stripped."""
        content = "+ /sub/dir/file.txt\n"
        with _mock_rclone_check(diff_content=content):
            result = _call_diff()

        assert result["added"] == ["sub/dir/file.txt"]


class TestGetCloudDiffErrors:
    """Error paths in get_cloud_diff."""

    def test_rclone_exit_2_sets_partial(self):
        """rclone exit >= 2 → _partial=True and _error set, partial data still parsed."""
        with _mock_rclone_check(
            diff_content="+ partial_file.txt\n", exit_code=2, stderr="some error"
        ) as mock_run:
            result = _call_diff()

        assert result["_partial"] is True
        assert "exited 2" in result["_error"]
        assert "some error" in result["_error"]
        # Partial data still parsed
        assert result["added"] == ["partial_file.txt"]
        assert mock_run.called

    def test_rclone_exit_1_is_not_partial(self):
        """rclone exit 1 means 'differences found' — that is data, not an error."""
        with _mock_rclone_check(diff_content="+ file1.txt\n", exit_code=1):
            result = _call_diff()

        assert "_partial" not in result
        assert "_error" not in result
        assert result["added"] == ["file1.txt"]

    def test_timeout_returns_partial(self):
        """TimeoutExpired → _partial=True, _error set."""
        with _mock_rclone_check(
            exception=subprocess.TimeoutExpired(cmd="rclone", timeout=100)
        ):
            result = _call_diff(timeout=100)

        assert result["_partial"] is True
        assert "timed out" in result["_error"]
        assert result["added"] == []

    def test_os_error_returns_partial(self):
        """OSError → _partial=True, _error set."""
        with _mock_rclone_check(exception=OSError("Access denied")):
            result = _call_diff()

        assert result["_partial"] is True
        assert "Access denied" in result["_error"]
        assert result["added"] == []

    def test_missing_diff_file(self):
        """Diff file absent after rclone → warning, empty diff, no _partial."""
        with _mock_rclone_check(drop_diff_file=True):
            result = _call_diff()

        assert result["added"] == []
        assert result["removed"] == []
        assert result["modified"] == []
        assert result["unchanged"] == []
        # A missing diff file on a clean exit is NOT an rclone error
        assert "_partial" not in result


class TestGetCloudDiffCommand:
    """Verify command construction and temp file management."""

    def test_command_construction(self):
        """Command uses `check`, --combined <tempfile>, and the size-only flags."""
        seen = {}

        with _mock_rclone_check(diff_content="") as mock_run:
            def _capture(cmd, *args, **kwargs):
                seen["diff_file"] = cmd[cmd.index("--combined") + 1]
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = _capture
            _call_diff()

        cmd = mock_run.call_args[0][0]
        assert "check" in cmd
        assert cmd[0] == "rclone"
        assert cmd[cmd.index("check") + 1] == SOURCE
        assert f"aam_gcs:{BUCKET}/{FY_PREFIX}" in cmd
        assert "--combined" in cmd
        assert cmd[cmd.index("--combined") + 1] == seen["diff_file"]
        assert "--size-only" in cmd
        assert "--modify-window" in cmd
        assert cmd[cmd.index("--modify-window") + 1] == "2s"
        assert "--checkers" in cmd
        assert cmd[cmd.index("--checkers") + 1] == "4"
        assert "--retries" in cmd
        assert cmd[cmd.index("--retries") + 1] == "3"
        assert "--retries-sleep" in cmd
        assert "10s" in cmd
        assert "--config" in cmd
        assert CONFIG_PATH in cmd
        assert "--gcs-no-check-bucket" in cmd
        assert "--fast-list" in cmd

    def test_temp_file_cleaned_up(self):
        """The mkstemp diff file is removed in the finally block."""
        with _mock_rclone_check(diff_content="+ file.txt\n") as mock_run:
            _call_diff()
            cmd = mock_run.call_args[0][0]
            diff_file = Path(cmd[cmd.index("--combined") + 1])
            # rclone "wrote" it, so the parser must have seen it
            assert not diff_file.exists(), "diff file leaked after a successful run"

        assert diff_file.name.startswith("cloud_diff_")

    def test_temp_file_cleaned_up_on_error(self):
        """The mkstemp diff file is also removed when rclone raises."""
        seen = []

        def _side_effect(cmd, *args, **kwargs):
            seen.append(Path(cmd[cmd.index("--combined") + 1]))
            raise subprocess.TimeoutExpired(cmd="rclone", timeout=100)

        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.run_tool", new_callable=SubprocessStyleRunTool) as mock_run:
            mock_run.side_effect = _side_effect
            result = _call_diff(timeout=100)

        assert result["_partial"] is True
        assert len(seen) == 1
        assert not seen[0].exists(), "diff file leaked after a failed run"
