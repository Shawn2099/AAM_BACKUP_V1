"""Characterization tests for the low-coverage core modules.

These tests pin down the *observed* behaviour of the modules that the coverage
run flagged as under-tested (report, cloud_reporter, cloud_sync, logging,
fy_rollover, lan_preflight, wol, health, process, time_utils,
backup_repository). They intentionally exercise error paths and boundary
conditions, not just the happy path, so the behaviour of the program is
documented by executable expectation rather than prose.

Where a module's current behaviour contradicts an intuitive expectation, the
test asserts the REAL behaviour and explains it in the docstring — that is the
point of a characterization test.
"""

import contextlib
import os
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from models.config import NotificationConfig, PathsConfig


# ══════════════════════════════════════════════════════════════════════════
# time_utils — FY prefix, cutoff, cron humanisation
# ══════════════════════════════════════════════════════════════════════════

class TestTimeUtilsCharacterization:
    def test_get_fy_prefix_boundaries(self):
        from core.time_utils import get_fy_prefix

        # FY starts April 1. Mar 31 belongs to the previous FY.
        assert get_fy_prefix(date(2026, 4, 1)) == "FY26-27"
        assert get_fy_prefix(date(2026, 3, 31)) == "FY25-26"
        assert get_fy_prefix(date(2026, 12, 31)) == "FY26-27"
        assert get_fy_prefix(date(2027, 1, 1)) == "FY26-27"
        # Century rollover
        assert get_fy_prefix(date(2099, 4, 1)) == "FY99-00"

    def test_now_iso_is_parseable_and_offset(self):
        from core.time_utils import now_iso

        import pendulum

        parsed = pendulum.parse(now_iso())
        assert parsed.utcoffset().total_seconds() == 5.5 * 3600

    def test_cutoff_iso_is_in_the_past(self):
        from core.time_utils import IST, cutoff_iso

        import pendulum

        cutoff = pendulum.parse(cutoff_iso(7))
        assert cutoff < pendulum.now(IST)

    def test_cron_to_human_valid(self):
        from core.time_utils import cron_to_human

        out = cron_to_human("0 18 * * *", tz="Asia/Kolkata")
        assert isinstance(out, str) and out
        assert "Kolkata" in out

    def test_cron_to_human_invalid_returns_raw(self):
        from core.time_utils import cron_to_human

        # Malformed input must never raise — the raw expression is echoed.
        assert cron_to_human("not a cron") == "not a cron"
        assert cron_to_human("") == ""
        assert cron_to_human(None) == ""


# ══════════════════════════════════════════════════════════════════════════
# health — source drive, GCS key, binary, clock, aggregate gate
# ══════════════════════════════════════════════════════════════════════════

class TestHealthCharacterization:
    def test_source_drive_missing(self, tmp_path):
        from core.health import check_source_drive

        ok, reason = check_source_drive(str(tmp_path / "nope"))
        assert ok is False
        assert "not accessible" in reason

    def test_source_drive_empty_is_left_to_guard_g1(self, tmp_path):
        """M2 2.2: health checks the drive is usable; G1 judges emptiness."""
        from core.health import check_source_drive

        empty = tmp_path / "empty"
        empty.mkdir()
        assert check_source_drive(str(empty)) == (True, "")

    def test_source_drive_with_files_ok(self, tmp_path):
        from core.health import check_source_drive

        (tmp_path / "a.txt").write_text("x")
        ok, reason = check_source_drive(str(tmp_path))
        assert ok is True and reason == ""

    def test_g1_ignores_windows_system_entries(self, tmp_path):
        from core import guards

        (tmp_path / "System Volume Information").mkdir()
        (tmp_path / "desktop.ini").write_text("x")
        assert guards.first_regular_file(str(tmp_path), guards.SOURCE_IGNORE_NAMES) is None

    def test_check_gcs_key_missing_empty_and_present(self, tmp_path):
        from core.health import check_gcs_key

        assert check_gcs_key(str(tmp_path / "missing.json"))[0] is False
        empty = tmp_path / "empty.json"
        empty.write_text("")
        ok, reason = check_gcs_key(str(empty))
        assert ok is False and "empty" in reason
        good = tmp_path / "good.json"
        good.write_text('{"type":"service_account"}')
        assert check_gcs_key(str(good)) == (True, "")

    def test_check_binary_exists_false_and_true(self):
        from core.health import check_binary_exists

        assert check_binary_exists("definitely-not-a-real-binary-xyz") is False
        with patch("core.health.resolve_binary", return_value="/usr/bin/rclone"):
            assert check_binary_exists("rclone") is True

    def test_pre_backup_health_rejects_bad_mode(self, tmp_path):
        from core.health import HealthError, pre_backup_health

        with pytest.raises(HealthError, match="Invalid mode"):
            pre_backup_health(str(tmp_path), mode="banana")

    def test_pre_backup_health_lan_requires_robocopy(self, tmp_path):
        from core.health import HealthError, pre_backup_health

        (tmp_path / "a.txt").write_text("x")
        with patch("core.health.check_binary_exists", return_value=False):
            with pytest.raises(HealthError, match="robocopy"):
                pre_backup_health(str(tmp_path), mode="lan")

    def test_pre_backup_health_lan_passes(self, tmp_path):
        from core.health import pre_backup_health

        (tmp_path / "a.txt").write_text("x")
        # Only robocopy is required for lan mode; force it present.
        with patch("core.health.check_binary_exists", return_value=True):
            pre_backup_health(str(tmp_path), mode="lan")

    def test_check_clock_skew_network_error_is_non_fatal(self):
        from core.health import check_clock_skew

        with patch("core.health.http.client.HTTPSConnection") as m:
            m.side_effect = OSError("no network")
            ok, reason = check_clock_skew()
            assert ok is True and reason == ""


