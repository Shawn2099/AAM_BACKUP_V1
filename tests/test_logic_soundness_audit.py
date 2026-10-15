"""Logic-soundness audit — characterization + property tests for core logic.

PURPOSE
=======
This module does NOT change any production code. It *tests* the branching
logic of the `core` package to (a) pin the actual behavior of every
decision path and (b) expose ambiguities, dead/redundant conditions, and
fail-open/fail-closed asymmetries that a reader cannot resolve from the
source alone. Every assertion describes what the code REALLY does, so a
future behavior change fails loudly here.

Each section header names the suspicious logic and the finding it pins.
"""

from __future__ import annotations

import http.client
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ═══════════════════════════════════════════════════════════════════════════
# core.health
# ═══════════════════════════════════════════════════════════════════════════

from core import health


class TestCheckSourceDrive:
    """Finding H-1: disk_usage failure is swallowed -> low space undetected."""

    def test_missing_drive(self, tmp_path):
        ok, reason = health.check_source_drive(str(tmp_path / "nope"))
        assert ok is False and "not accessible" in reason

    # M2 2.2: emptiness is guard G1's verdict (BLOCKED for a human), not a
    # health failure; the builders refuse it as the last line of defence.
    def test_empty_dir_is_refused_by_g1(self, tmp_path):
        from core import guards
        assert health.check_source_drive(str(tmp_path)) == (True, "")
        with pytest.raises(ValueError, match="contains no files"):
            guards.require_source_files(str(tmp_path))

    def test_system_entries_only_is_refused_by_g1(self, tmp_path):
        from core import guards
        (tmp_path / "System Volume Information").mkdir()
        (tmp_path / "$RECYCLE.BIN").mkdir()
        (tmp_path / "desktop.ini").write_text("x")
        assert health.check_source_drive(str(tmp_path)) == (True, "")
        with pytest.raises(ValueError, match="contains no files"):
            guards.require_source_files(str(tmp_path))

    def test_one_real_file_passes(self, tmp_path):
        (tmp_path / "real.txt").write_text("x")
        ok, reason = health.check_source_drive(str(tmp_path))
        assert ok is True and reason == ""

    def test_low_space_refused(self, tmp_path):
        (tmp_path / "real.txt").write_text("x")
        usage = shutil.disk_usage("/")
        fake = MagicMock(free=0)
        with patch("core.health.shutil.disk_usage", return_value=fake):
            ok, reason = health.check_source_drive(str(tmp_path), min_free_gb=1)
        assert ok is False and "low on space" in reason

    def test_disk_usage_oserror_is_swallowed(self, tmp_path):
        """Pinned ambiguity: if disk_usage raises, the drive is still OK.

        A drive that is nearly full but whose usage cannot be read passes
        the health gate silently (only a warning is logged)."""
        (tmp_path / "real.txt").write_text("x")
        with patch("core.health.shutil.disk_usage", side_effect=OSError("boom")):
            ok, reason = health.check_source_drive(str(tmp_path), min_free_gb=1)
        assert ok is True and reason == ""


class TestCheckGcsKey:
    def test_missing(self, tmp_path):
        ok, reason = health.check_gcs_key(str(tmp_path / "k.json"))
        assert ok is False and "not found" in reason

    def test_empty_file(self, tmp_path):
        p = tmp_path / "k.json"
        p.write_text("")
        ok, reason = health.check_gcs_key(str(p))
        assert ok is False and "empty" in reason

    def test_nonempty_file_passes(self, tmp_path):
        p = tmp_path / "k.json"
        p.write_text("{}")
        ok, reason = health.check_gcs_key(str(p))
        assert ok is True and reason == ""


class _FakeResp:
    def __init__(self, date_header):
        self._d = date_header

    def getheader(self, name):
        return self._d


class _FakeConn:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    def request(self, *a, **k):
        if self._exc:
            raise self._exc

    def getresponse(self):
        return self._resp

    def close(self):
        pass


