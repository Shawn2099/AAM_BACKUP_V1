"""F-08 verification-liveness regression tests.

P8-VERIFY-ONLY proved a running `rclone check` killed via
TerminateProcess(pid, 0) reports exit code 0 while its log is truncated
mid-check — and the old `verified = (returncode == 0)` logic recorded
CLOUD_NO_CHANGES_COMPLETE / verified=true / verify_liveness=true over it.

These tests pin the completion contract: VERIFIED requires the check
process's OWN completion summary, never the killable exit code alone.
"""

import ctypes
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.cloud_verify import decide_cloud_verify_result, verify_cloud_integrity

RCLONE = shutil.which("rclone")
needs_rclone = pytest.mark.skipif(not RCLONE, reason="rclone binary not available")
needs_windows_kill = pytest.mark.skipif(
    sys.platform != "win32", reason="TerminateProcess forgery is Windows-specific"
)

CLEAN_STDERR = (
    "2026/09/06 18:18:48 NOTICE: Local file system at //?/C:/dst: 0 differences found\n"
    "2026/09/06 18:18:48 NOTICE: Local file system at //?/C:/dst: 1 matching files\n"
)
DIVERGED_STDERR = (
    "2026/09/06 18:18:52 ERROR : f1.txt: sizes differ\n"
    "2026/09/06 18:18:52 NOTICE: Local file system at //?/C:/dst: 1 differences found\n"
    "2026/09/06 18:18:52 NOTICE: Local file system at //?/C:/dst: 1 errors while checking\n"
    "2026/09/06 18:18:52 NOTICE: Failed to check: 1 differences found\n"
)
# Truncated log of a check killed mid-run: startup lines only, no summary.
TRUNCATED_STDERR = (
    "2026/09/06 18:20:49 NOTICE: Local file system at //?/C:/dst: checking 3004 files\n"
)


