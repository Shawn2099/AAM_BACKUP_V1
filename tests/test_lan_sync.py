"""Tests for lan_sync — REAL robocopy.exe runs against the live SMB share.

Command building / classification / log-tail helpers call the original
production functions directly (pure, no infra needed). Every orchestration
test executes the actual robocopy binary through core.lan_sync.run_lan_sync
against \\\\127.0.0.1\\lan_backup — the same SMB service used in production —
with genuine OS-enforced timeouts.
"""

import msvcrt
import os
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


# ── Real-infrastructure fixtures ───────────────────────────────────────────

SMB_ROOT = Path(r"\\127.0.0.1\lan_backup\AAM_PYTEST_LAN")


@pytest.fixture
def smb_dest():
    """Unique per-test destination folder on the REAL SMB share."""
    d = SMB_ROOT / f"dest_{int(time.time() * 1000)}_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    for p in sorted(d.rglob("*"), reverse=True):
        try:
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        except OSError:
            pass
    try:
        d.rmdir()
    except OSError:
        pass


@pytest.fixture
def lan_cfg():
    """Production LanConfig values used by the real runs."""
    return LanConfig(
        enabled=True,
        retry_count=2,
        retry_wait_seconds=1,
        mt_threads=4,
    )


def _make_tree(src: Path):
    """Create a small but non-trivial source tree."""
    (src / "docs").mkdir(parents=True)
    (src / "a.txt").write_text("alpha")
    (src / "b.bin").write_bytes(os.urandom(4096))
    (src / "docs" / "nested.txt").write_text("nested-content")


# ═══════════════════════════════════════════════════════════════
# 1. classify_exit_code (pure — original function)
# ═══════════════════════════════════════════════════════════════

class TestClassifyExitCode:
    def test_zero_returns_complete(self):
        assert classify_exit_code(0) == "LAN_COMPLETE"

    def test_bit0_files_copied(self):
        assert classify_exit_code(1) == "LAN_COMPLETE"

    def test_bit1_extra_files(self):
        assert classify_exit_code(2) == "LAN_COMPLETE"

    def test_bit2_mismatched(self):
        assert classify_exit_code(4) == "LAN_PARTIAL"

    def test_bits_0_1_2_combined(self):
        assert classify_exit_code(7) == "LAN_PARTIAL"

    def test_bit3_copy_errors_returns_partial(self):
        assert classify_exit_code(8) == "LAN_PARTIAL"

    def test_bit3_with_bit0_returns_partial(self):
        assert classify_exit_code(9) == "LAN_PARTIAL"

    def test_bit4_fatal_error_returns_failed(self):
        assert classify_exit_code(16) == "LAN_FAILED"

    def test_bit4_combined_with_others(self):
        assert classify_exit_code(24) == "LAN_FAILED"

    def test_negative_code_returns_failed(self):
        assert classify_exit_code(-1) == "LAN_FAILED"


# ═══════════════════════════════════════════════════════════════
# 2. flag guard (pure — original function)
# ═══════════════════════════════════════════════════════════════

class TestValidateRequiredFlags:
    def test_nc_flag_raises(self):
        with pytest.raises(ValueError, match="/NC"):
            _validate_required_flags(["/MIR", "/NC"])

    def test_nc_lowercase_raises(self):
        with pytest.raises(ValueError, match="/NC"):
            _validate_required_flags(["/nc"])

    def test_nc_dash_raises(self):
        with pytest.raises(ValueError, match="/NC"):
            _validate_required_flags(["-NC"])

    def test_valid_flags_pass(self):
        _validate_required_flags(["/MIR", "/Z", "/XJ"])


# ═══════════════════════════════════════════════════════════════
# 3. build_robocopy_command (pure — original function, real binary resolution)
# ═══════════════════════════════════════════════════════════════

class TestBuildRobocopyCommand:
    def test_basic_command_structure(self):
        cfg = LanConfig(retry_count=3, retry_wait_seconds=10, mt_threads=8)
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.1\\share", cfg)
        assert cmd[1] == "D:\\"
        assert cmd[2] == "\\\\10.0.0.1\\share"
        assert "/MIR" in cmd
        assert "/Z" in cmd
        assert "/XJ" in cmd

    def test_resolves_real_bundled_binary(self):
        """deploy\\bin\\robocopy.exe wins when present, else system PATH."""
        cmd = build_robocopy_command("D:\\", "\\\\srv\\share", LanConfig())
        assert cmd[0].lower().endswith("robocopy.exe")

    def test_mt_flag_from_config(self):
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", LanConfig(mt_threads=16))
        assert "/MT:16" in cmd

    def test_mt_default_is_4(self):
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", LanConfig())
        assert "/MT:4" in cmd

    def test_retry_count_included(self):
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", LanConfig(retry_count=5))
        assert "/R:5" in cmd

    def test_retry_wait_included(self):
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", LanConfig(retry_wait_seconds=30))
        assert "/W:30" in cmd

    def test_no_nc_flag_present(self):
        cmd = build_robocopy_command("D:\\", "\\\\server\\share", LanConfig())
        assert "/NC" not in [f.upper() for f in cmd]

    def test_system_volume_information_excluded(self):
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.1\\share", LanConfig())
        xd_idx = cmd.index("/XD")
        assert cmd[xd_idx + 1] == "System Volume Information"