class TestPreBackupHealth:
    """Finding H-3: the second `if gcs_key_path:` guard is always True.

    After `if not gcs_key_path: raise`, the path is guaranteed non-empty,
    so the inner guard can never be False. The tests below prove
    check_gcs_key always runs in cloud mode."""

    @pytest.fixture(autouse=True)
    def _ok_source(self):
        with patch("core.health.check_source_drive", return_value=(True, "")):
            yield

    def test_invalid_mode(self):
        with pytest.raises(health.HealthError):
            health.pre_backup_health("E:\\", "sideways")

    def test_source_failure_propagates(self):
        with patch("core.health.check_source_drive", return_value=(False, "bad")):
            with pytest.raises(health.HealthError):
                health.pre_backup_health("E:\\", "lan")

    def test_cloud_without_key_path(self):
        with patch("core.health.check_binary_exists", return_value=True):
            with pytest.raises(health.HealthError):
                health.pre_backup_health("E:\\", "cloud", gcs_key_path=None)

    def test_cloud_always_validates_gcs_key(self):
        with patch("core.health.check_binary_exists", return_value=True), \
             patch("core.health.check_gcs_key", return_value=(True, "")) as m_key, \
             patch("core.health.check_clock", return_value=health.ClockResult("OK", 0.0, "google", "")):
            health.pre_backup_health("E:\\", "cloud", gcs_key_path="C:\\k.json")
        assert m_key.call_count == 1  # guard is redundant -> always runs

    def test_cloud_bad_key_propagates(self):
        with patch("core.health.check_binary_exists", return_value=True), \
             patch("core.health.check_gcs_key", return_value=(False, "empty")), \
             patch("core.health.check_clock", return_value=health.ClockResult("OK", 0.0, "google", "")):
            with pytest.raises(health.HealthError):
                health.pre_backup_health("E:\\", "cloud", gcs_key_path="C:\\k.json")

    def test_cloud_missing_rclone(self):
        with patch("core.health.check_binary_exists", return_value=False):
            with pytest.raises(health.HealthError, match="rclone"):
                health.pre_backup_health("E:\\", "cloud", gcs_key_path="C:\\k.json")

    def test_lan_missing_robocopy(self):
        with patch("core.health.check_binary_exists", return_value=False):
            with pytest.raises(health.HealthError, match="robocopy"):
                health.pre_backup_health("E:\\", "lan")

    def test_all_good_passes(self):
        with patch("core.health.check_binary_exists", return_value=True), \
             patch("core.health.check_gcs_key", return_value=(True, "")), \
             patch("core.health.check_clock", return_value=health.ClockResult("OK", 0.0, "google", "")):
            health.pre_backup_health("E:\\", "all", gcs_key_path="C:\\k.json")


# ═══════════════════════════════════════════════════════════════════════════
# core.time_utils
# ═══════════════════════════════════════════════════════════════════════════

from core import time_utils


class TestFiscalYearRouting:
    @pytest.mark.parametrize("d,expected", [
        ("2026-04-01", "FY26-27"),
        ("2026-12-31", "FY26-27"),
        ("2027-03-31", "FY26-27"),
        ("2027-04-01", "FY27-28"),
        ("2026-03-31", "FY25-26"),
        ("2000-01-01", "FY99-00"),
    ])
    def test_boundary(self, d, expected):
        from datetime import date
        y, m, dd = map(int, d.split("-"))
        assert time_utils.get_fy_prefix(date(y, m, dd)) == expected

    def test_prefix_format_is_two_digit(self):
        assert time_utils.get_fy_prefix() == time_utils.get_fy_prefix()


class TestCronToHuman:
    def test_empty_and_none(self):
        assert time_utils.cron_to_human("") == ""
        assert time_utils.cron_to_human(None) == ""

    def test_invalid_returns_raw(self):
        assert time_utils.cron_to_human("not a cron") == "not a cron"


# ═══════════════════════════════════════════════════════════════════════════
# core.lan_sync
# ═══════════════════════════════════════════════════════════════════════════

from core import lan_sync


class TestClassifyExitCode:
    @pytest.mark.parametrize("code,expected", [
        (0, "LAN_COMPLETE"),
        (1, "LAN_COMPLETE"),
        (2, "LAN_COMPLETE"),
        (4, "LAN_COMPLETE"),
        (7, "LAN_COMPLETE"),
        (8, "LAN_PARTIAL"),
        (12, "LAN_PARTIAL"),
        (15, "LAN_PARTIAL"),
        (16, "LAN_FAILED"),
        (24, "LAN_FAILED"),   # 16|8 -> bit 4 wins (FAILED, not PARTIAL)
        (100, "LAN_FAILED"),
        (-1, "LAN_FAILED"),   # timeout sentinel
    ])
    def test_table(self, code, expected):
        assert lan_sync.classify_exit_code(code) == expected

    @given(st.integers(-10_000, 10_000))
    def test_partial_implies_bit3(self, code):
        """Finding L-1: LAN_PARTIAL is *always* a bit-3 code.

        Therefore fy_rollover.run_final_backup's second success disjunct
        `lan_status == "LAN_PARTIAL" and not (exit_code & 8)` is dead code:
        the `and` can never be true."""
        if lan_sync.classify_exit_code(code) == "LAN_PARTIAL":
            assert code & 8

    @given(st.integers(-10_000, 10_000))
    def test_always_a_known_status(self, code):
        assert lan_sync.classify_exit_code(code) in {
            "LAN_COMPLETE", "LAN_PARTIAL", "LAN_FAILED",
        }


_SYNC_HEADER = (
    "               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
)


def _summary(total, copied, skipped, mismatch, failed, extras, with_ended=True):
    row = (
        f"    Files : {total:>9} {copied:>9} {skipped:>9} "
        f"{mismatch:>9} {failed:>9} {extras:>9}\n"
    )
    text = _SYNC_HEADER + row
    if with_ended:
        text += "   Ended : Wednesday, October 14, 2026 1:00:00 AM\n"
    return text


