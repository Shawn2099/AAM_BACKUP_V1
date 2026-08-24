"""Tests for lan_sync — REAL robocopy.exe runs against the live SMB share."""

import msvcrt
import os
import tempfile
import time
from pathlib import Path

import pytest

from core.health import HealthError
from core.lan_sync import (
    _read_log_tail,
    _validate_required_flags,
    build_robocopy_command,
    classify_exit_code,
    cleanup_orphaned_robocopy_logs,
    failed_file_count,
    run_lan_sync,
)
from core.lan_preflight import run_lan_dry_run
from models.config import LanConfig


SMB_ROOT = Path(r"\\127.0.0.1\lan_backup\AAM_PYTEST_LAN")


@pytest.fixture
def smb_dest():
    d = SMB_ROOT / f"dest_{int(time.time() * 1000)}_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def lan_cfg():
    return LanConfig(enabled=True, retry_count=2, retry_wait_seconds=1,
                     mt_threads=4)


def _make_tree(src: Path):
    (src / "docs").mkdir(parents=True)
    (src / "a.txt").write_text("alpha")
    (src / "b.bin").write_bytes(os.urandom(4096))
    (src / "docs" / "nested.txt").write_text("nested-content")


class TestClassifyExitCode:

    def test_zero_returns_complete(self):
        assert classify_exit_code(0) == "LAN_COMPLETE"

    def test_bit0_files_copied(self):
        assert classify_exit_code(1) == "LAN_COMPLETE"

    def test_bit2_mismatched(self):
        assert classify_exit_code(4) == "LAN_PARTIAL"

    def test_bit3_copy_errors_returns_partial(self):
        assert classify_exit_code(8) == "LAN_PARTIAL"

    def test_bit4_fatal_error_returns_failed(self):
        assert classify_exit_code(16) == "LAN_FAILED"

    def test_negative_code_returns_failed(self):
        assert classify_exit_code(-1) == "LAN_FAILED"


class TestValidateRequiredFlags:

    def test_nc_flag_raises(self):
        with pytest.raises(ValueError, match="/NC"):
            _validate_required_flags(["/MIR", "/NC"])

    def test_valid_flags_pass(self):
        _validate_required_flags(["/MIR", "/Z", "/XJ"])


class TestBuildRobocopyCommand:

    def test_basic_command_structure(self):
        cfg = LanConfig(retry_count=3, retry_wait_seconds=10, mt_threads=8)
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.1\\share", cfg)
        assert cmd[1] == "D:\\"
        assert "/MIR" in cmd and "/XJ" in cmd

    def test_resolves_real_bundled_binary(self):
        cmd = build_robocopy_command("D:\\", "\\\\srv\\share", LanConfig())
        assert Path(cmd[0]).exists()

    def test_no_nc_flag_present(self):
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", LanConfig())
        assert "/NC" not in [f.upper() for f in cmd]


class TestRunLanSyncReal:

    def test_first_mirror_copies_everything(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)

        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)

        assert result["status"] == "LAN_COMPLETE", result
        assert result["exit_code"] in (1, 3)
        assert result["error"] is None and result["files_failed"] == 0
        assert (smb_dest / "a.txt").read_text() == "alpha"
        assert (smb_dest / "docs" / "nested.txt").read_text() == "nested-content"

    def test_second_run_is_no_op_complete(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)
        run_lan_sync(str(src), str(smb_dest), lan_cfg)

        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)

        assert result["status"] == "LAN_COMPLETE"
        assert result["exit_code"] == 0

    def test_mirror_restores_deleted_destination_file(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)
        run_lan_sync(str(src), str(smb_dest), lan_cfg)

        (smb_dest / "a.txt").unlink()
        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)

        assert result["status"] == "LAN_COMPLETE"
        assert result["exit_code"] & 1
        assert (smb_dest / "a.txt").read_text() == "alpha"

    def test_mirror_purges_extra_destination_files(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)
        orphan = smb_dest / "stale.docx"
        orphan.write_text("orphan")

        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)

        assert result["status"] == "LAN_COMPLETE"
        assert not orphan.exists()

    def test_canary_file_never_copied(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)
        (src / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)

        assert result["status"] == "LAN_COMPLETE"
        assert not (smb_dest / ".AAM_TARGET_MOUNTED").exists()

    def test_copy_failure_sets_error_and_failed_count(self, tmp_path, smb_dest, lan_cfg):
        """REAL copy error: source file locked by a byte-range lock (what
        Excel/AV do). Robocopy exhausts retries → bit 3 + FAILED line."""
        src = tmp_path / "src"
        _make_tree(src)
        victim = src / "locked.txt"
        victim.write_text("0123456789")
        fd = os.open(str(victim), os.O_RDWR)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 10)
        try:
            result = run_lan_sync(str(src), str(smb_dest), lan_cfg)
        finally:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 10)
            os.close(fd)

        assert result["status"] == "LAN_PARTIAL"
        assert result["exit_code"] & 8
        assert "locked.txt" in result["error"]
        assert result["files_failed"] >= 1
        assert result["anomaly_details"] is None

    def test_fatal_invalid_source_returns_failed(self, smb_dest, lan_cfg):
        result = run_lan_sync(
            str(Path("Q:") / "definitely_not_here"), str(smb_dest), lan_cfg
        )
        assert result["status"] == "LAN_FAILED"
        assert result["error"]

    def test_real_timeout_kills_run(self, lan_cfg):
        """OS-enforced subprocess timeout during robocopy's real retry sleep."""
        src = Path(tempfile.gettempdir()) / f"aam_tmo_src_{os.getpid()}"
        src.mkdir(parents=True, exist_ok=True)
        victim = src / "stalled.txt"
        victim.write_text("0123456789")
        fd = os.open(str(victim), os.O_RDWR)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 10)
        try:
            lan_cfg.subprocess_timeout_seconds = 2   # real instance, real deadline
            lan_cfg.retry_count = 5                  # keeps robocopy in its
            lan_cfg.retry_wait_seconds = 60          # 60s retry sleep past t=2s
            started = time.monotonic()
            result = run_lan_sync(
                str(src), r"\\127.0.0.1\lan_backup\AAM_PYTEST_LAN\tmo_dest", lan_cfg
            )
            elapsed = time.monotonic() - started
        finally:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 10)
            os.close(fd)

        assert result["status"] == "LAN_FAILED"
        assert result["exit_code"] == -1
        assert "Timeout after 2s" in result["error"]
        assert 1.5 <= elapsed < 30

    def test_temp_log_cleaned_up_after_success(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)
        marker = time.time()
        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)
        assert result["status"] == "LAN_COMPLETE"

        leftovers = [
            p for p in Path(tempfile.gettempdir()).glob("robocopy_sync_*.log")
            if p.stat().st_mtime >= marker
        ]
        assert leftovers == []

    def test_result_contract_keys_exact(self, tmp_path, smb_dest, lan_cfg):
        result = run_lan_sync(str(tmp_path / "src"), str(smb_dest), lan_cfg)
        assert set(result.keys()) == {
            "status", "exit_code", "error", "anomaly_details", "files_failed",
        }