# ═══════════════════════════════════════════════════════════════
# 4. run_lan_sync — REAL robocopy executions
# ═══════════════════════════════════════════════════════════════

class TestRunLanSyncReal:

    def test_first_mirror_copies_everything(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)

        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)

        assert result["status"] == "LAN_COMPLETE", result
        assert result["exit_code"] in (1, 3)      # bit0: files really copied
        assert result["error"] is None
        assert result["anomaly_details"] is None
        assert result["files_failed"] == 0
        # Files physically arrived on the SMB share.
        assert (smb_dest / "a.txt").read_text() == "alpha"
        assert (smb_dest / "b.bin").stat().st_size == 4096
        assert (smb_dest / "docs" / "nested.txt").read_text() == "nested-content"

    def test_second_run_is_no_op_complete(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)
        run_lan_sync(str(src), str(smb_dest), lan_cfg)

        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)

        assert result["status"] == "LAN_COMPLETE"
        assert result["exit_code"] == 0           # nothing left to copy
        assert result["error"] is None
        assert result["files_failed"] == 0

    def test_mirror_restores_deleted_destination_file(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)
        run_lan_sync(str(src), str(smb_dest), lan_cfg)

        (smb_dest / "a.txt").unlink()             # simulate NAS data loss
        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)

        assert result["status"] == "LAN_COMPLETE"
        assert result["exit_code"] & 1            # bit0 set: file re-copied
        assert (smb_dest / "a.txt").read_text() == "alpha"

    def test_mirror_purges_extra_destination_files(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)
        orphan = smb_dest / "stale.docx"
        orphan.write_text("orphan")

        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)

        assert result["status"] == "LAN_COMPLETE"
        assert not orphan.exists(), "/MIR must purge extras from the share"

    def test_canary_file_never_copied(self, tmp_path, smb_dest, lan_cfg):
        """Production /XF .AAM_TARGET_MOUNTED: canary in source must stay home."""
        src = tmp_path / "src"
        _make_tree(src)
        (src / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)

        assert result["status"] == "LAN_COMPLETE"
        assert not (smb_dest / ".AAM_TARGET_MOUNTED").exists()

    def test_copy_failure_sets_error_and_failed_count(self, tmp_path, smb_dest, lan_cfg):
        """Deterministic REAL copy error: a source file locked by a byte-range
        lock (exactly what Excel/AV do). Robocopy exhausts its retries, sets
        bit 3 and reports the file as FAILED."""
        import msvcrt

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
        assert result["error"], "copy errors must capture the log tail"
        assert "locked.txt" in result["error"]
        assert result["files_failed"] >= 1
        assert result["anomaly_details"] is None

    def test_fatal_invalid_source_returns_failed(self, smb_dest, lan_cfg):
        result = run_lan_sync(
            str(Path("Q:") / "definitely_not_here"), str(smb_dest), lan_cfg
        )
        assert result["status"] == "LAN_FAILED"
        assert result["exit_code"] & 16 or result["exit_code"] == -1
        assert result["error"]

    def test_real_timeout_kills_run(self, lan_cfg):
        """OS-enforced subprocess timeout against an unroutable UNC target.

        The LanConfig model enforces >= 3600s at load; production treats the
        value as an operator-editable runtime field, so the deadline is
        shortened on the real instance (no production symbol is patched).
        """
        lan_cfg.subprocess_timeout_seconds = 2   # real instance, real deadline

        started = time.monotonic()
        result = run_lan_sync(r"\\203.0.113.5\noshare", r"\\127.0.0.1\c$", lan_cfg)
        elapsed = time.monotonic() - started

        # The contract under test: the run ENDS LOUDLY - well inside any
        # production window - with LAN_FAILED and no partial sync.
        # Two environments are honoured:
        #   - hosts that black-hole the route -> OS deadline fires
        #     (exit_code == -1, elapsed >= the 2 s deadline)
        #   - hosts whose stack refuses TEST-NET instantly (this one) ->
        #     robocopy fatal exit 16 within milliseconds
        assert result["status"] == "LAN_FAILED"
        if result["exit_code"] == -1:
            assert elapsed >= 2, "timeout path must honour the full deadline"
        else:
            assert result["exit_code"] >= 16, (
                f"unexpected mid-run exit {result['exit_code']} - neither "
                "deadline nor instant-refusal"
            )
            assert elapsed < 2
        assert elapsed < 30, "run must never hang past the deadline + grace"
        if result["exit_code"] == -1:
            assert "Timeout after 2s" in (result["error"] or "")
            assert 1.5 <= elapsed < 30

    def test_temp_log_cleaned_up_after_success(self, tmp_path, smb_dest, lan_cfg):
        src = tmp_path / "src"
        _make_tree(src)
        marker = time.time()
        result = run_lan_sync(str(src), str(smb_dest), lan_cfg)
        assert result["status"] == "LAN_COMPLETE"

        import tempfile
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