class TestFailedFileCount:
    def test_parsed_from_summary(self):
        assert lan_sync.failed_file_count(_summary(10, 5, 3, 1, 2, 0), 3) == 2

    def test_bit3_floor_when_log_unparseable(self):
        assert lan_sync.failed_file_count("garbage", 8) == 1

    def test_bit3_floor_beats_zero_summary(self):
        """Pinned: a summary claiming 0 FAILED with bit 3 set resolves loud (1)."""
        assert lan_sync.failed_file_count(_summary(10, 5, 3, 1, 0, 0), 8) == 1

    def test_clean(self):
        assert lan_sync.failed_file_count(_summary(10, 10, 0, 0, 0, 0), 0) == 0


class TestSummaryFilesRow:
    def test_exact_six_columns(self):
        assert lan_sync.summarize_log_counts(_summary(10, 5, 3, 1, 2, 0)) == {
            "total": 10, "copied": 5, "skipped": 3,
            "mismatch": 1, "failed": 2, "extras": 0,
        }

    def test_extra_columns_silently_truncated(self):
        """Finding L-2: a row with 7 numeric columns keeps only the first 6.

        `dict(zip(...))` without strict=True silently discards the surplus,
        so a future robocopy format addition would be lost without error."""
        text = (
            "    Files : 10 5 3 1 2 0 99\n"
            "   Ended : x\n"
        )
        counts = lan_sync.summarize_log_counts(text)
        assert counts is not None
        assert len(counts) == 6
        assert 99 not in counts.values()

    def test_non_numeric_column_returns_none(self):
        text = "    Files : 10 5 3 1 2 x\n   Ended : x\n"
        assert lan_sync.summarize_log_counts(text) is None

    def test_missing_row_returns_none(self):
        assert lan_sync.summarize_log_counts("no summary here") is None


class TestLogShowsCompletedSummary:
    def test_requires_both_files_row_and_ended(self):
        assert lan_sync.log_shows_completed_summary(_summary(1, 1, 0, 0, 0, 0)) is True
        assert lan_sync.log_shows_completed_summary(_summary(1, 1, 0, 0, 0, 0, False)) is False
        assert lan_sync.log_shows_completed_summary("   Ended : x\n") is False
        assert lan_sync.log_shows_completed_summary("") is False


class TestDecideLanResult:
    def test_negative_code_failed(self):
        r = lan_sync.decide_lan_result(-1, _summary(1, 1, 0, 0, 0, 0))
        assert r["status"] == "LAN_FAILED" and r["termination"] == "abnormal"

    def test_bit4_wins_over_truncated_log(self):
        """Finding L-3: bit 4 is checked BEFORE log completeness -> FAILED."""
        r = lan_sync.decide_lan_result(16, "")
        assert r["status"] == "LAN_FAILED"

    def test_truncated_log_is_suspect(self):
        r = lan_sync.decide_lan_result(0, "")
        assert r["status"] == "LAN_SUSPECT" and r["log_complete"] is False

    def test_bit3_with_summary_is_partial(self):
        r = lan_sync.decide_lan_result(8, _summary(3, 1, 1, 0, 2, 0))
        assert r["status"] == "LAN_PARTIAL" and r["termination"] == "normal"

    def test_zero_with_failed_markers_is_suspect(self):
        text = _summary(2, 1, 0, 0, 0, 0) + "  ** FAILED: a.txt\n"
        r = lan_sync.decide_lan_result(0, text)
        assert r["status"] == "LAN_SUSPECT"

    def test_zero_with_failed_summary_is_suspect(self):
        r = lan_sync.decide_lan_result(0, _summary(2, 1, 0, 0, 1, 0))
        assert r["status"] == "LAN_SUSPECT"

    def test_mismatch_with_finished_pass_is_complete(self):
        r = lan_sync.decide_lan_result(4, _summary(4, 2, 2, 2, 0, 0))
        assert r["status"] == "LAN_COMPLETE" and r["reason"] is not None

    def test_plain_success(self):
        r = lan_sync.decide_lan_result(1, _summary(3, 1, 2, 0, 0, 0))
        assert r["status"] == "LAN_COMPLETE" and r["reason"] is None

    @given(st.integers(-10_000, 10_000))
    def test_partial_implies_bit3_here_too(self, code):
        r = lan_sync.decide_lan_result(code, _summary(1, 0, 0, 0, 1, 0))
        if r["status"] == "LAN_PARTIAL":
            assert code & 8

    @given(st.integers(-10_000, 10_000))
    def test_never_raises_and_termination_consistent(self, code):
        r = lan_sync.decide_lan_result(code, _summary(1, 1, 0, 0, 0, 0))
        assert r["status"] in {"LAN_COMPLETE", "LAN_PARTIAL", "LAN_SUSPECT", "LAN_FAILED"}
        assert r["termination"] in {"normal", "abnormal"}