# ══════════════════════════════════════════════════════════════════════════
# process — lock file round-trip, PID reuse, binary resolution
# ══════════════════════════════════════════════════════════════════════════

class TestProcessCharacterization:
    def test_write_and_read_lock_roundtrip(self, tmp_path):
        from core.process import read_lock_alive, write_lock

        lock = tmp_path / "backup.lock"
        write_lock(lock)
        alive, pid = read_lock_alive(lock)
        assert alive is True
        assert pid == os.getpid()

    def test_read_missing_lock(self, tmp_path):
        from core.process import read_lock_alive

        assert read_lock_alive(tmp_path / "nope.lock") == (False, None)

    def test_read_corrupt_lock(self, tmp_path):
        from core.process import read_lock_alive

        lock = tmp_path / "corrupt.lock"
        lock.write_text("not-a-pid:not-a-time")
        assert read_lock_alive(lock) == (False, None)

    def test_read_negative_pid_never_alive(self, tmp_path):
        from core.process import read_lock_alive

        lock = tmp_path / "neg.lock"
        lock.write_text("-5:123.0")
        alive, pid = read_lock_alive(lock)
        assert alive is False and pid == -5

    def test_acquire_lock_first_true_second_false(self, tmp_path):
        from core.process import acquire_lock

        lock = tmp_path / "backup.lock"
        assert acquire_lock(lock) is True
        # A live lock owned by this same process must never be overwritten.
        assert acquire_lock(lock) is False

    def test_acquire_reclaims_stale_lock(self, tmp_path):
        from core.process import acquire_lock

        lock = tmp_path / "backup.lock"
        lock.write_text("999999:1.0")  # PID almost certainly gone
        with patch("core.process._get_create_time", return_value=None):
            assert acquire_lock(lock) is True

    def test_pid_alive_self(self):
        from core.process import pid_alive

        assert pid_alive(os.getpid()) is True

    def test_resolve_binary_none_for_unknown(self):
        from core.process import resolve_binary

        assert resolve_binary("definitely-not-real-xyz") is None


# ══════════════════════════════════════════════════════════════════════════
# lan_manifest — missing API parity check is unnecessary; keep to pure funcs
# ══════════════════════════════════════════════════════════════════════════

class TestBackupRepositoryCharacterization:
    def test_clean_path_normalization(self):
        from core.backup_repository import _clean_path

        assert _clean_path(r"\docs\a.txt") == "docs/a.txt"
        assert _clean_path("  ") == ""
        assert _clean_path(None) == ""
        assert _clean_path(".") == ""
        # A drive-qualified path keeps its drive; only leading slashes are stripped.
        assert _clean_path(r"D:\x\y.pdf") == "D:/x/y.pdf"

    def test_record_sync_results_rclone_shaped(self, tmp_db):
        from core.backup_repository import record_sync_results

        entries = [{"Path": "docs/a.pdf", "Size": 100, "ModTime": 1700000000.0}]
        record_sync_results(tmp_db, "cloud", entries)
        assert tmp_db.get_entry("docs/a.pdf") is not None

    def test_record_sync_results_walk_shaped(self, tmp_db):
        from core.backup_repository import record_sync_results

        entries = [{"path": "docs/b.pdf", "size": 50, "mtime": 1700000000.0}]
        record_sync_results(tmp_db, "lan", entries)
        assert tmp_db.get_entry("docs/b.pdf") is not None

    def test_record_run_history_success(self, tmp_db):
        from core.backup_repository import record_run_history

        ok = record_run_history(
            tmp_db,
            run_id="run-1",
            mode="cloud",
            started_at="2026-01-01T00:00:00+05:30",
            ended_at="2026-01-01T00:10:00+05:30",
            status="CLOUD_COMPLETE",
            exit_code=0,
            duration_seconds=600.0,
            files_copied=3,
            bytes_copied=1024,
        )
        assert ok is True
        assert tmp_db.last_run("cloud")["run_id"] == "run-1"

    def test_record_run_history_never_raises(self, tmp_db):
        from core.backup_repository import record_run_history

        # The contract: any DB write failure is swallowed and reported as False.
        with patch.object(tmp_db, "insert_run", side_effect=RuntimeError("boom")):
            ok = record_run_history(
                tmp_db,
                run_id="run-x",
                mode="cloud",
                started_at="2026-01-01T00:00:00+05:30",
                ended_at="2026-01-01T00:10:00+05:30",
                status="CLOUD_COMPLETE",
                exit_code=0,
                duration_seconds=1.0,
            )
        assert ok is False