class TestLanDryRunReal:

    def test_passes_when_canary_present(self, tmp_path, smb_dest):
        _make_tree(tmp_path / "src")
        (smb_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        result = run_lan_dry_run(str(tmp_path / "src"), str(smb_dest))

        assert result["ok"] is True
        assert result["exit_code"] < 8

    def test_missing_canary_refuses_to_mirror(self, tmp_path, smb_dest):
        (smb_dest / ".AAM_TARGET_MOUNTED").unlink(missing_ok=True)

        with pytest.raises(HealthError, match="Canary"):
            run_lan_dry_run(str(tmp_path / "src"), str(smb_dest))

    def test_missing_source_reports_failure(self, smb_dest):
        (smb_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        result = run_lan_dry_run(r"Q:\missing_source", str(smb_dest))

        assert result["ok"] is False
        assert result["exit_code"] >= 8

    def test_real_timeout_on_unreachable_network(self, smb_dest):
        (smb_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        started = time.monotonic()
        result = run_lan_dry_run(
            r"\\203.0.113.5\noshare_src", str(smb_dest), timeout=2
        )
        elapsed = time.monotonic() - started

        # Canary gate passes (real share); blocked robocopy killed at 2s.
        if not result["ok"]:
            assert result["exit_code"] == -1
            assert "Timeout after 2s" in result["error"]
        assert elapsed < 60

    def test_zero_bytes_moved_in_list_mode(self, tmp_path, smb_dest):
        src = tmp_path / "src"
        _make_tree(src)
        (smb_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        run_lan_dry_run(str(src), str(smb_dest))

        assert [p.name for p in smb_dest.iterdir()] == [".AAM_TARGET_MOUNTED"]


class TestReadLogTail:

    def test_short_log_returned_in_full(self, tmp_path):
        log = tmp_path / "robocopy.log"
        log.write_text("short log", encoding="utf-8")
        assert _read_log_tail(log, 100) == "short log"

    def test_long_log_truncated_to_max_bytes(self, tmp_path):
        log = tmp_path / "robocopy.log"
        log.write_text("x" * 200, encoding="utf-8")
        tail = _read_log_tail(log, 100)
        assert len(tail) == 100 and tail == "x" * 100

    def test_missing_file_returns_fallback_message(self, tmp_path):
        assert "log unreadable" in _read_log_tail(tmp_path / "gone.log", 1000)


class TestFailedFileCountFromRealLog:

    def test_summary_parse_from_genuine_run(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)
        victim = src / "locked.txt"
        victim.write_text("0123456789")
        fd = os.open(str(victim), os.O_RDWR)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 10)
        try:
            result = run_lan_sync(str(src), str(smb_dest), lan_cfg)
        finally:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 10)
            os.close(fd)

        assert result["exit_code"] & 8
        assert result["files_failed"] >= 1

    def test_pure_positional_row_parser(self):
        log = (
            "               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
            "    Files :         4         3         0         0         1         2\n"
        )
        assert failed_file_count(log, exit_code=8) == 1

    def test_missing_summary_floors_by_bit3(self):
        assert failed_file_count("", exit_code=8) == 1
        assert failed_file_count("", exit_code=0) == 0


class TestCleanupOrphanedLogs:

    def test_removes_only_stale_logs_in_real_tempdir(self):
        tempdir = Path(tempfile.gettempdir())
        old = tempdir / "robocopy_sync_pytest_old.log"
        fresh = tempdir / "robocopy_sync_pytest_fresh.log"
        old.write_text("stale")
        fresh.write_text("current")
        three_days_ago = time.time() - 72 * 3600
        os.utime(old, (three_days_ago, three_days_ago))

        try:
            removed = cleanup_orphaned_robocopy_logs(max_age_hours=24)
        finally:
            fresh.unlink(missing_ok=True)

        assert removed >= 1
        assert not old.exists()