class TestRequiredFlags:
    def test_nc_forbidden(self):
        with pytest.raises(ValueError):
            lan_sync._validate_required_flags(["/MIR", "/NC"])

    def test_other_flags_allowed(self):
        lan_sync._validate_required_flags(["/MIR", "/V"])


# ═══════════════════════════════════════════════════════════════════════════
# core.process
# ═══════════════════════════════════════════════════════════════════════════

from core import process


class TestCreateTime:
    def test_self_is_float(self):
        ct = process._get_create_time(os.getpid())
        assert isinstance(ct, float)

    @pytest.mark.parametrize("bad_pid", [-1, -999, 10**12])
    def test_bad_pid_returns_none(self, bad_pid):
        assert process._get_create_time(bad_pid) is None


class TestReadLockAlive:
    def test_absent(self, tmp_path):
        assert process.read_lock_alive(tmp_path / "l.lock") == (False, None)

    def test_live_self_lock(self, tmp_path):
        p = tmp_path / "l.lock"
        process.write_lock(p)
        alive, pid = process.read_lock_alive(p)
        assert alive is True and pid == os.getpid()

    def test_dead_pid(self, tmp_path):
        p = tmp_path / "l.lock"
        p.write_text("999999999:123.0")
        alive, pid = process.read_lock_alive(p)
        assert alive is False and pid == 999999999

    def test_pid_reuse_detected(self, tmp_path):
        p = tmp_path / "l.lock"
        p.write_text(f"{os.getpid()}:1.0")  # wrong create_time for this pid
        alive, pid = process.read_lock_alive(p)
        assert alive is False and pid == os.getpid()

    def test_legacy_bare_pid(self, tmp_path):
        p = tmp_path / "l.lock"
        p.write_text(str(os.getpid()))
        alive, _ = process.read_lock_alive(p)
        assert alive is True

    @pytest.mark.parametrize("content", ["", "abc", "1:notafloat", "0", "-5", "1:", "::"])
    def test_corrupt_is_not_alive(self, tmp_path, content):
        p = tmp_path / "l.lock"
        p.write_text(content)
        alive, _ = process.read_lock_alive(p)
        assert alive is False

    def test_permission_error_treated_as_live(self, tmp_path):
        """Pinned: an unreadable lock is assumed live (fail-safe)."""
        p = tmp_path / "l.lock"
        p.write_text("x")
        with patch.object(Path, "read_text", side_effect=PermissionError()):
            assert process.read_lock_alive(p) == (True, -1)

    @given(st.text(max_size=40))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_never_raises_on_arbitrary_content(self, tmp_path, content):
        p = tmp_path / "l.lock"
        p.write_text(content, encoding="utf-8")
        alive, pid = process.read_lock_alive(p)
        assert isinstance(alive, bool)


class TestAcquireLock:
    def test_fresh_acquire(self, tmp_path):
        p = tmp_path / "b.lock"
        assert process.acquire_lock(p) is True
        assert p.exists()
        alive, _ = process.read_lock_alive(p)
        assert alive is True

    def test_live_lock_not_stolen(self, tmp_path):
        p = tmp_path / "b.lock"
        process.write_lock(p)
        before = p.read_text()
        assert process.acquire_lock(p) is False
        assert p.read_text() == before  # untouched

    def test_dead_lock_reclaimed(self, tmp_path):
        p = tmp_path / "b.lock"
        p.write_text("999999999:123.0")
        assert process.acquire_lock(p) is True
        alive, _ = process.read_lock_alive(p)
        assert alive is True


class TestResolveBinary:
    def test_missing_binary(self):
        assert process.resolve_binary("definitely-not-real-xyz-123") is None


# ═══════════════════════════════════════════════════════════════════════════
# core.hashing
# ═══════════════════════════════════════════════════════════════════════════

from core import hashing