# ══════════════════════════════════════════════════════════════════════════
# cloud_sync — exit classification, duration cap, command build, log scan
# ══════════════════════════════════════════════════════════════════════════

class TestCloudSyncCharacterization:
    def test_classify_documented_mapping(self):
        from core.cloud_sync import classify_rclone_exit

        assert classify_rclone_exit(0) == "CLOUD_COMPLETE"
        assert classify_rclone_exit(1) == "CLOUD_FAILED"
        assert classify_rclone_exit(2) == "CLOUD_FAILED"
        assert classify_rclone_exit(3) == "CLOUD_FAILED"
        assert classify_rclone_exit(4) == "CLOUD_PARTIAL"
        assert classify_rclone_exit(5) == "CLOUD_PARTIAL"
        assert classify_rclone_exit(6) == "CLOUD_FAILED"
        assert classify_rclone_exit(7) == "CLOUD_FAILED"
        assert classify_rclone_exit(8) == "CLOUD_FAILED"
        assert classify_rclone_exit(9) == "CLOUD_NO_CHANGES_COMPLETE"
        assert classify_rclone_exit(10) == "CLOUD_PARTIAL"
        assert classify_rclone_exit(99) == "CLOUD_FAILED"

    def test_resolve_max_duration_precedence(self):
        from core.cloud_sync import resolve_max_duration_seconds

        # explicit positive wins
        assert resolve_max_duration_seconds(18000, 3600) == 3600
        # explicit zero disables the cap
        assert resolve_max_duration_seconds(18000, 0) is None
        # None -> auto = timeout - 300s margin
        assert resolve_max_duration_seconds(18000, None) == 17700
        # too-small timeout cannot hold the margin -> cap off (never negative)
        assert resolve_max_duration_seconds(200, None) is None

    def test_build_rclone_sync_command_flags(self, tmp_path):
        from core.cloud_sync import build_rclone_sync_command

        (tmp_path / "a.txt").write_text("x")
        cmd = build_rclone_sync_command(
            source=str(tmp_path),
            bucket="mybucket",
            fy_prefix="FY26-27",
            config_path="/cfg",
            storage_class="STANDARD",
            bwlimit="5M",
            max_duration_seconds=None,
        )
        assert "sync" in cmd
        assert "aam_gcs:mybucket/FY26-27" in cmd
        assert "--bwlimit" in cmd and "5M" in cmd
        assert "--gcs-storage-class" in cmd and "STANDARD" in cmd
        assert "--max-duration" not in cmd
        assert "--error-on-no-transfer" in cmd

    def test_build_rclone_sync_command_with_duration_cap(self, tmp_path):
        from core.cloud_sync import build_rclone_sync_command

        (tmp_path / "a.txt").write_text("x")
        cmd = build_rclone_sync_command(
            source=str(tmp_path),
            bucket="b",
            fy_prefix="FY26-27",
            config_path="/cfg",
            storage_class="STANDARD",
            max_duration_seconds=3600,
        )
        assert "--max-duration" in cmd
        assert "3600s" in cmd
        assert "--cutoff-mode" in cmd and "SOFT" in cmd

    def test_build_rclone_sync_command_refuses_empty_source(self, tmp_path):
        from core.cloud_sync import build_rclone_sync_command

        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ValueError, match="contains no files"):
            build_rclone_sync_command(
                source=str(empty),
                bucket="b",
                fy_prefix="FY26-27",
                config_path="/cfg",
                storage_class="STANDARD",
            )

    def test_scan_rclone_log_clean(self):
        from core.cloud_sync import scan_rclone_log_for_errors

        assert scan_rclone_log_for_errors("") == (False, "")
        assert scan_rclone_log_for_errors("")[0] is False

    def test_scan_rclone_log_json_error(self):
        from core.cloud_sync import scan_rclone_log_for_errors

        log = '{"level":"ERROR","msg":"upload failed"}'
        has_error, tail = scan_rclone_log_for_errors(log)
        assert has_error is True and "upload failed" in tail

    def test_scan_rclone_log_plaintext_fatal_marker(self):
        from core.cloud_sync import scan_rclone_log_for_errors

        has_error, tail = scan_rclone_log_for_errors("Failed to copy: bucket not found")
        assert has_error is True

    def test_run_cloud_sync_no_changes(self, tmp_path):
        from core.cloud_sync import run_cloud_sync
        from test_support.fake_tools import fake_run_tool

        @contextlib.contextmanager
        def fake_cfg(*a, **k):
            yield "/tmp/fake.conf"

        with patch("core.cloud_sync.temp_rclone_config", fake_cfg), \
             patch("core.cloud_sync.build_rclone_sync_command", return_value=["rclone"]), \
             fake_run_tool("core.cloud_sync", exit_code=9):
            result = run_cloud_sync(
                source=str(tmp_path), bucket="b", fy_prefix="FY26-27",
                gcs_key_path="/k", project_number="1", storage_class="STANDARD",
            )
        assert result["status"] == "CLOUD_NO_CHANGES_COMPLETE"
        assert result["exit_code"] == 9

    def test_run_cloud_sync_timeout(self, tmp_path):
        from core.cloud_sync import run_cloud_sync
        from test_support.fake_tools import fake_run_tool

        @contextlib.contextmanager
        def fake_cfg(*a, **k):
            yield "/tmp/fake.conf"

        with patch("core.cloud_sync.temp_rclone_config", fake_cfg), \
             patch("core.cloud_sync.build_rclone_sync_command", return_value=["rclone"]), \
             fake_run_tool("core.cloud_sync", timed_out=True):
            result = run_cloud_sync(
                source=str(tmp_path), bucket="b", fy_prefix="FY26-27",
                gcs_key_path="/k", project_number="1", storage_class="STANDARD",
                timeout=18000,
            )
        assert result["status"] == "CLOUD_FAILED"
        assert result["exit_code"] == -1
        assert "Timeout" in result["error"]


