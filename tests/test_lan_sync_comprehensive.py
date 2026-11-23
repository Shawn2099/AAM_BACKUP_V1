"""Comprehensive tests for core/lan_sync.py — all functions and edge scenarios."""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.lan_sync import (
    ROBOCOPY_LOG_PREFIX,
    build_robocopy_command,
    classify_exit_code,
    cleanup_orphaned_robocopy_logs,
    count_failed_lines,
    decide_lan_result,
    failed_file_count,
    log_shows_completed_summary,
    run_lan_sync,
    summarize_log_counts,
    _summary_files_row_values,
    _validate_required_flags,
    _read_log_tail,
)


class TestValidateRequiredFlags:
    def test_nc_flag_rejected(self):
        with pytest.raises(ValueError, match="/NC"):
            _validate_required_flags(["/NC"])

    def test_nc_flag_case_insensitive(self):
        with pytest.raises(ValueError, match="/NC"):
            _validate_required_flags(["/nc"])

    def test_valid_flags_pass(self):
        _validate_required_flags(["/MIR", "/V", "/TS"])


class TestBuildRobocopyCommand:
    def test_basic_command(self):
        from models.config import LanConfig
        config = LanConfig()
        cmd = build_robocopy_command("E:\\", "\\\\nas\\share", config)
        # resolve_binary() returns the full path to the real binary
        # (deploy\bin\robocopy.exe or system PATH), not the bare name.
        assert cmd[0].lower().endswith("robocopy.exe")
        assert "E:\\" in cmd
        assert "\\\\nas\\share" in cmd
        assert "/MIR" in cmd
        assert "/MT:4" in cmd
        assert "/R:3" in cmd
        assert "/W:10" in cmd
        assert "/V" in cmd
        assert "/TS" in cmd
        assert "/FP" in cmd

    def test_custom_threads(self):
        from models.config import LanConfig
        config = LanConfig(mt_threads=8)
        cmd = build_robocopy_command("E:\\", "\\\\nas\\share", config)
        assert "/MT:8" in cmd

    def test_excludes_canary(self):
        from models.config import LanConfig
        config = LanConfig()
        cmd = build_robocopy_command("E:\\", "\\\\nas\\share", config)
        assert ".AAM_TARGET_MOUNTED" in cmd


class TestSummaryFilesRowValues:
    def test_valid_summary(self):
        log = "Files : 10 8 0 1 2 3\n"
        result = _summary_files_row_values(log)
        assert result == [10, 8, 0, 1, 2, 3]

    def test_no_summary(self):
        result = _summary_files_row_values("no summary here")
        assert result is None

    def test_empty_log(self):
        result = _summary_files_row_values("")
        assert result is None

    def test_non_numeric_columns(self):
        log = "Files : 10 abc 0 0 0 0\n"
        result = _summary_files_row_values(log)
        assert result is None

    def test_comma_separated(self):
        log = "Files : 1,000 800 0 0 0 0\n"
        result = _summary_files_row_values(log)
        assert result == [1000, 800, 0, 0, 0, 0]


class TestSummarizeLogCounts:
    def test_valid_counts(self):
        log = "Files : 10 8 0 1 2 3\n"
        result = summarize_log_counts(log)
        assert result == {"total": 10, "copied": 8, "skipped": 0, "mismatch": 1, "failed": 2, "extras": 3}

    def test_no_summary(self):
        result = summarize_log_counts("no summary")
        assert result is None


class TestLogShowsCompletedSummary:
    def test_complete_log(self):
        log = "Files : 10 10 0 0 0 0\nEnded : Mon Jan 01 00:00:00 2026\n"
        assert log_shows_completed_summary(log) is True

    def test_missing_ended_line(self):
        log = "Files : 10 10 0 0 0 0\n"
        assert log_shows_completed_summary(log) is False

    def test_missing_files_row(self):
        log = "Ended : Mon Jan 01 00:00:00 2026\n"
        assert log_shows_completed_summary(log) is False

    def test_empty_log(self):
        assert log_shows_completed_summary("") is False

    def test_none_log(self):
        assert log_shows_completed_summary(None) is False


class TestReadLogTail:
    def test_small_file_read_fully(self, tmp_path):
        f = tmp_path / "small.log"
        f.write_text("hello world")
        result = _read_log_tail(f, 1000)
        assert result == "hello world"

    def test_large_file_reads_tail(self, tmp_path):
        f = tmp_path / "large.log"
        f.write_bytes(b"x" * 2000)
        result = _read_log_tail(f, 100)
        assert len(result) <= 100

    def test_nonexistent_file(self, tmp_path):
        result = _read_log_tail(tmp_path / "nonexistent.log", 100)
        assert "unreadable" in result


class TestCleanupOrphanedLogs:
    def test_cleanup_old_logs(self, tmp_path):
        """Old log files are removed."""
        import time
        old_log = tmp_path / f"{ROBOCOPY_LOG_PREFIX}old.log"
        old_log.write_text("old")
        # Set mtime to 25 hours ago
        old_time = time.time() - 25 * 3600
        os.utime(old_log, (old_time, old_time))

        # Production scans tempfile.gettempdir() — point it at tmp_path so the
        # real glob/age-filter/unlink logic runs hermetically.
        with patch("core.lan_sync.tempfile.gettempdir", return_value=str(tmp_path)):
            result = cleanup_orphaned_robocopy_logs(max_age_hours=24)
        assert result >= 1
        assert not old_log.exists()

    def test_cleanup_keeps_recent_logs(self, tmp_path):
        """Recent log files are kept."""
        import time
        recent_log = tmp_path / f"{ROBOCOPY_LOG_PREFIX}recent.log"
        recent_log.write_text("recent")
        # Set mtime to 1 hour ago
        old_time = time.time() - 3600
        os.utime(recent_log, (old_time, old_time))

        # Same tempdir indirection as test_cleanup_old_logs — without it the
        # scan never sees this file and the test passes vacuously.
        with patch("core.lan_sync.tempfile.gettempdir", return_value=str(tmp_path)):
            result = cleanup_orphaned_robocopy_logs(max_age_hours=24)
        assert recent_log.exists()