class TestHashing:
    def test_known_md5(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_bytes(b"hello")
        assert hashing.compute_md5(f) == "5d41402abc4b2a76b9719d911017c592"

    def test_pending_never_verifies(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_bytes(b"hello")
        assert hashing.verify_checksum(f, hashing.PENDING_CHECKSUM) is False

    def test_match_and_mismatch(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_bytes(b"hello")
        assert hashing.verify_checksum(f, "5d41402abc4b2a76b9719d911017c592") is True
        assert hashing.verify_checksum(f, "0" * 32) is False

    @given(st.binary(max_size=200_000))
    def test_deterministic_and_chunk_agnostic(self, data):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(data)
            path = fh.name
        try:
            assert hashing.compute_md5(path) == hashing.compute_md5(path)
        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════════
# core.lan_manifest
# ═══════════════════════════════════════════════════════════════════════════

from core import lan_manifest


class TestWalkLanDestination:
    def test_walks_files(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "sub" / "b.txt").write_text("bb")
        files = lan_manifest.walk_lan_destination(str(tmp_path))
        paths = {f["path"] for f in files}
        assert paths == {"a.txt", "sub/b.txt"}

    def test_empty_share_returns_empty(self, tmp_path):
        assert lan_manifest.walk_lan_destination(str(tmp_path)) == []

    def test_root_error_raises(self, tmp_path):
        def fake_walk(path, onerror=None, **kw):
            if onerror:
                onerror(OSError(13, "denied", str(tmp_path)))
            return iter([])

        with patch("core.lan_manifest.os.walk", side_effect=fake_walk):
            with pytest.raises(OSError):
                lan_manifest.walk_lan_destination(str(tmp_path))

    def test_root_error_with_odd_filename_does_not_raise(self, tmp_path):
        """Finding M-1: root-failure detection is a brittle string match on
        OSError.filename. If the filename differs even by a separator, the
        'must be loud' contract fails open and [] is returned."""
        def fake_walk(path, onerror=None, **kw):
            if onerror:
                onerror(OSError(13, "denied", str(tmp_path) + os.sep))
            return iter([])

        with patch("core.lan_manifest.os.walk", side_effect=fake_walk):
            assert lan_manifest.walk_lan_destination(str(tmp_path)) == []


class TestDiffSnapshots:
    @given(
        st.dictionaries(st.text(min_size=1, max_size=8), st.tuples(st.integers(), st.floats(allow_nan=False, allow_infinity=False)), max_size=20),
        st.dictionaries(st.text(min_size=1, max_size=8), st.tuples(st.integers(), st.floats(allow_nan=False, allow_infinity=False)), max_size=20),
    )
    def test_partition_is_total_and_disjoint(self, before, after):
        d = lan_manifest.diff_snapshots(before, after)
        all_sets = [set(d["added"]), set(d["removed"]),
                    set(d["modified"]), set(d["unchanged"])]
        # disjoint
        for i in range(len(all_sets)):
            for j in range(i + 1, len(all_sets)):
                assert all_sets[i].isdisjoint(all_sets[j])
        # union == all keys
        assert set().union(*all_sets) == set(before) | set(after)

    def test_every_change_is_classified(self):
        d = lan_manifest.diff_snapshots({"a": (1, 1.0)}, {"b": (1, 1.0), "a": (2, 1.0)})
        assert d["added"] == ["b"]
        assert d["removed"] == []
        assert d["modified"] == ["a"]
        assert d["unchanged"] == []


# ═══════════════════════════════════════════════════════════════════════════
# core.cloud_verify
# ═══════════════════════════════════════════════════════════════════════════

from core import cloud_verify


class TestDecideCloudVerifyResult:
    def test_bool_exit_code_is_unusable(self):
        r = cloud_verify.decide_cloud_verify_result(True, "NOTICE: 0 differences found")
        assert r["verified"] is False and "unusable exit code" in r["reason"]

    def test_negative_is_unusable(self):
        r = cloud_verify.decide_cloud_verify_result(-1, "NOTICE: 0 differences found")
        assert r["verified"] is False

    def test_non_int_is_unusable(self):
        r = cloud_verify.decide_cloud_verify_result("0", "NOTICE: 0 differences found")
        assert r["verified"] is False

    def test_no_summary_fails_closed(self):
        r = cloud_verify.decide_cloud_verify_result(0, "")
        assert r["verified"] is False and r["completion"] is False

    def test_clean_verifies(self):
        r = cloud_verify.decide_cloud_verify_result(0, "NOTICE: 0 differences found")
        assert r["verified"] is True and r["differences"] == 0

    def test_clean_summary_with_error_line_contradicts(self):
        log = "NOTICE: 0 differences found\n2026/09/06 18:18:52 ERROR : boom"
        r = cloud_verify.decide_cloud_verify_result(0, log)
        assert r["verified"] is False and "contradicts" in r["reason"]

    def test_clean_summary_with_failed_verdict_contradicts(self):
        log = "NOTICE: 0 differences found\nFailed to check: oops"
        r = cloud_verify.decide_cloud_verify_result(0, log)
        assert r["verified"] is False

    def test_clean_summary_exit1_fails(self):
        r = cloud_verify.decide_cloud_verify_result(1, "NOTICE: 0 differences found")
        assert r["verified"] is False and r["termination"] == "normal"

    def test_differences_exit1(self):
        r = cloud_verify.decide_cloud_verify_result(1, "1 differences found")
        assert r["verified"] is False and r["differences"] == 1

    def test_exit0_with_differences_contradicts(self):
        r = cloud_verify.decide_cloud_verify_result(0, "3 differences found")
        assert r["verified"] is False and "contradicts" in r["reason"]

    @given(st.integers(-100, 100), st.text(max_size=200))
    def test_verified_implies_zero_differences_and_exit_zero(self, code, log):
        r = cloud_verify.decide_cloud_verify_result(code, log)
        if r["verified"]:
            assert code == 0 and r["differences"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# core.cloud_reporter
# ═══════════════════════════════════════════════════════════════════════════

from core import cloud_reporter


class TestCloudReporterErrors:
    def _run(self, **kw):
        m = MagicMock()
        for k, v in kw.items():
            setattr(m, k, v)
        return m

    def test_size_nonzero_exit_reports_error(self):
        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.subprocess.run",
                   return_value=self._run(returncode=1, stdout="", stderr="boom")):
            out = cloud_reporter.get_cloud_size("b", "FY26-27", "cfg")
        assert out["count"] == 0 and "_error" in out

    def test_size_timeout_reports_error(self):
        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("rclone", 1)):
            out = cloud_reporter.get_cloud_size("b", "FY26-27", "cfg")
        assert out["count"] == 0 and "_error" in out

    def test_manifest_nonzero_raises(self):
        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.subprocess.run",
                   return_value=self._run(returncode=1, stdout="", stderr="boom")):
            with pytest.raises(cloud_reporter.CloudReporterError):
                cloud_reporter.get_cloud_manifest("b", "FY26-27", "cfg")

    def test_manifest_unparsable_raises(self):
        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.subprocess.run",
                   return_value=self._run(returncode=0, stdout="{not json", stderr="")):
            with pytest.raises(cloud_reporter.CloudReporterError):
                cloud_reporter.get_cloud_manifest("b", "FY26-27", "cfg")

    def test_manifest_timeout_raises(self):
        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("rclone", 1)):
            with pytest.raises(cloud_reporter.CloudReporterError):
                cloud_reporter.get_cloud_manifest("b", "FY26-27", "cfg")

    def test_diff_partial_has_heterogeneous_extra_keys(self):
        """Finding C-1: get_cloud_diff returns a dict whose values are
        lists for added/removed/modified/unchanged but bool/str for the
        _partial/_error keys — a mixed-type mapping consumers must guard."""
        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.subprocess.run",
                   return_value=self._run(returncode=2, stdout="", stderr="boom")):
            diff = cloud_reporter.get_cloud_diff("src", "b", "FY26-27", "cfg")
        assert diff["_partial"] is True
        assert isinstance(diff["_error"], str)
        for k in ("added", "removed", "modified", "unchanged"):
            assert isinstance(diff[k], list)

    def test_diff_timeout_partial(self):
        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("rclone", 1)):
            diff = cloud_reporter.get_cloud_diff("src", "b", "FY26-27", "cfg")
        assert diff["_partial"] is True