# ══════════════════════════════════════════════════════════════════════════
# cloud_reporter — size / manifest / diff parsing and failure honesty
# ══════════════════════════════════════════════════════════════════════════

class TestCloudReporterCharacterization:
    def test_get_cloud_size_parses_json(self):
        from core.cloud_reporter import get_cloud_size

        with patch("core.cloud_reporter.subprocess.run") as m:
            m.return_value = MagicMock(
                returncode=0, stdout='{"count": 7, "bytes": 4096}', stderr=""
            )
            result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert result["count"] == 7
        assert result["bytes"] == 4096

    def test_get_cloud_size_nonzero_exit_reports_error(self):
        from core.cloud_reporter import get_cloud_size

        with patch("core.cloud_reporter.subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
            result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert result["count"] == 0
        assert "exited 1" in result["_error"]

    def test_get_cloud_size_unparsable_json(self):
        from core.cloud_reporter import get_cloud_size

        with patch("core.cloud_reporter.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
            result = get_cloud_size("bucket", "FY26-27", "/cfg")
        assert "_error" in result

    def test_get_cloud_size_timeout(self):
        from core.cloud_reporter import get_cloud_size

        with patch("core.cloud_reporter.subprocess.run") as m:
            m.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=1)
            result = get_cloud_size("bucket", "FY26-27", "/cfg", timeout=1)
        assert result["count"] == 0 and "_error" in result

    def test_get_cloud_manifest_filters_dirs(self):
        from core.cloud_reporter import get_cloud_manifest

        payload = (
            '[{"Path":"a.txt","Size":1,"IsDir":false},'
            ' {"Path":"sub","Size":0,"IsDir":true}]'
        )
        with patch("core.cloud_reporter.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout=payload, stderr="")
            files = get_cloud_manifest("bucket", "FY26-27", "/cfg")
        assert [f["Path"] for f in files] == ["a.txt"]

    def test_get_cloud_manifest_raises_on_failure(self):
        from core.cloud_reporter import CloudReporterError, get_cloud_manifest

        with patch("core.cloud_reporter.subprocess.run") as m:
            m.return_value = MagicMock(returncode=3, stdout="", stderr="dir not found")
            with pytest.raises(CloudReporterError, match="exited 3"):
                get_cloud_manifest("bucket", "FY26-27", "/cfg")

    def test_get_cloud_manifest_raises_on_timeout(self):
        from core.cloud_reporter import CloudReporterError, get_cloud_manifest

        with patch("core.cloud_reporter.subprocess.run") as m:
            m.side_effect = subprocess.TimeoutExpired(cmd="rclone", timeout=1)
            with pytest.raises(CloudReporterError, match="timed out"):
                get_cloud_manifest("bucket", "FY26-27", "/cfg", timeout=1)

    def _run_diff_with_file(self, content: str):
        """Helper: run get_cloud_diff with a diff file pre-populated by rclone."""
        from core import cloud_reporter

        real_mkstemp = tempfile.mkstemp

        def fake_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(suffix=".txt")
            os.write(fd, content.encode("utf-8"))
            return fd, path

        with patch.object(cloud_reporter.tempfile, "mkstemp", side_effect=fake_mkstemp), \
             patch("core.cloud_reporter.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="", stderr="")
            return cloud_reporter.get_cloud_diff(
                source="E:\\", bucket="bucket", fy_prefix="FY26-27",
                config_path="/cfg",
            )

    def test_get_cloud_diff_parses_all_prefixes(self):
        result = self._run_diff_with_file(
            "+ added.txt\n- removed.txt\n* modified.txt\n= same.txt\n"
        )
        assert result["added"] == ["added.txt"]
        assert result["removed"] == ["removed.txt"]
        assert result["modified"] == ["modified.txt"]
        assert result["unchanged"] == ["same.txt"]

    def test_get_cloud_diff_skips_blank_and_short_lines(self):
        result = self._run_diff_with_file("\n\nx\n+ ok.txt\n")
        assert result["added"] == ["ok.txt"]

    def test_get_cloud_diff_marks_partial_on_exit_2(self):
        from core import cloud_reporter

        real_mkstemp = tempfile.mkstemp

        def fake_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(suffix=".txt")
            os.write(fd, b"+ a.txt\n")
            return fd, path

        with patch.object(cloud_reporter.tempfile, "mkstemp", side_effect=fake_mkstemp), \
             patch("core.cloud_reporter.subprocess.run") as m:
            m.return_value = MagicMock(returncode=2, stdout="", stderr="checks failed")
            result = cloud_reporter.get_cloud_diff(
                source="E:\\", bucket="bucket", fy_prefix="FY26-27", config_path="/cfg",
            )
        assert result["_partial"] is True
        assert "exited 2" in result["_error"]


# ══════════════════════════════════════════════════════════════════════════
# lan_preflight — UNC host extraction and dry-run gating
# ══════════════════════════════════════════════════════════════════════════

class TestLanPreflightCharacterization:
    def test_extract_unc_host(self):
        from core.lan_preflight import _extract_unc_host

        assert _extract_unc_host(r"\\192.168.10.10\share") == "192.168.10.10"
        assert _extract_unc_host("//nas/share") == "nas"
        assert _extract_unc_host(r"D:\data") is None
        assert _extract_unc_host("relative/path") is None

    def test_dry_run_missing_canary_is_reported_for_g5(self, tmp_path):
        from core.lan_preflight import run_lan_dry_run

        dest = tmp_path / "dest"
        dest.mkdir()
        from test_support.fake_tools import fake_run_tool
        with fake_run_tool("core.lan_preflight", log_encoding="utf-16", exit_code=0):
            assert run_lan_dry_run(source=str(tmp_path), dest=str(dest))["canary_present"] is False

    def test_dry_run_with_canary_and_ok_exit(self, tmp_path):
        from core.lan_preflight import run_lan_dry_run

        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / ".AAM_TARGET_MOUNTED").write_text("")
        from test_support.fake_tools import fake_run_tool
        with fake_run_tool("core.lan_preflight", log_encoding="utf-16", exit_code=1):
            result = run_lan_dry_run(source=str(tmp_path), dest=str(dest))
        assert result["ok"] is True
        assert result["exit_code"] == 1

    def test_dry_run_high_exit_fails(self, tmp_path):
        from core.lan_preflight import run_lan_dry_run

        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / ".AAM_TARGET_MOUNTED").write_text("")
        from test_support.fake_tools import fake_run_tool
        with fake_run_tool("core.lan_preflight", log_encoding="utf-16", exit_code=8, log="copy error\n"):
            result = run_lan_dry_run(source=str(tmp_path), dest=str(dest))
        assert result["ok"] is False
        assert result["exit_code"] == 8

    def test_dry_run_unreachable_unc_raises(self):
        from core.health import HealthError
        from core.lan_preflight import run_lan_dry_run

        with patch("core.lan_preflight._is_smb_reachable", return_value=False):
            with pytest.raises(HealthError, match="Cannot reach"):
                run_lan_dry_run(source="E:\\", dest=r"\\10.0.0.9\share")


# ══════════════════════════════════════════════════════════════════════════
# wol — port guard, magic packet fan-out, wait/ensure
# ══════════════════════════════════════════════════════════════════════════

class TestWolCharacterization:
    def test_smb_port_open_rejects_invalid_port(self):
        from core.wol import _smb_port_open

        assert _smb_port_open("127.0.0.1", port=-1) is False
        assert _smb_port_open("127.0.0.1", port=99999) is False
        assert _smb_port_open("127.0.0.1", port="445") is False

    def test_send_magic_packet_rounds_and_broadcasts(self):
        from core.wol import _send_magic_packet

        with patch("core.wol.wol_send") as m, patch("core.wol.time.sleep") as s:
            _send_magic_packet("00:11:22:33:44:55", "192.168.1.255", repeat=3, interval=0)
        # 2 sends per round (global + subnet) x 3 rounds = 6
        assert m.call_count == 6
        # sleeps between rounds only
        assert s.call_count == 2

    def test_send_magic_packet_swallows_errors(self):
        from core.wol import _send_magic_packet

        with patch("core.wol.wol_send", side_effect=OSError("dead nic")), \
             patch("core.wol.time.sleep"):
            # Must not raise.
            _send_magic_packet("00:11:22:33:44:55", "255.255.255.255", repeat=1)

    def test_wait_for_server_success(self):
        from core.wol import wait_for_server

        with patch("core.wol._smb_port_open", return_value=True):
            wait_for_server("10.0.0.1", wake_timeout=5, ping_interval=1, stability_wait=0)

    def test_wait_for_server_timeout(self):
        from core.wol import WolTimeout, wait_for_server

        with patch("core.wol._smb_port_open", return_value=False), \
             patch("core.wol.time.sleep"):
            with pytest.raises(WolTimeout):
                wait_for_server("10.0.0.1", wake_timeout=1, ping_interval=1, stability_wait=0)

    def test_ensure_server_online_disabled(self):
        from core.wol import ensure_server_online

        cfg = MagicMock()
        cfg.wol.enabled = False
        assert ensure_server_online(cfg) is True

    def test_ensure_server_online_already_up(self):
        from core.wol import ensure_server_online

        cfg = MagicMock()
        cfg.wol.enabled = True
        cfg.wol.server_ip = "10.0.0.1"
        with patch("core.wol._smb_port_open", return_value=True):
            assert ensure_server_online(cfg) is True


# ══════════════════════════════════════════════════════════════════════════
# fy_rollover — path algebra, detection, folders, config rewrite, archive
# ══════════════════════════════════════════════════════════════════════════

class TestFyRolloverCharacterization:
    def test_fy_name_extraction(self):
        from core.fy_rollover import _fy_name

        assert _fy_name(r"E:\SOURCE\FY26-27") == "FY26-27"
        assert _fy_name(r"E:/source/fy25-26") == "FY25-26"
        assert _fy_name(r"E:\data") is None

    def test_parent_and_child_path(self):
        from core.fy_rollover import _child_path, _parent_path

        assert _parent_path(r"E:\src\FY26-27") == r"E:\src"
        assert _parent_path("A/B/FY26-27") == "A/B"
        assert _child_path("A/B", "FY27-28") == "A/B/FY27-28"
        assert _child_path(r"E:\src", "FY27-28") == r"E:\src\FY27-28"

    def test_detect_rollover_true_and_false(self):
        from core.fy_rollover import detect_rollover

        # A clearly-old FY in both paths -> rollover needed
        assert detect_rollover(r"E:\FY20-21", r"\\nas\share\FY20-21") is True
        # No FY folder anywhere -> rollover intentionally disabled
        assert detect_rollover(r"E:\data", r"\\nas\share") is False

    def test_create_new_fy_folders_source_and_lan(self, tmp_path):
        from core.fy_rollover import create_new_fy_folders

        src_root = tmp_path / "src"
        lan_root = tmp_path / "lan"
        src_root.mkdir()
        lan_root.mkdir()
        created = create_new_fy_folders(str(src_root), str(lan_root), "FY27-28")
        assert created["source"].exists()
        assert created["lan"].exists()
        assert (created["lan"] / ".AAM_TARGET_MOUNTED").exists()

    def test_create_new_fy_folders_lan_best_effort(self, tmp_path):
        from core.fy_rollover import create_new_fy_folders

        src_root = tmp_path / "src"
        src_root.mkdir()
        # LAN root points at a file so mkdir raises OSError -> must not raise.
        bad_lan = tmp_path / "afile"
        bad_lan.write_text("x")
        created = create_new_fy_folders(str(src_root), str(bad_lan), "FY27-28")
        assert created["source"].exists()
        assert "lan" not in created

    def test_update_config_yaml_round_trip(self, tmp_path):
        from core.fy_rollover import update_config_yaml

        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "firm_name: Test\n"
            "paths:\n"
            r"  source_drive: E:\src\FY26-27" + "\n"
            r"  lan_destination: \\nas\share\FY26-27" + "\n",
            encoding="utf-8",
        )
        update_config_yaml(str(cfg), r"E:\src", r"\\nas\share", "FY27-28")

        import yaml

        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert data["paths"]["source_drive"] == r"E:\src\FY27-28"
        assert data["paths"]["lan_destination"] == r"\\nas\share\FY27-28"

    def test_archive_transition_gcloud_missing_returns_false(self, tmp_path):
        from core.fy_rollover import run_archive_transition

        with patch("core.fy_rollover._resolve_gcloud", return_value=None):
            assert run_archive_transition("bucket", "FY25-26", str(tmp_path / "k.json")) is False

    def test_archive_transition_success(self, tmp_path):
        from core.fy_rollover import run_archive_transition

        key = tmp_path / "key.json"
        key.write_text("{}")
        with patch("core.fy_rollover._resolve_gcloud", return_value="/usr/bin/gcloud"), \
             patch("core.fy_rollover.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert run_archive_transition("bucket", "FY25-26", str(key)) is True

    def test_archive_transition_nonzero_returns_false(self, tmp_path):
        from core.fy_rollover import run_archive_transition

        key = tmp_path / "key.json"
        key.write_text("{}")
        with patch("core.fy_rollover._resolve_gcloud", return_value="/usr/bin/gcloud"), \
             patch("core.fy_rollover.subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="", stderr="denied")
            assert run_archive_transition("bucket", "FY25-26", str(key)) is False

    def test_archive_transition_timeout_returns_false(self, tmp_path):
        from core.fy_rollover import run_archive_transition

        key = tmp_path / "key.json"
        key.write_text("{}")
        with patch("core.fy_rollover._resolve_gcloud", return_value="/usr/bin/gcloud"), \
             patch("core.fy_rollover.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="gcloud", timeout=1)):
            assert run_archive_transition("bucket", "FY25-26", str(key)) is False

    def test_resolve_gcloud_returns_str_or_none(self):
        from core.fy_rollover import _resolve_gcloud

        result = _resolve_gcloud()
        assert result is None or isinstance(result, str)

    def test_run_final_backup_both_disabled(self):
        from core.fy_rollover import run_final_backup

        lan_cfg = MagicMock(enabled=False)
        cloud_cfg = MagicMock(enabled=False)
        config = MagicMock()
        config.wol.enabled = False
        assert run_final_backup(
            "E:/src", r"\\nas\share", lan_cfg, cloud_cfg, MagicMock(), config, "FY25-26"
        ) == (False, False)

    def test_run_final_backup_cloud_complete(self):
        from core.fy_rollover import run_final_backup

        lan_cfg = MagicMock(enabled=False)
        cloud_cfg = MagicMock(enabled=True)
        config = MagicMock()
        config.wol.enabled = False
        paths = MagicMock()
        with patch("core.fy_rollover.run_cloud_sync",
                   return_value={"status": "CLOUD_COMPLETE", "exit_code": 0}):
            cloud_ok, lan_ok = run_final_backup(
                "E:/src", r"\\nas\share", lan_cfg, cloud_cfg, paths, config, "FY25-26"
            )
        assert cloud_ok is True and lan_ok is False

    def test_run_final_backup_cloud_runtime_error_is_caught(self):
        from core.fy_rollover import run_final_backup

        lan_cfg = MagicMock(enabled=False)
        cloud_cfg = MagicMock(enabled=True)
        config = MagicMock()
        config.wol.enabled = False
        with patch("core.fy_rollover.run_cloud_sync", side_effect=OSError("disk gone")):
            cloud_ok, lan_ok = run_final_backup(
                "E:/src", r"\\nas\share", lan_cfg, cloud_cfg, MagicMock(), config, "FY25-26"
            )
        assert cloud_ok is False and lan_ok is False

    def test_rollover_no_fy_returns_false(self, tmp_path):
        from core.fy_rollover import rollover

        runtime = tmp_path / "runtime"
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "firm_name: Test\n"
            "paths:\n"
            "  source_drive: E:/data\n"
            "  lan_destination: '\\\\nas\\share'\n"
            f"  runtime_dir: '{runtime.as_posix()}'\n"
            "cloud:\n  enabled: false\n"
            "lan:\n  enabled: true\n"
            "wol:\n  enabled: false\n"
            "dashboard:\n  auth_enabled: false\n",
            encoding="utf-8",
        )
        # No FYxx-xx folder anywhere -> rollover is a deliberate no-op.
        assert rollover(str(cfg)) is False


# ══════════════════════════════════════════════════════════════════════════
# logging — configure creates dir, prefect bridge is safe outside Prefect
# ══════════════════════════════════════════════════════════════════════════

class TestLoggingCharacterization:
    @pytest.fixture(autouse=True)
    def _restore_loguru_state(self):
        """Restore the process-global loguru logger after each test here.

        These tests exercise core.logging.configure(), which mutates the
        global loguru logger (logger.remove() + a new enqueue file sink).
        Leaving that state behind makes any later logger.remove() in the same
        process raise OSError('Bad file descriptor') on Windows (see
        watchdog._configure_logging). We reset to a single clean stderr sink so
        the rest of the suite is unaffected.
        """
        yield
        import sys

        from loguru import logger as _logger

        from core import logging as app_logging

        app_logging._bridge_configured = False
        for hid in list(_logger._core.handlers):
            try:
                _logger.remove(hid)
            except Exception:
                _logger._core.handlers.pop(hid, None)
        _logger.add(sys.stderr, level="INFO")

    def test_configure_creates_log_dir(self, tmp_path):
        from core import logging as app_logging

        target = tmp_path / "logs"
        app_logging.configure(target, log_retention_days=7)
        assert target.exists()

    def test_configure_is_idempotent(self, tmp_path):
        from core import logging as app_logging

        app_logging.configure(tmp_path / "logs")
        app_logging.configure(tmp_path / "logs")  # must not raise

    def test_prefect_bridge_no_context_is_safe(self, tmp_path):
        from core import logging as app_logging
        from loguru import logger

        app_logging.configure(tmp_path / "logs")
        app_logging.configure_prefect_bridge()
        # Emitting outside a Prefect run must simply no-op, never raise.
        logger.info("characterization-log-line")

    def test_prefect_bridge_forwards_inside_flow_context(self, tmp_path):
        """Inside an active flow run, loguru lines are forwarded to Prefect."""
        from core import logging as app_logging
        from loguru import logger

        app_logging.configure(tmp_path / "logs")

        mock_logger = MagicMock()
        flow_ctx = MagicMock()
        flow_ctx.flow_run.id = "flow-run-123"

        with patch("prefect.context.FlowRunContext.get", return_value=flow_ctx), \
             patch("prefect.context.TaskRunContext.get", return_value=None), \
             patch("prefect.get_run_logger", return_value=mock_logger):
            app_logging.configure_prefect_bridge()
            logger.info("forward-me")
            logger.warning("warn-me")

        mock_logger.info.assert_any_call("forward-me")
        mock_logger.warning.assert_any_call("warn-me")

    def test_prefect_bridge_is_idempotent(self, tmp_path):
        from core import logging as app_logging

        app_logging.configure(tmp_path / "logs")
        with patch("prefect.context.FlowRunContext.get", return_value=None), \
             patch("prefect.context.TaskRunContext.get", return_value=None):
            app_logging.configure_prefect_bridge()
            app_logging.configure_prefect_bridge()  # second call must no-op


# ══════════════════════════════════════════════════════════════════════════
# report — CSV safety, CSV payload, HTML report, email gating
# ══════════════════════════════════════════════════════════════════════════

class TestReportCharacterization:
    def test_csv_safe_neutralizes_formula_prefixes(self):
        from core.report import _csv_safe

        assert _csv_safe("=1+1") == "'=1+1"
        assert _csv_safe("+cmd") == "'+cmd"
        assert _csv_safe("-2") == "'-2"
        assert _csv_safe("@ref") == "'@ref"
        assert _csv_safe("hello") == "hello"
        assert _csv_safe(None) == ""

    def test_generate_csv_data_header_and_rows(self):
        from core.report import _generate_csv_data

        runs = [{
            "started_at": "2026-01-01", "ended_at": "2026-01-01",
            "mode": "cloud", "status": "CLOUD_COMPLETE",
            "files_copied": 3, "files_failed": 0, "bytes_copied": 100,
            "duration_seconds": 5, "exit_code": 0,
            "error_message": "=DANGER", "extended_metrics": "",
        }]
        data = _generate_csv_data(runs)
        text = data.decode("utf-8")
        assert "Started At" in text
        assert "'=DANGER" in text  # injection neutralized

    def test_generate_report_html_empty_returns_blank(self, tmp_db):
        from core.report import generate_report_html

        assert generate_report_html(tmp_db, "Firm", 7, "Weekly") == ""

    def test_generate_report_html_with_runs(self, tmp_db):
        from core.report import generate_report_html
        from core.time_utils import now_iso

        tmp_db.insert_run({
            "run_id": "r1", "mode": "cloud", "started_at": now_iso(),
            "ended_at": now_iso(), "status": "CLOUD_COMPLETE", "exit_code": 0,
            "files_copied": 3, "bytes_copied": 1024, "duration_seconds": 5.0,
        })
        html = generate_report_html(tmp_db, "MyFirm", 7, "Weekly")
        assert "Weekly Backup Report" in html
        assert "MyFirm" in html
        assert "Success Rate" in html

    def test_send_summary_no_runs_returns_false(self, tmp_db):
        from core.report import send_summary_report

        cfg = NotificationConfig()
        assert send_summary_report(tmp_db, cfg, "Firm", 7, "Weekly") is False

    def test_send_email_unconfigured_returns_false(self):
        from core.report import _send_email_with_attachments

        cfg = NotificationConfig()  # no host/sender/recipients
        assert _send_email_with_attachments(cfg, "subj", "<html>") is False

    def test_send_email_missing_credentials_returns_false(self):
        from core.report import _send_email_with_attachments

        cfg = NotificationConfig(
            smtp_host="smtp.x", sender="s@x.com", recipients=["r@x.com"]
        )
        assert _send_email_with_attachments(cfg, "subj", "<html>") is False

    def test_send_email_success_via_smtp(self):
        from core.report import _send_email_with_attachments

        cfg = NotificationConfig(
            smtp_host="smtp.x", smtp_port=587, smtp_username="u",
            smtp_password="p", sender="s@x.com", recipients=["r@x.com"],
        )
        with patch("core.report.smtplib.SMTP") as m:
            m.return_value = MagicMock()
            assert _send_email_with_attachments(cfg, "subj", "<html>") is True

    def test_send_failure_alert_disabled(self, tmp_db):
        from core.report import send_failure_alert

        cfg = NotificationConfig(send_on_failure=False)
        assert send_failure_alert(cfg, "Firm", "boom", {"mode": "cloud"}) is False

    def test_send_failure_alert_long_error_attaches_file(self):
        from core.report import send_failure_alert

        cfg = NotificationConfig(send_on_failure=True)
        with patch("core.report._send_email_with_attachments", return_value=True) as m:
            ok = send_failure_alert(
                cfg, "Firm", "x" * 1500,
                {"mode": "cloud", "status": "CLOUD_FAILED", "exit_code": 7},
            )
        assert ok is True
        attachments = m.call_args[0][3]
        assert attachments and attachments[0]["filename"] == "failure_details.txt"