def _mock_run(returncode=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestDecideCloudVerifyResult:
    def test_clean_completed_run_verifies(self):
        d = decide_cloud_verify_result(0, CLEAN_STDERR)
        assert d == {
            "verified": True,
            "termination": "normal",
            "completion": True,
            "differences": 0,
            "reason": None,
        }

    def test_killed_check_with_forged_exit_0_never_verifies(self):
        # Exact F-08 shape: exit 0, truncated log, clean destination data.
        d = decide_cloud_verify_result(0, TRUNCATED_STDERR)
        assert d["verified"] is False
        assert d["termination"] == "abnormal"
        assert d["completion"] is False
        assert d["differences"] is None

    def test_killed_check_empty_log_never_verifies(self):
        d = decide_cloud_verify_result(0, "")
        assert d["verified"] is False
        assert d["termination"] == "abnormal"

    def test_diverged_completed_run_fails_normally(self):
        d = decide_cloud_verify_result(1, DIVERGED_STDERR)
        assert d["verified"] is False
        assert d["termination"] == "normal"
        assert d["completion"] is True
        assert d["differences"] == 1

    def test_killed_check_over_diverged_data_fails(self):
        d = decide_cloud_verify_result(0, TRUNCATED_STDERR)
        assert d["verified"] is False

    def test_timeout_sentinel_is_abnormal(self):
        d = decide_cloud_verify_result(-1, "")
        assert d["verified"] is False
        assert d["termination"] == "abnormal"

    def test_none_exit_code_fails_closed(self):
        d = decide_cloud_verify_result(None, CLEAN_STDERR)
        assert d["verified"] is False
        assert d["termination"] == "abnormal"

    def test_crash_exit_without_summary_fails(self):
        d = decide_cloud_verify_result(2, "some fatal error, no summary")
        assert d["verified"] is False
        assert d["termination"] == "abnormal"
        assert d["completion"] is False

    def test_error_exit_with_summary_fails_normally(self):
        d = decide_cloud_verify_result(2, CLEAN_STDERR)
        assert d["verified"] is False
        assert d["termination"] == "normal"

    def test_clean_summary_with_error_lines_is_contradiction(self):
        stderr = CLEAN_STDERR + "2026/09/06 18:18:48 ERROR : x.dat: corrupted\n"
        d = decide_cloud_verify_result(0, stderr)
        assert d["verified"] is False
        assert d["termination"] == "abnormal"

    def test_clean_summary_with_failure_verdict_is_contradiction(self):
        stderr = CLEAN_STDERR + "2026/09/06 18:18:48 NOTICE: Failed to check: transient\n"
        d = decide_cloud_verify_result(0, stderr)
        assert d["verified"] is False
        assert d["termination"] == "abnormal"

    def test_exit_0_claiming_differences_is_contradiction(self):
        d = decide_cloud_verify_result(0, DIVERGED_STDERR)
        assert d["verified"] is False
        assert d["termination"] == "abnormal"

    def test_stdout_summary_accepted_as_completion_evidence(self):
        d = decide_cloud_verify_result(0, "", CLEAN_STDERR)
        assert d["verified"] is True


class TestVerifyCloudIntegrityF08:
    @patch("core.cloud_verify.subprocess.run")
    def test_forged_exit_0_with_truncated_log_not_verified(self, mock_run):
        mock_run.return_value = _mock_run(0, stderr=TRUNCATED_STDERR)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["termination"] == "abnormal"
        assert result["completion"] is False
        assert result["error"] is not None

    @patch("core.cloud_verify.subprocess.run")
    def test_completed_clean_check_still_verifies(self, mock_run):
        mock_run.return_value = _mock_run(0, stderr=CLEAN_STDERR)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is True
        assert result["termination"] == "normal"
        assert result["completion"] is True
        assert result["differences"] == 0
        assert result["error"] is None

    @patch("core.cloud_verify.subprocess.run")
    def test_completed_diverged_check_fails(self, mock_run):
        mock_run.return_value = _mock_run(1, stderr=DIVERGED_STDERR)
        result = verify_cloud_integrity("/src", "bucket", "FY26-27", "/cfg")
        assert result["verified"] is False
        assert result["termination"] == "normal"
        assert result["differences"] == 1


@needs_rclone
@needs_windows_kill
class TestLiveKillWithForgedExitZero:
    """Real subprocess semantics: a live `rclone check` terminated with
    TerminateProcess(handle, 0) — the exact P8 mechanism — must not
    produce VERIFIED, even though the OS reports exit code 0."""

    def _tree(self, root: Path, n: int = 300) -> None:
        (root / "a").mkdir(parents=True, exist_ok=True)
        (root / "b").mkdir(parents=True, exist_ok=True)
        blob = b"x" * 20480
        for i in range(n):
            (root / ("a" if i % 2 else "b") / f"f{i:04d}.dat").write_bytes(blob)

    def _kill_exit_zero(self, proc) -> None:
        handle = proc._handle
        ok = ctypes.windll.kernel32.TerminateProcess(int(handle), 0)
        assert ok, "TerminateProcess failed"

    def test_killed_check_exit_0_is_not_verified(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        self._tree(src)
        self._tree(dst)
        cmd = [
            RCLONE,
            "check",
            str(src),
            str(dst),
            "--one-way",
            "--size-only",
            "--modify-window",
            "2s",
            "--checkers",
            "1",
        ]
        forged = None
        for _ in range(5):
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            # Kill (near-)immediately: at this point rclone is still in
            # startup/walk, so no completion summary can have been emitted.
            time.sleep(0.05)
            if proc.poll() is None:
                self._kill_exit_zero(proc)
            _, stderr = proc.communicate(timeout=120)
            if proc.returncode == 0 and "differences found" not in (stderr or ""):
                forged = (proc.returncode, stderr or "")
                break
        if forged is None:
            pytest.skip("could not land a mid-check kill in 5 attempts")
        returncode, stderr = forged
        assert returncode == 0  # the OS really did report success for a kill
        d = decide_cloud_verify_result(returncode, stderr)
        assert d["verified"] is False
        assert d["termination"] == "abnormal"
        assert d["completion"] is False

    def test_completed_check_verifies(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        self._tree(src, n=20)
        self._tree(dst, n=20)
        cmd = [
            RCLONE,
            "check",
            str(src),
            str(dst),
            "--one-way",
            "--size-only",
            "--modify-window",
            "2s",
            "--checkers",
            "1",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        assert proc.returncode == 0
        d = decide_cloud_verify_result(proc.returncode, proc.stderr, proc.stdout)
        assert d["verified"] is True
        assert d["termination"] == "normal"


class TestWeeklyAuditCompletionContract:
    def test_exit_0_without_summary_is_not_verified(self):
        # The --combined diff file is pre-created empty: presence alone
        # must not yield VERIFIED (same F-08 class as the daily check).
        from core import integrity as integ_mod

        with patch.object(
            integ_mod.subprocess,
            "run",
            return_value=_mock_run(0, stdout="", stderr=""),
        ):
            result = integ_mod._run_rclone_check("/src", "/dst", "test", timeout=60)
        assert result["status"] == "VERIFICATION_FAILED"
        assert result["termination"] == "abnormal"
        assert "completion summary" in result["detail"]

    def test_summary_diff_contradiction_fails_closed(self):
        from core import integrity as integ_mod

        def fake_run(cmd, **kwargs):
            idx = list(cmd).index("--combined")
            Path(cmd[idx + 1]).write_text("+ ghost.txt\n", encoding="utf-8")
            return _mock_run(0, stderr=CLEAN_STDERR)

        with patch.object(integ_mod.subprocess, "run", side_effect=fake_run):
            result = integ_mod._run_rclone_check("/src", "/dst", "test", timeout=60)
        assert result["status"] == "VERIFICATION_FAILED"
        assert "contradictory" in result["detail"]

    def test_timeout_never_verifies(self):
        from core import integrity as integ_mod

        with patch.object(
            integ_mod.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="rclone", timeout=60),
        ):
            result = integ_mod._run_rclone_check("/src", "/dst", "test", timeout=60)
        assert result["status"] == "VERIFICATION_FAILED"
        assert result["termination"] == "abnormal"