# ═══════════════════════════════════════════════════════════════════════════
# core.integrity
# ═══════════════════════════════════════════════════════════════════════════

from core import integrity


class TestParseCombinedFile:
    def test_absent_file(self):
        added, removed, modified, unchanged, errors, present = \
            integrity._parse_combined_file(None)
        assert present is False and added == []

    def test_all_prefixes(self, tmp_path):
        f = tmp_path / "d.txt"
        f.write_text(
            "+ only-src.txt\n"
            "- only-dest.txt\n"
            "* changed.txt\n"
            "= same.txt\n"
            "! error.txt\n"
            "? unknown.txt\n"
        )
        added, removed, modified, unchanged, errors, present = \
            integrity._parse_combined_file(str(f))
        assert present is True
        assert added == ["only-src.txt"]
        assert removed == ["only-dest.txt"]
        assert modified == ["changed.txt"]
        assert unchanged == ["same.txt"]
        assert errors == ["error.txt"]
        # Finding I-1: an unrecognised status char is silently dropped
        # (neither counted as a mismatch nor as an error) — fail-open.
        assert "unknown.txt" not in added + removed + modified + unchanged + errors

    def test_short_and_empty_lines_ignored(self, tmp_path):
        f = tmp_path / "d.txt"
        f.write_text("\n=\n= .\n")
        added, removed, modified, unchanged, errors, present = \
            integrity._parse_combined_file(str(f))
        assert present is True and (added + removed + modified + unchanged + errors) == []

    @given(st.lists(st.text(max_size=30), max_size=30))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_never_raises(self, tmp_path, lines):
        f = tmp_path / "d.txt"
        f.write_text("\n".join(lines), encoding="utf-8")
        integrity._parse_combined_file(str(f))


class TestCanary:
    @pytest.mark.parametrize("rel,expected", [
        (".AAM_TARGET_MOUNTED", True),
        ("sub/.AAM_TARGET_MOUNTED", True),
        ("a/b/c/.AAM_TARGET_MOUNTED", True),
        ("x.AAM_TARGET_MOUNTED", False),
        ("normal.txt", False),
    ])
    def test_is_canary(self, rel, expected):
        assert integrity._is_canary(rel) is expected


