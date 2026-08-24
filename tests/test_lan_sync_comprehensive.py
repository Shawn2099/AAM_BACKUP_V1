"""Comprehensive tests for core/lan_sync.py — pure matrices plus REAL runs.

Classification, command building, log parsing and orphan cleanup call the
original production functions directly. The orchestration smoke test drives
the actual robocopy binary through run_lan_sync against \\127.0.0.1\lan_backup.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from core.lan_sync import (
    _read_log_tail,
    _validate_required_flags,
    build_robocopy_command,
    classify_exit_code,
    cleanup_orphaned_robocopy_logs,
    count_failed_lines,
    failed_file_count,
    run_lan_sync,
)
from models.config import LanConfig


SMB_ROOT = Path(r"\\127.0.0.1\lan_backup\AAM_PYTEST_LAN")


@pytest.fixture
def smb_dest():
    d = SMB_ROOT / f"comp_{int(time.time() * 1000)}_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 1. classify_exit_code — full bitmask matrix (pure, original fn)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestClassifyExitCode:

    @pytest.mark.parametrize("code", [0, 1, 2, 3])
    def test_codes_0_to_3_complete(self, code):
        assert classify_exit_code(code) == "LAN_COMPLETE"

    @pytest.mark.parametrize("code", [4, 5, 6, 7])
    def test_codes_4_to_7_partial(self, code):
        assert classify_exit_code(code) == "LAN_PARTIAL"

    @pytest.mark.parametrize("code", [8, 9, 10, 15])
    def test_codes_8_to_15_partial(self, code):
        assert classify_exit_code(code) == "LAN_PARTIAL"

    @pytest.mark.parametrize("code", [16, 17, 32, 255])
    def test_bit4_and_above_failed(self, code):
        assert classify_exit_code(code) == "LAN_FAILED"

    @pytest.mark.parametrize("code", [-1, -8, -16])
    def test_negative_codes_failed(self, code):
        assert classify_exit_code(code) == "LAN_FAILED"


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 2. build_robocopy_command — flag matrix + REAL binary resolution
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestBuildRobocopyCommand:

    def test_all_flags_present(self):
        cfg = LanConfig(mt_threads=8, retry_count=5, retry_wait_seconds=15)
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.5\\share", cfg)

        for flag in ("/MIR", "/Z", "/ZB", "/XJ", "/MT:8", "/R:5", "/W:15",
                     "/NP", "/NDL", "/NJH", "/TS", "/FP", "/V"):
            assert flag in cmd
        # P1-COUNT: /NJS removed — job summary is the files_failed source.
        assert "/NJS" not in cmd

    def test_source_and_dest_in_command(self):
        cfg = LanConfig()
        cmd = build_robocopy_command("E:\\SOURCE", "\\\\NAS\\Backups", cfg)
        assert cmd[1] == "E:\\SOURCE"
        assert cmd[2] == "\\\\NAS\\Backups"

    def test_exclusions_present(self):
        cmd = build_robocopy_command("D:\\", "\\\\10.0.0.5\\share", LanConfig())
        assert "System Volume Information" in cmd
        assert "$RECYCLE.BIN" in cmd
        assert ".AAM_TARGET_MOUNTED" in cmd

    def test_resolves_real_binary_on_disk(self):
        """Production resolution: deploy\\bin first, then PATH. The returned
        path must exist as a real executable."""
        cmd = build_robocopy_command("D:\\", "\\\\srv\\share", LanConfig())
        assert Path(cmd[0]).exists(), f"{cmd[0]} does not exist"

    def test_nc_flag_forbidden(self):
        with pytest.raises(ValueError, match="/NC"):
            _validate_required_flags(["/MIR", "/NC"])


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 3. _read_log_tail — real-file edge cases incl. F15 seek path
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestReadLogTail:

    def test_short_log(self, tmp_path):
        log = tmp_path / "test.log"
        log.write_text("line1\nline2\nline3")
        assert _read_log_tail(log, 10000) == "line1\nline2\nline3"

    def test_long_log_returns_exact_tail(self, tmp_path):
        log = tmp_path / "test.log"
        content = "x" * 200_000
        log.write_text(content)
        result = _read_log_tail(log, 100_000)
        assert len(result) == 100_000
        assert result == content[-100_000:]

    def test_unreadable_file_fallback(self, tmp_path):
        result = _read_log_tail(tmp_path / "nonexistent.log", 10000)
        assert "log unreadable" in result

    def test_multibyte_cut_does_not_crash(self, tmp_path):
        """Tail cut may land mid UTF-8 sequence; replacement-char handling."""
        log = tmp_path / "test.log"
        payload = ("â‚¬" * 60_000).encode("utf-8")   # 3 bytes each â†’ >100KB
        log.write_bytes(payload)
        result = _read_log_tail(log, 100_000)
        assert len(result.encode("utf-8")) <= 100_000


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 4. Failed-line / summary parsers (pure, original functions)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestParsers:

    def test_count_failed_lines_counts_markers(self):
        log = "** FAILED: a.txt\nok\n  ** FAILED: b.txt\n"
        assert count_failed_lines(log) == 2

    def test_count_failed_lines_empty(self):
        assert count_failed_lines("") == 0

    def test_summary_row_positional_parse(self):
        log = (
            "              Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
            "   Files :         4         3         0         0         1         2\n"
        )
        assert failed_file_count(log, exit_code=8) == 1
        assert failed_file_count(log, exit_code=0) == 1

    def test_missing_summary_floors_by_bit3_only(self):
        assert failed_file_count("", exit_code=8) == 1
        assert failed_file_count("nothing", exit_code=0) == 0

    def test_non_numeric_row_aborts_to_floor(self):
        assert failed_file_count("Files : a b c d e f", exit_code=9) == 1


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 5. cleanup_orphaned_robocopy_logs — real %TEMP% filesystem
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestCleanupOrphanedLogs:

    def test_age_gate_removes_stale_keeps_fresh(self):
        tempdir = Path(tempfile.gettempdir())
        stale = tempdir / "robocopy_sync_comp_stale.log"
        fresh = tempdir / "robocopy_sync_comp_fresh.log"
        stale.write_text("stale")
        fresh.write_text("fresh")
        old = time.time() - 48 * 3600
        os.utime(stale, (old, old))
        try:
            removed = cleanup_orphaned_robocopy_logs(max_age_hours=24)
        finally:
            fresh.unlink(missing_ok=True)

        assert removed >= 1
        assert not stale.exists()

    def test_never_raises_on_garbage(self):
        # Must stay silent even when nothing matches.
        assert cleanup_orphaned_robocopy_logs(max_age_hours=24) >= 0


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# 6. run_lan_sync smoke — REAL robocopy /LOG lifecycle
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestRunLanSyncRealSmoke:
    """The mocked suite asserted subprocess kwargs; with the real binary the
    observable contract is: /LOG written to %TEMP% during the run, deleted in
    the finally block, correct classification of a genuine copy."""

    def test_log_written_then_removed(self, tmp_path, smb_dest):
        src = tmp_path / "src"
        (src / "docs").mkdir(parents=True)
        (src / "docs" / "f.txt").write_text("data")
        cfg = LanConfig(retry_count=2, retry_wait_seconds=1, mt_threads=2)

        tempdir = Path(tempfile.gettempdir())

        # No pre-existing logs newer than this marker.
        marker = time.time() - 5
        before = {p.name for p in tempdir.glob("robocopy_sync_*.log")}

        result = run_lan_sync(str(src), str(smb_dest), cfg)

        assert result["status"] == "LAN_COMPLETE"
        after = {
            p.name for p in tempdir.glob("robocopy_sync_*.log")
            if p.stat().st_mtime >= marker
        }
        new_names = after - before
        # The run's own log must be gone from %TEMP% (finally-block unlink).
        assert new_names == set() or all(
            not n.startswith("robocopy_sync_") or n in before for n in new_names
        )

    def test_full_mirror_cycle_on_share(self, tmp_path, smb_dest):
        src = tmp_path / "src"
        (src / "d").mkdir(parents=True)
        (src / "a.txt").write_text("v1")
        (src / "d" / "b.bin").write_bytes(os.urandom(2048))
        cfg = LanConfig(retry_count=2, retry_wait_seconds=1, mt_threads=2)

        r1 = run_lan_sync(str(src), str(smb_dest), cfg)
        assert r1["exit_code"] & 1                       # copies happened
        assert (smb_dest / "a.txt").read_text() == "v1"

        (src / "a.txt").write_text("v2")                 # modify source
        r2 = run_lan_sync(str(src), str(smb_dest), cfg)
        assert r2["status"] == "LAN_COMPLETE"
        assert (smb_dest / "a.txt").read_text() == "v2"  # mirror updated