class TestRunLanSync:
    """run_lan_sync contract tests (M1 1.5: shared tool runner + full logs).

    The fake runner writes a real-format robocopy /UNILOG (UTF-16) into the
    path the module reserved, so the real UTF-16 tail reader, summary parser
    and decide_lan_result all run unchanged.
    """

    KEYS = {"status", "exit_code", "error", "anomaly_details", "files_failed",
            "termination", "log_complete", "counts", "log_retained",
            "bytes", "files_deleted"}           # WP6: the tool's own accounting

    @pytest.fixture(autouse=True)
    def _art(self, tmp_path):
        from core.run_artefacts import RunArtefacts

        self.art = RunArtefacts(tmp_path / "run_logs", "run-x-lan", day="2026-10-14")
        with patch("core.lan_sync.resolve_binary", return_value="robocopy"), \
             patch("core.lan_sync.require_source_files"):
            yield

    @staticmethod
    def _config():
        from models.config import LanConfig
        config = LanConfig()
        config.subprocess_timeout_seconds = 60
        return config

    def _run(self, **fake):
        from test_support.fake_tools import fake_run_tool

        fake.setdefault("log_encoding", "utf-16")
        with fake_run_tool("core.lan_sync", **fake) as calls:
            result = run_lan_sync("E:\\", "\\\\nas\\share", self._config(), artefacts=self.art)
        self.calls = calls
        return result

    def test_successful_sync(self):
        from test_support.fake_tools import robocopy_log

        result = self._run(exit_code=1, log=robocopy_log(total=10, copied=10))
        assert result["status"] == "LAN_COMPLETE"
        assert result["exit_code"] == 1
        assert result["termination"] == "normal" and result["log_complete"] is True
        assert result["counts"] == {"total": 10, "copied": 10, "skipped": 0,
                                    "mismatch": 0, "failed": 0, "extras": 0}
        assert result["error"] is None and result["anomaly_details"] is None
        assert result["files_failed"] == 0
        assert set(result) == self.KEYS

    def test_full_log_is_kept_and_unilog_flag_used(self):
        from test_support.fake_tools import robocopy_log

        result = self._run(exit_code=1, log=robocopy_log(body="New File  C:\\Données\\été.txt\n"))
        log_path = Path(result["log_retained"])
        assert log_path.parent == self.art.dir and log_path.exists()
        assert "été.txt" in log_path.read_text(encoding="utf-16")
        assert f"/UNILOG:{log_path}" in self.calls[0]["argv"]
        assert not any(a.upper().startswith("/LOG:") for a in self.calls[0]["argv"])

    def test_sync_with_copy_errors(self):
        from test_support.fake_tools import robocopy_log

        result = self._run(exit_code=8, log=robocopy_log(total=10, copied=8, failed=2))
        assert result["status"] == "LAN_PARTIAL"
        assert result["exit_code"] == 8
        assert result["termination"] == "normal" and result["log_complete"] is True
        assert result["files_failed"] == 2
        assert result["error"] and result["anomaly_details"] is None

    def test_sync_timeout(self):
        result = self._run(timed_out=True)
        assert result["status"] == "LAN_FAILED"
        assert result["exit_code"] == -1
        assert result["termination"] == "abnormal" and result["log_complete"] is False
        assert result["counts"] is None and result["files_failed"] == 0
        assert "Timeout after 60s" in result["error"]
        assert result["log_retained"]

    def test_missing_robocopy(self):
        result = self._run(spawn_error="FileNotFoundError: [WinError 2]")
        assert result["status"] == "LAN_FAILED"
        assert "robocopy.exe not found" in result["error"]

    def test_sync_fatal_error(self):
        result = self._run(exit_code=16, log="ERROR : Invalid Parameter #3")
        assert result["status"] == "LAN_FAILED"
        assert result["exit_code"] == 16
        assert result["termination"] == "abnormal" and result["log_complete"] is False
        assert result["files_failed"] == 0
        assert "Invalid Parameter" in result["error"]

    def test_sync_killed_truncated_log(self):
        result = self._run(exit_code=1, log="truncated log without summary")
        assert result["status"] == "LAN_SUSPECT"
        assert result["termination"] == "abnormal" and result["log_complete"] is False
        assert "WITHOUT a completed job summary" in result["error"]
        assert result["files_failed"] == 0
        assert Path(result["log_retained"]).exists()

    def test_tail_size_comes_from_config(self):
        from test_support.fake_tools import robocopy_log

        big = robocopy_log(total=1, copied=0, failed=1, body="x" * 400_000)
        result = self._run(exit_code=8, log=big)
        # log_tail_kb default 100 KB of UTF-16 bytes -> ~51,200 chars
        assert len(result["error"]) <= 100 * 1024 // 2 + 1
        assert "Ended :" in result["error"]