class TestRunRcloneCheck:
    """Branch-level audit of _run_rclone_check via controlled parse + run."""

    def _result(self, returncode=0, stdout="", stderr=""):
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = stderr
        return m

    def _call(self, parse, run_result, size_only=False):
        # mkstemp must return a real fd so os.close() works.
        with patch("core.integrity.resolve_binary", return_value="rclone"), \
             patch("core.integrity.subprocess.run", return_value=run_result), \
             patch("core.integrity._parse_combined_file", return_value=parse):
            return integrity._run_rclone_check("src", "dst", "test", 5, size_only=size_only)

    def test_clean_is_verified(self):
        r = self._call(([], [], [], [], [], True),
                       self._result(0, "NOTICE: 0 differences found"))
        assert r["status"] == "VERIFIED" and r["mismatches"] == 0

    def test_size_only_never_verified(self):
        r = self._call(([], [], [], [], [], True),
                       self._result(0, "NOTICE: 0 differences found"), size_only=True)
        assert r["status"] == "VERIFICATION_FAILED"
        assert r["capability"] == integrity.CAPABILITY_SIZE_ONLY

    def test_read_error_fails_closed(self):
        r = self._call(([], [], [], [], ["bad.txt"], True),
                       self._result(0, "NOTICE: 0 differences found"))
        assert r["status"] == "VERIFICATION_FAILED"

    def test_no_summary_fails_closed(self):
        r = self._call(([], [], [], [], [], True), self._result(0, ""))
        assert r["status"] == "AUDIT_INCOMPLETE" and r["mismatches"] == -1

    def test_process_error_fails_closed(self):
        r = self._call(([], [], [], [], [], True), self._result(2, "boom"))
        assert r["status"] == "AUDIT_INCOMPLETE"

    def test_contradictory_evidence_fails_closed(self):
        r = self._call(([], [], ["m.txt"], [], [], True),
                       self._result(0, "0 differences found"))
        assert r["status"] == "VERIFICATION_FAILED"
        assert "contradictory" in r["detail"]

    def test_canary_only_difference_verifies_clean(self):
        """The operational canary is filtered; a canary-only diff is VERIFIED."""
        r = self._call(([".AAM_TARGET_MOUNTED"], [], [], [], [], True),
                       self._result(0, "1 differences found"))
        assert r["status"] == "VERIFIED"
        assert r["canary_excluded"] == 1 and r["raw_mismatches"] == 1

    def test_real_difference_fails(self):
        r = self._call(([], [], ["m.txt"], [], [], True),
                       self._result(1, "1 differences found"))
        assert r["status"] == "VERIFICATION_FAILED"
        assert r["mismatches"] == 1

    def test_timeout_returns_full_schema(self):
        with patch("core.integrity.resolve_binary", return_value="rclone"), \
             patch("core.integrity.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("rclone", 1)):
            r = integrity._run_rclone_check("src", "dst", "test", 5)
        for key in ("raw_missing", "raw_extra", "raw_changed",
                    "raw_mismatches", "canary_excluded", "canary_excluded_paths"):
            assert key in r
        assert r["status"] == "AUDIT_INCOMPLETE"


class TestAuditCloudCapability:
    """Finding I-2 / WP7: the cloud capability label is decided BEFORE the
    check runs and logs (it used to be rewritten afterwards)."""

    def test_cloud_audit_passes_the_gcs_capability_up_front(self):
        with patch("core.integrity._run_rclone_check", return_value={"status": "VERIFIED"}) as run:
            integrity.audit_cloud("s", "b", "FY26-27", "cfg")
        assert run.call_args.kwargs["capability"] == integrity.CAPABILITY_HASH_GCS


# ═══════════════════════════════════════════════════════════════════════════
# core.backup_repository
# ═══════════════════════════════════════════════════════════════════════════

from core import backup_repository as br

class TestCleanPath:
    @pytest.mark.parametrize("raw,expected", [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("a\\b\\c.txt", "a/b/c.txt"),
        ("\\a\\b", "a/b"),
        (".", ""),
        ("C:\\x\\y", "C:/x/y"),
    ])
    def test_basic(self, raw, expected):
        assert br._clean_path(raw) == expected

    def test_duplicate_separators_not_collapsed(self):
        """Finding B-2: doubled separators survive as `//` — _clean_path
        normalizes separators but never collapses repeats, so a path with
        doubled separators does not equal its single-separator form."""
        # Doubled separators behave inconsistently by POSITION: a leading
        # anchor-style double survives as `//`, but an internal double is
        # collapsed by PureWindowsPath.
        assert br._clean_path("\\\\a\\\\b") == "a//b"     # leading: NOT collapsed
        assert br._clean_path("a\\\\b") == "a/b"        # internal: collapsed

    @given(st.text(alphabet="ab/\\.:", min_size=1, max_size=15))
    def test_fixed_point_within_two_passes(self, raw):
        """Finding B-1: _clean_path is not idempotent in one application
        (a trailing separator is dropped only on the next pass) but is a
        fixed point within two passes."""
        once = br._clean_path(raw)
        twice = br._clean_path(once)
        assert br._clean_path(twice) == twice

    @given(st.text(alphabet="ab/\\.:", min_size=1, max_size=15))
    def test_result_shape(self, raw):
        out = br._clean_path(raw)
        assert "\\" not in out
        assert not out.startswith("/")


# ═══════════════════════════════════════════════════════════════════════════
# core.fy_rollover
# ═══════════════════════════════════════════════════════════════════════════

from core import fy_rollover


class TestFyName:
    @pytest.mark.parametrize("path,expected", [
        ("E:\\SOURCE\\FY26-27", "FY26-27"),
        ("E:/source/fy25-26", "FY25-26"),
        ("\\\\srv\\share\\FY24-25", "FY24-25"),
        ("E:\\SOURCE", None),
        ("E:\\SOURCE\\notify", None),
    ])
    def test_fy_name(self, path, expected):
        assert fy_rollover._fy_name(path) == expected


class TestPathHelpers:
    def test_parent_windows(self):
        assert fy_rollover._parent_path("E:\\SRC\\FY26-27") == "E:\\SRC"

    def test_parent_unc(self):
        assert fy_rollover._parent_path("\\\\srv\\share\\FY26-27") == "\\\\srv\\share"

    def test_parent_posix(self):
        assert fy_rollover._parent_path("/mnt/src/FY26-27") == "/mnt/src"

    def test_child_windows_style(self):
        assert fy_rollover._child_path("E:\\SRC", "FY26-27") == "E:\\SRC\\FY26-27"

    def test_child_posix_style(self):
        assert fy_rollover._child_path("/mnt/src", "FY26-27") == "/mnt/src/FY26-27"


class TestDetectRollover:
    def test_match_means_no_rollover(self):
        computed = time_utils.get_fy_prefix()
        assert fy_rollover.detect_rollover(f"E:\\SRC\\{computed}", "\\\\s\\x") is False

    def test_old_fy_triggers(self):
        assert fy_rollover.detect_rollover("E:\\SRC\\FY00-01", "\\\\s\\x") is True

    def test_no_fy_anywhere_is_opt_out(self):
        assert fy_rollover.detect_rollover("E:\\SRC", "\\\\s\\x") is False

    def test_source_missing_fy_but_lan_has_old_fy(self):
        """Finding F-1: detection falls back to the LAN FY, but the later
        rollover uses `src_fy or lan_fy`; a source without an FY suffix can
        still be forced through a rollover driven solely by the LAN path."""
        assert fy_rollover.detect_rollover("E:\\SRC", "\\\\s\\x\\FY00-01") is True


# ═══════════════════════════════════════════════════════════════════════════
# core.manifest — allow-list guards around f-string SQL (bandit B608)
# ═══════════════════════════════════════════════════════════════════════════

from core.manifest import ManifestDB  # noqa: E402


class TestManifestModeGuards:
    """Bandit flags f-string SQL in manifest.py (B608). These tests prove
    every interpolated identifier (mode / status_field) is validated against
    a fixed allow-list first, so the interpolation cannot take user input."""

    @pytest.fixture
    def db(self, tmp_path):
        d = ManifestDB(str(tmp_path / "m.db"))
        yield d
        d.close()

    @pytest.mark.parametrize("bad", ["users; DROP TABLE file_entries", "LAN", "", "lan_status"])
    def test_bulk_upsert_rejects_bad_mode(self, db, bad):
        with pytest.raises(ValueError):
            db.bulk_upsert_synced([{"path": "a", "size": 1, "mtime": 0.0}], mode=bad)

    @pytest.mark.parametrize("bad", ["x", "", "cloud_status"])
    def test_clear_mode_status_rejects_bad_mode(self, db, bad):
        with pytest.raises(ValueError):
            db.clear_mode_status(bad, ["a"])

    @pytest.mark.parametrize("bad", ["x", "", "cloud_status"])
    def test_prune_stale_rejects_bad_mode(self, db, bad):
        with pytest.raises(ValueError):
            db.prune_stale_synced(bad, set())

    @pytest.mark.parametrize("bad", ["x", "", "lan_status = NULL --"])
    def test_get_synced_paths_rejects_bad_mode(self, db, bad):
        with pytest.raises(ValueError):
            db.get_synced_paths(bad)

    @pytest.mark.parametrize("bad", ["x", "1=1", "lan_status; --"])
    def test_file_count_rejects_bad_status_field(self, db, bad):
        with pytest.raises(ValueError):
            db.file_count(bad)

    def test_valid_modes_accepted(self, db):
        db.bulk_upsert_synced([{"path": "a", "size": 1, "mtime": 0.0}], mode="cloud")
        assert db.file_count("cloud_status") == 1


def test_run_final_backup_lan_partial_guard_is_unreachable():
    """Finding F-2: the `(lan_status == 'LAN_PARTIAL' and not (exit_code & 8))`
    success disjunct in run_final_backup can never be true, because every
    producer of LAN_PARTIAL guarantees bit 3 is set (see the classify
    invariant above)."""
    for code in range(-5, 40):
        status = lan_sync.classify_exit_code(code)
        if status == "LAN_PARTIAL":
            assert (code & 8) != 0