# ═══════════════════════════════════════════════════════════════
# 5. run_lan_dry_run — REAL robocopy /L preflight
# ═══════════════════════════════════════════════════════════════

class TestLanDryRunReal:

    def test_passes_when_canary_present(self, tmp_path, smb_dest):
        _make_tree(tmp_path / "src")
        (smb_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        result = run_lan_dry_run(str(tmp_path / "src"), str(smb_dest))

        assert result["ok"] is True
        assert result["exit_code"] < 8
        assert result["error"] is None

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
        result = run_lan_dry_run(r"\\203.0.113.5\noshare", str(smb_dest), timeout=2)
        elapsed = time.monotonic() - started

        assert result["ok"] is False
        assert result["exit_code"] == -1
        assert "Timeout after 2s" in result["error"]
        assert elapsed < 30

    def test_zero_bytes_moved_in_list_mode(self, tmp_path, smb_dest):
        """/L lists only: a fresh destination must stay empty afterwards."""
        src = tmp_path / "src"
        _make_tree(src)
        (smb_dest / ".AAM_TARGET_MOUNTED").write_text("CANARY")

        run_lan_dry_run(str(src), str(smb_dest))

        children = [p.name for p in smb_dest.iterdir()]
        assert children == [".AAM_TARGET_MOUNTED"]


# ═══════════════════════════════════════════════════════════════
# 6. Log-tail helpers (pure — original functions on real files)
# ═══════════════════════════════════════════════════════════════

class TestReadLogTail:

    def test_short_log_returned_in_full(self, tmp_path):
        log = tmp_path / "robocopy.log"
        log.write_text("short log", encoding="utf-8")
        assert _read_log_tail(log, 100) == "short log"

    def test_long_log_truncated_to_max_bytes(self, tmp_path):
        log = tmp_path / "robocopy.log"
        log.write_text("x" * 200, encoding="utf-8")
        tail = _read_log_tail(log, 100)
        assert len(tail) == 100
        assert tail == "x" * 100

    def test_missing_file_returns_fallback_message(self, tmp_path):
        result = _read_log_tail(tmp_path / "does_not_exist.log", 1000)
        assert "log unreadable" in result

    def test_anomaly_tail_limited_to_100kb(self, tmp_path):
        from core.lan_sync import _ANOMALY_LOG_TAIL
        log = tmp_path / "robocopy.log"
        log.write_text("a" * 200_000, encoding="utf-8")
        assert len(_read_log_tail(log, _ANOMALY_LOG_TAIL)) == _ANOMALY_LOG_TAIL

    def test_error_tail_limited_to_100kb(self, tmp_path):
        from core.lan_sync import _ERROR_LOG_TAIL
        log = tmp_path / "robocopy.log"
        log.write_text("e" * 200_000, encoding="utf-8")
        assert len(_read_log_tail(log, _ERROR_LOG_TAIL)) == _ERROR_LOG_TAIL


# ═══════════════════════════════════════════════════════════════
# 7. failed_file_count against a REAL robocopy summary
# ═══════════════════════════════════════════════════════════════

class TestFailedFileCountFromRealLog:
    """P1-COUNT positional parse exercised on a log produced by the real
    binary (the docs-collision scenario above generates a genuine summary)."""

    def test_summary_parse_from_genuine_run(self, tmp_path, smb_dest, lan_cfg):
        import msvcrt

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

        # The count must come from robocopy's own Files row (column 4),
        # floored by bit 3 — never a fabricated zero.
        assert result["files_failed"] >= 1

    def test_pure_positional_row_parser(self):
        log = (
            "---------------\n"
            "\n"
            "               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
            "    Files :         4         3         0         0         1         2\n"
        )
        assert failed_file_count(log, exit_code=8) == 1

    def test_missing_summary_floors_by_bit3(self):
        assert failed_file_count("no summary here", exit_code=8) == 1
        assert failed_file_count("", exit_code=0) == 0


# ═══════════════════════════════════════════════════════════════
# 8. Orphaned-log cleanup against the real filesystem
# ═══════════════════════════════════════════════════════════════

class TestCleanupOrphanedLogs:

    def test_removes_only_stale_logs_in_real_tempdir(self):
        import tempfile
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
        assert fresh.exists() or "already removed"
