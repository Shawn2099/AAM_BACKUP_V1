"""Session-2 remediation regression tests (2026-08-19).

Covers the fixes for: AUDIT-001 (serve wiring), AUDIT-002 (cron_to_human),
AUDIT-003 (bounded cloud stderr), AUDIT-004 (pre-phase failure statuses),
AUDIT-012 (wipe-risk guard), NA-01 (FY seed marker), NA-02 (mtime compare),
NA-03 (partial-walk manifest protection), NA-04 (strict concurrency),
NA-05 (trigger timeout=0 — in test_ui.py), NA-06 (slot-timeout record),
NA-07 (alert dedupe), NA-08 (/health disclosure), NV-02 (verify labeling),
and the FY-rollover exclusive lock.
"""

import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════
# AUDIT-001 — launch.main() must serve EVERY deployment
# ═══════════════════════════════════════════════════════════════

class TestLaunchServeWiring:
    """The old launch.py called deployments() → 5-tuple but passed only 4
    to serve() — the rollover-check deployment was silently never registered
    on the production Prefect server (verified live: exactly 4 deployments).
    This test drives the real launch.main() and asserts serve() received
    exactly what deployments() returned, so any future deployment added to
    deployments() must be served (or this test fails).
    """

    def test_serve_receives_all_deployments(self):
        import launch
        import prefect
        import serve as serve_mod

        d = (MagicMock(name="cloud"), MagicMock(name="lan"),
             MagicMock(name="weekly"), MagicMock(name="monthly"),
             MagicMock(name="rollover"))

        # launch.main() imports `from serve import deployments` and
        # `from prefect import serve` INSIDE the function, so patch the
        # source modules; the other main() steps are patched out.
        with patch("launch._check_prefect_api", return_value=True), \
             patch("launch._run_dashboard"), \
             patch("core.fy_rollover.rollover", return_value=False), \
             patch("launch._ensure_concurrency_limit"), \
             patch("launch._cancel_orphaned_runs"), \
             patch("models.config.load_config", return_value=MagicMock(
                 dashboard=MagicMock(bind_address="127.0.0.1", port=8080))), \
             patch.object(serve_mod, "deployments", return_value=d), \
             patch.object(prefect, "serve") as mock_serve:
            launch.main()

        assert mock_serve.called, "serve() was not called"
        served = list(mock_serve.call_args[0])  # positional deployment args
        # ignore non-deployment kwargs (pause_on_shutdown etc.)
        served += [v for v in mock_serve.call_args[1].values()
                   if v in d]
        assert served == list(d), \
            f"serve() got {served} — expected exactly deployments() returns {d}"
        # explicit: the rollover deployment must be among them (AUDIT-001)
        assert d[4] in served, "rollover-check deployment is not being served"


# ═══════════════════════════════════════════════════════════════
# NA-01 — FY seed marker + health gate
# ═══════════════════════════════════════════════════════════════

class TestFySeedMarker:
    def test_empty_source_still_unhealthy(self, tmp_path):
        from core.health import check_source_drive
        ok, reason = check_source_drive(str(tmp_path))
        assert not ok
        assert "empty" in reason.lower()

    def test_seed_only_source_is_healthy(self, tmp_path):
        """Post-rollover state: new FY folder holds only the seed marker —
        must NOT fail the health gate every night until data arrives."""
        from core.health import check_source_drive
        (tmp_path / ".AAM_SOURCE_SEEDED").write_text("seed\n")
        ok, reason = check_source_drive(str(tmp_path))
        assert ok, reason

    def test_populated_source_healthy(self, tmp_path):
        from core.health import check_source_drive
        (tmp_path / "data.txt").write_text("x")
        ok, _ = check_source_drive(str(tmp_path))
        assert ok

    def test_create_new_fy_folders_writes_seed(self, tmp_path):
        from core.fy_rollover import create_new_fy_folders
        src_root = tmp_path / "SRC"
        lan_root = tmp_path / "LAN"
        src_root.mkdir()
        created = create_new_fy_folders(str(src_root), str(lan_root), "FY27-28")
        seed = src_root / "FY27-28" / ".AAM_SOURCE_SEEDED"
        assert seed.is_file()
        content = seed.read_text()
        assert "FY27-28" in content

    def test_seed_excluded_from_robocopy_flags(self):
        from core.lan_sync import build_robocopy_command
        cfg = MagicMock()
        cfg.mt_threads = 4
        cfg.retry_count = 1
        cfg.retry_wait_seconds = 1
        cmd = build_robocopy_command("E:\\SRC", "\\\\srv\\share", cfg)
        assert ".AAM_SOURCE_SEEDED" in cmd
        # must be after the /XF switch
        assert cmd.index(".AAM_SOURCE_SEEDED") > cmd.index("/XF")

    def test_seed_excluded_from_rclone_flags(self):
        from core.cloud_sync import build_rclone_sync_command
        cmd = build_rclone_sync_command("E:\\SRC", "bucket", "FY27-28",
                                        "cfg", "STANDARD")
        assert ".AAM_SOURCE_SEEDED" in cmd
        assert cmd.index(".AAM_SOURCE_SEEDED") > cmd.index("--exclude")


# ═══════════════════════════════════════════════════════════════
# AUDIT-012 — wipe-risk guard
# ═══════════════════════════════════════════════════════════════

class TestWipeGuard:
    def _src(self, tmp_path, n=0, seed=False):
        src = tmp_path / "src"
        src.mkdir(exist_ok=True)
        for i in range(n):
            (src / f"f{i}.txt").write_text("x")
        if seed:
            (src / ".AAM_SOURCE_SEEDED").write_text("seed\n")
        return str(src)

    def test_empty_source_large_baseline_blocks(self, tmp_path):
        from core.wipe_guard import WipeRiskError, check_wipe_risk
        src = self._src(tmp_path, n=0)
        with pytest.raises(WipeRiskError):
            check_wipe_risk(src, previous_files=5000, min_files=100, min_ratio=0.5)

    def test_new_fy_seed_blocks_with_friendly_reason(self, tmp_path):
        from core.wipe_guard import WipeRiskError, check_wipe_risk
        src = self._src(tmp_path, n=0, seed=True)
        with pytest.raises(WipeRiskError) as ei:
            check_wipe_risk(src, previous_files=5000, min_files=100, min_ratio=0.5)
        assert "seed" in ei.value.reason.lower() or "fiscal" in ei.value.reason.lower()

    def test_small_dataset_below_min_files_passes(self, tmp_path):
        from core.wipe_guard import check_wipe_risk
        src = self._src(tmp_path, n=0)
        check_wipe_risk(src, previous_files=20, min_files=100, min_ratio=0.5)  # no raise

    def test_half_collapsed_source_blocks(self, tmp_path):
        from core.wipe_guard import WipeRiskError, check_wipe_risk
        src = self._src(tmp_path, n=49)  # baseline 5000 → threshold 2500
        with pytest.raises(WipeRiskError):
            check_wipe_risk(src, previous_files=5000, min_files=100, min_ratio=0.5)

    def test_healthy_source_passes(self, tmp_path):
        from core.wipe_guard import check_wipe_risk
        src = self._src(tmp_path, n=500)
        check_wipe_risk(src, previous_files=500, min_files=100, min_ratio=0.5)

    def test_walk_error_blocks(self, tmp_path):
        from core.wipe_guard import WipeRiskError, check_wipe_risk
        src = tmp_path / "src"
        src.mkdir()
        sub = src / "secret"
        sub.mkdir()
        (sub / "f.txt").write_text("x")
        os.chmod(sub, 0o000)
        try:
            with pytest.raises(WipeRiskError) as ei:
                check_wipe_risk(str(src), previous_files=5000,
                                min_files=100, min_ratio=0.5)
            assert "walk" in ei.value.reason.lower() or "error" in ei.value.reason.lower()
        finally:
            os.chmod(sub, 0o755)

    def test_config_fields_exist(self):
        from models.config import MaintenanceConfig
        m = MaintenanceConfig()
        assert m.wipe_guard_enabled is True
        assert m.wipe_guard_min_files == 100
        assert m.wipe_guard_min_ratio == 0.5

    def test_svi_only_source_counts_empty_and_blocks(self, tmp_path):
        """R4: a source holding ONLY System Volume Information + $RECYCLE.BIN
        (the AUDIT-012 SVI bypass state, live-verified on Windows) must count
        as EMPTY — even with thousands of shadow-copy/recycle files — so the
        guard blocks instead of letting the mirror wipe the destination."""
        from core.wipe_guard import WipeRiskError, check_wipe_risk
        src = tmp_path / "src"
        svi = src / "System Volume Information" / "5"
        rec = src / "$RECYCLE.BIN" / "S-1-5-21-123"
        svi.mkdir(parents=True)
        rec.mkdir(parents=True)
        for i in range(3000):
            (svi / f"shadow{i}.dat").write_text("x")
        for i in range(2000):
            (rec / f"deleted{i}.tmp").write_text("x")
        with pytest.raises(WipeRiskError) as ei:
            check_wipe_risk(str(src), previous_files=5000, min_files=100, min_ratio=0.5)
        # counted as empty (5000 junk files must not count as source data)
        assert ei.value.current_files == 0
        assert "EMPTY" in ei.value.reason

    def test_excluded_dirs_not_counted_in_ratio(self, tmp_path):
        """R4: excluded-directory files must not inflate the count — 49 user
        files + 5000 recycle-bin files against a 5000-file destination is a
        99% collapse of REAL data and must block (old code counted 5049 and
        let it through)."""
        from core.wipe_guard import WipeRiskError, check_wipe_risk
        src = tmp_path / "src"
        rec = src / "$RECYCLE.BIN"
        rec.mkdir(parents=True)
        for i in range(49):
            (src / f"real{i}.txt").write_text("x")
        for i in range(5000):
            (rec / f"junk{i}.tmp").write_text("x")
        with pytest.raises(WipeRiskError) as ei:
            check_wipe_risk(str(src), previous_files=5000, min_files=100, min_ratio=0.5)
        assert ei.value.current_files == 49


# ═══════════════════════════════════════════════════════════════
# R4 — mirror exclusions must be identical across sync/verify/diff
# ═══════════════════════════════════════════════════════════════

class TestMirrorExclusions:
    """The cloud sync, the post-sync verify check, and the diff check must
    all carry the same --exclude list (MIRROR_EXCLUDED_DIRS) as the LAN
    mirror's /XD list — otherwise sync and verify disagree on the file set
    and verify reports excluded directories as 'missing from cloud' (or,
    without the exclusions at all, deleted user files in $RECYCLE.BIN land
    in GCS)."""

    def _flag_pairs(self, cmd):
        """Return {flag: value} for all two-token flags in cmd."""
        pairs = {}
        for i, tok in enumerate(cmd):
            if tok.startswith("--") and i + 1 < len(cmd):
                pairs.setdefault(tok, []).append(cmd[i + 1])
        return pairs

    def test_sync_command_excludes_mirror_dirs(self):
        from core.cloud_sync import build_rclone_sync_command
        from core.lan_sync import MIRROR_EXCLUDED_DIRS
        cmd = build_rclone_sync_command("E:\\SRC", "bucket", "FY26-27", "cfg", "STANDARD")
        pairs = self._flag_pairs(cmd)
        for d in MIRROR_EXCLUDED_DIRS:
            assert d in pairs.get("--exclude", []), f"rclone sync must --exclude {d}"

    def test_verify_command_excludes_mirror_dirs(self):
        import subprocess
        from unittest.mock import patch
        from core.cloud_verify import verify_cloud_integrity
        from core.lan_sync import MIRROR_EXCLUDED_DIRS
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("core.cloud_verify.resolve_binary", return_value="rclone"), \
             patch("core.cloud_verify.subprocess.run", return_value=fake) as m:
            verify_cloud_integrity("E:\\SRC", "bucket", "FY26-27", "cfg")
        cmd = m.call_args[0][0]
        pairs = self._flag_pairs(cmd)
        for d in MIRROR_EXCLUDED_DIRS:
            assert d in pairs.get("--exclude", []), f"rclone check must --exclude {d}"

    def test_diff_command_excludes_mirror_dirs(self):
        import subprocess
        from unittest.mock import patch
        from core.cloud_reporter import get_cloud_diff
        from core.lan_sync import MIRROR_EXCLUDED_DIRS
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.subprocess.run", return_value=fake) as m:
            get_cloud_diff("E:\\SRC", "bucket", "FY26-27", "cfg")
        cmd = m.call_args[0][0]
        pairs = self._flag_pairs(cmd)
        for d in MIRROR_EXCLUDED_DIRS:
            assert d in pairs.get("--exclude", []), f"rclone check --combined must --exclude {d}"

    def test_robocopy_and_rclone_exclusion_sets_match(self):
        """Single source of truth: the /XD list and the --exclude lists all
        derive from MIRROR_EXCLUDED_DIRS, so they cannot drift apart."""
        from core.lan_sync import MIRROR_EXCLUDED_DIRS, build_robocopy_command
        from core.cloud_sync import build_rclone_sync_command
        cfg = MagicMock()
        cfg.mt_threads = 4
        cfg.retry_count = 1
        cfg.retry_wait_seconds = 1
        robocopy = build_robocopy_command("E:\\SRC", "\\\\srv\\share", cfg)
        rclone = build_rclone_sync_command("E:\\SRC", "b", "FY26-27", "c", "STANDARD")
        xd_idx = robocopy.index("/XD")
        assert robocopy[xd_idx + 1: xd_idx + 1 + len(MIRROR_EXCLUDED_DIRS)] == list(MIRROR_EXCLUDED_DIRS)
        pairs = self._flag_pairs(rclone)
        assert all(d in pairs["--exclude"] for d in MIRROR_EXCLUDED_DIRS)


# ═══════════════════════════════════════════════════════════════
# NA-02 — mtime normalization (production-proven metric corruption)
# ═══════════════════════════════════════════════════════════════

class TestMtimeNormalization:
    def test_epoch_float_passthrough(self):
        import flow
        assert flow._mtime_to_epoch(1784992800.0) == 1784992800.0
        assert flow._mtime_to_epoch(1784992800) == 1784992800.0

    def test_numeric_string(self):
        import flow
        assert flow._mtime_to_epoch("1784992800.5") == 1784992800.5

    def test_iso_with_nanos_and_offset(self):
        import flow
        from datetime import datetime, timezone, timedelta
        # 2026-08-18T15:50:55.123456789+05:30 == 2026-08-18T10:20:55.123456789Z
        ref = datetime(2026, 8, 18, 10, 20, 55, 123456, tzinfo=timezone.utc)
        v = flow._mtime_to_epoch("2026-08-18T15:50:55.123456789+05:30")
        assert v is not None
        assert abs(v - ref.timestamp()) < 0.01

    def test_garbage_returns_none(self):
        import flow
        assert flow._mtime_to_epoch("not-a-date") is None
        assert flow._mtime_to_epoch("") is None
        assert flow._mtime_to_epoch(None) is None

    def test_production_scenario_no_false_difference(self):
        """The exact production bug: LAN upsert writes an epoch float, the
        next cloud run sees the same instant as a GCS ISO string. Old code:
        pendulum.parse(str(float)) raises → string compare → ALWAYS counted
        as copied. New code: both normalize to the same epoch → equal."""
        import flow
        t = 1784992800.0
        iso = "2026-07-01T14:40:00+00:00"  # placeholder; compare via helper
        # same instant expressed both ways:
        import pendulum
        iso_same = pendulum.from_timestamp(t, tz="UTC").to_iso8601_string()
        t1 = flow._mtime_to_epoch(t)          # DB value (LAN float)
        t2 = flow._mtime_to_epoch(iso_same)   # GCS ModTime (ISO string)
        assert t1 is not None and t2 is not None
        assert abs(t1 - t2) <= 1.1  # → NOT counted as copied


# ═══════════════════════════════════════════════════════════════
# NA-03 — partial walk protection
# ═══════════════════════════════════════════════════════════════

class TestDetailedWalk:
    def test_counts_walk_errors(self, tmp_path):
        from core.lan_manifest import walk_lan_destination_detailed
        sub = tmp_path / "noperm"
        sub.mkdir()
        (sub / "f.txt").write_text("x")
        (tmp_path / "ok.txt").write_text("y")
        os.chmod(sub, 0o000)
        try:
            files, errors = walk_lan_destination_detailed(str(tmp_path))
        finally:
            os.chmod(sub, 0o755)
        assert errors >= 1
        # the readable file is still reported
        assert any(f["path"].endswith("ok.txt") for f in files)

    def test_clean_walk_zero_errors(self, tmp_path):
        from core.lan_manifest import walk_lan_destination_detailed
        (tmp_path / "a.txt").write_text("x")
        files, errors = walk_lan_destination_detailed(str(tmp_path))
        assert errors == 0
        assert len(files) == 1

    def test_compat_wrapper_unchanged(self, tmp_path):
        from core.lan_manifest import walk_lan_destination
        (tmp_path / "a.txt").write_text("x")
        files = walk_lan_destination(str(tmp_path))
        assert isinstance(files, list)
        assert files[0]["path"].endswith("a.txt")


# ═══════════════════════════════════════════════════════════════
# NA-04 — strict concurrency
# ═══════════════════════════════════════════════════════════════

class TestStrictConcurrency:
    def test_backup_passes_strict_true(self):
        import flow
        with patch("flow.concurrency") as mock_conc, \
             patch("flow.write_lock"), \
             patch("flow.load_config") as mock_cfg, \
             patch("flow.configure_logging"), \
             patch("flow.configure_prefect_bridge"), \
             patch("flow.cleanup_orphaned_robocopy_logs"):
            cfg = MagicMock()
            cfg.cloud.enabled = False
            cfg.lan.enabled = False
            mock_cfg.return_value = cfg
            cm = MagicMock()
            cm.__enter__.return_value = None
            cm.__exit__.return_value = False
            mock_conc.return_value = cm
            flow.backup.fn("config.yaml", "all")
            args, kwargs = mock_conc.call_args
            assert kwargs.get("strict") is True, \
                "NA-04: concurrency() must be strict=True so a missing limit fails loudly"


# ═══════════════════════════════════════════════════════════════
# NA-06 — slot-timeout leaves a record and an alert
# ═══════════════════════════════════════════════════════════════

class TestSlotTimeout:
    def test_slot_timeout_records_and_alerts(self):
        import flow
        with patch("flow.concurrency", side_effect=TimeoutError("slot timeout")), \
             patch("flow.load_config") as mock_cfg, \
             patch("flow.configure_logging"), \
             patch("flow.configure_prefect_bridge"), \
             patch("flow.cleanup_orphaned_robocopy_logs"), \
             patch("flow._record_run") as mock_record, \
             patch("flow.send_failure_alert") as mock_alert:
            cfg = MagicMock()
            cfg.cloud.enabled = True
            cfg.lan.enabled = True
            cfg.paths.database_path = "/tmp/test.db"
            cfg.notifications = MagicMock()
            cfg.firm_name = "Test"
            mock_cfg.return_value = cfg
            with pytest.raises(TimeoutError):
                flow.backup.fn("config.yaml", "all")
            # both enabled pipelines got a FAILED record
            assert mock_record.call_count == 2
            modes = {c.args[2] for c in mock_record.call_args_list}
            assert modes == {"cloud", "lan"}
            statuses = {c.args[4] for c in mock_record.call_args_list}
            assert statuses == {"CLOUD_FAILED", "LAN_FAILED"}
            mock_alert.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# NA-07 — verify-failure alert dedupe
# ═══════════════════════════════════════════════════════════════

class TestAlertDedupe:
    def test_all_deduped_suppresses_summary(self):
        import flow

        marked = RuntimeError("verify failed")
        marked._dedupe_alert = True

        with patch("flow.concurrency") as mock_conc, \
             patch("flow.write_lock"), \
             patch("flow.load_config") as mock_cfg, \
             patch("flow.configure_logging"), \
             patch("flow.configure_prefect_bridge"), \
             patch("flow.cleanup_orphaned_robocopy_logs"), \
             patch("flow._run_cloud_pipeline", side_effect=marked), \
             patch("flow._run_lan_pipeline"), \
             patch("flow.send_failure_alert") as mock_alert:
            cfg = MagicMock()
            cfg.cloud.enabled = True
            cfg.lan.enabled = True
            mock_cfg.return_value = cfg
            cm = MagicMock()
            cm.__enter__.return_value = None
            cm.__exit__.return_value = False
            mock_conc.return_value = cm
            with pytest.raises(BaseException):
                flow.backup.fn("config.yaml", "all")
            # cloud raised a deduped alert; LAN raised nothing → the ONLY
            # exception in the group is deduped → summary suppressed.
            # (LAN pipeline succeeded here, so excs == [marked])
            mock_alert.assert_not_called()

    def test_non_deduped_failure_still_alerts(self):
        import flow

        plain = RuntimeError("sync failed")  # no _dedupe_alert

        with patch("flow.concurrency") as mock_conc, \
             patch("flow.write_lock"), \
             patch("flow.load_config") as mock_cfg, \
             patch("flow.configure_logging"), \
             patch("flow.configure_prefect_bridge"), \
             patch("flow.cleanup_orphaned_robocopy_logs"), \
             patch("flow._run_cloud_pipeline", side_effect=plain), \
             patch("flow._run_lan_pipeline"), \
             patch("flow.send_failure_alert") as mock_alert:
            cfg = MagicMock()
            cfg.cloud.enabled = True
            cfg.lan.enabled = True
            cfg.notifications = MagicMock()
            cfg.firm_name = "Test"
            mock_cfg.return_value = cfg
            cm = MagicMock()
            cm.__enter__.return_value = None
            cm.__exit__.return_value = False
            mock_conc.return_value = cm
            with pytest.raises(BaseException):
                flow.backup.fn("config.yaml", "all")
            mock_alert.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# AUDIT-004 — pre-phase failures are FAILED, not SKIPPED
# ═══════════════════════════════════════════════════════════════

class TestPrePhaseStatus:
    """AUDIT-004: pre-phase (health/preflight) failures must be recorded as
    FAILED, not SKIPPED — 5 weeks of production LAN outages were hidden as
    'LAN_SKIPPED' in the reports.

    Integration style: REAL ManifestDB + real _record_run, only the external
    steps (health/preflight/sync) are mocked.
    """

    def _config(self, tmp_path):
        cfg = MagicMock()
        cfg.paths.database_path = str(tmp_path / "m.db")
        cfg.paths.source_drive = str(tmp_path / "src")
        cfg.cloud.enabled = True
        cfg.cloud.max_attempts = 1
        cfg.cloud.retry_delay_seconds = 0
        cfg.lan.enabled = True
        cfg.lan.max_attempts = 1
        cfg.lan.retry_delay_seconds = 0
        cfg.maintenance.sqlite_busy_timeout_ms = 30000
        cfg.maintenance.sqlite_vacuum_freelist_threshold = 10000
        cfg.maintenance.wipe_guard_enabled = True
        cfg.maintenance.wipe_guard_min_files = 100
        cfg.maintenance.wipe_guard_min_ratio = 0.5
        return cfg

    def _seed_manifest(self, tmp_path, mode, n):
        """Pre-populate file_entries the way a real sync would."""
        from core.manifest import ManifestDB
        from core.backup_repository import record_sync_results
        db = ManifestDB(str(tmp_path / "m.db"))
        try:
            record_sync_results(
                db, mode,
                [{"path": f"file{i}.txt", "size": 10, "mtime": 1.0}
                 for i in range(n)],
            )
        finally:
            db.close()

    def _read_status(self, tmp_path, run_id):
        db = sqlite3.connect(str(tmp_path / "m.db"))
        row = db.execute(
            "SELECT status, error_message FROM run_history WHERE run_id=?",
            (run_id,),
        ).fetchone()
        db.close()
        return row

    def test_cloud_health_failure_is_preflight_failed(self, tmp_path):
        import flow
        cfg = self._config(tmp_path)
        with patch.object(flow, "health_check_task",
                          side_effect=RuntimeError("source not accessible")), \
             patch.object(flow, "get_fy_prefix", return_value="FY26-27"):
            with pytest.raises(RuntimeError):
                flow._run_cloud_pipeline(
                    cfg, "run-1", "2026-08-19T00:00:00+00:00")
        status, err = self._read_status(tmp_path, "run-1")
        assert status == "CLOUD_PREFLIGHT_FAILED", \
            f"AUDIT-004: pre-phase failure must not be SKIPPED (got {status})"
        assert "source not accessible" in err

    def test_lan_preflight_failure_is_preflight_failed(self, tmp_path):
        import flow
        cfg = self._config(tmp_path)
        # with_options(retries=...) must return the SAME mock so the
        # side_effect survives the retry-wrapper (the pipeline wraps the
        # preflight task with retry options).
        mock_preflight = MagicMock(
            side_effect=RuntimeError("SMB not accessible"))
        mock_preflight.with_options.return_value = mock_preflight
        with patch.object(flow, "health_check_task"), \
             patch.object(flow, "wol_check_task"), \
             patch.object(flow, "lan_preflight_task", mock_preflight), \
             patch.object(flow, "get_fy_prefix", return_value="FY26-27"):
            with pytest.raises(RuntimeError):
                flow._run_lan_pipeline(
                    cfg, "run-2", "2026-08-19T00:00:00+00:00")
        status, err = self._read_status(tmp_path, "run-2")
        assert status == "LAN_PREFLIGHT_FAILED"
        assert "SMB not accessible" in err

    def test_wipe_guard_blocks_cloud_with_distinct_status(self, tmp_path):
        """AUDIT-012 integration: GCS destination holds 5000 objects, 0 files
        on the source → the sync never runs; the run is recorded as
        CLOUD_WIPE_RISK_BLOCKED with the reason. (The baseline is the LIVE
        destination count, not the manifest — see the flow.py guard.)"""
        import flow
        cfg = self._config(tmp_path)
        src = tmp_path / "src"
        src.mkdir()
        cfg.paths.source_drive = str(src)
        cfg.cloud.bucket = "bucket"
        cfg.cloud.location = "asia-south1"
        cfg.cloud.project_number = "123"
        cfg.cloud.storage_class = "STANDARD"
        cfg.paths.gcs_key_path = str(tmp_path / "key.json")
        (tmp_path / "key.json").write_text("{}")
        with patch.object(flow, "health_check_task"), \
             patch.object(flow, "cloud_preflight_task"), \
             patch.object(flow, "cloud_sync_task") as mock_sync, \
             patch.object(flow, "temp_rclone_config") as mock_tcfg, \
             patch.object(flow, "get_cloud_size",
                          return_value={"count": 5000, "bytes": 0}), \
             patch.object(flow, "get_fy_prefix", return_value="FY26-27"):
            mock_tcfg.__enter__.return_value = "/tmp/fake-rclone-conf"
            mock_tcfg.__exit__.return_value = False
            # the pipeline re-raises the WipeRiskError after recording the
            # blocked status (backup() collects it for the summary alert)
            with pytest.raises(flow.wipe_guard.WipeRiskError):
                flow._run_cloud_pipeline(
                    cfg, "run-3", "2026-08-19T00:00:00+00:00")
            # sync must never run when the guard blocks (R5: this was a no-op
            # tuple expression — a discarded assert_not_called() call)
            assert not mock_sync.called, "sync must not run when the guard blocks"
        status, err = self._read_status(tmp_path, "run-3")
        assert status == "CLOUD_WIPE_RISK_BLOCKED"
        assert "EMPTY" in err or "empty" in err

    def test_wipe_guard_silent_when_destination_empty(self, tmp_path):
        """Post-rollover: the new FY GCS prefix is empty (count=0) and the
        new source is empty too → guard silent, sync proceeds. Mirroring
        empty→empty is harmless; the health seed marker lets the run start.
        (NA-01 companion — without this, the guard would block all year.)"""
        import flow
        cfg = self._config(tmp_path)
        src = tmp_path / "src"
        src.mkdir()
        (src / ".AAM_SOURCE_SEEDED").write_text("seed\n")
        cfg.paths.source_drive = str(src)
        cfg.cloud.bucket = "bucket"
        cfg.cloud.location = "asia-south1"
        cfg.cloud.project_number = "123"
        cfg.cloud.storage_class = "STANDARD"
        cfg.paths.gcs_key_path = str(tmp_path / "key.json")
        (tmp_path / "key.json").write_text("{}")
        # The pipeline wraps sync/verify with with_options(retries=...), so the
        # patched mocks must pass through .with_options() unchanged — otherwise
        # the auto-created child mock swallows the configured return_value
        # (same trick as test_lan_preflight_failure_is_preflight_failed).
        mock_sync = MagicMock(return_value={
            "status": "CLOUD_NO_CHANGES_COMPLETE", "exit_code": 9, "error": None})
        mock_sync.with_options.return_value = mock_sync
        mock_verify = MagicMock(return_value={
            "verified": True, "diff": {}, "manifest": [],
            "size": {"count": 0, "bytes": 0}})
        mock_verify.with_options.return_value = mock_verify
        with patch.object(flow, "health_check_task"), \
             patch.object(flow, "cloud_preflight_task"), \
             patch.object(flow, "cloud_sync_task", mock_sync), \
             patch.object(flow, "cloud_verify_and_report_task", mock_verify), \
             patch.object(flow, "cloud_record_task"), \
             patch.object(flow, "cloud_publish_artifact_task"), \
             patch.object(flow, "temp_rclone_config") as mock_tcfg, \
             patch.object(flow, "get_cloud_size",
                          return_value={"count": 0, "bytes": 0}), \
             patch.object(flow, "get_fy_prefix", return_value="FY27-28"):
            mock_tcfg.__enter__.return_value = "/tmp/fake-rclone-conf"
            mock_tcfg.__exit__.return_value = False
            result = flow._run_cloud_pipeline(
                cfg, "run-4", "2026-08-19T00:00:00+00:00")
        # ran to completion (guard did not block)
        assert result["status"] == "CLOUD_NO_CHANGES_COMPLETE"


# ═══════════════════════════════════════════════════════════════
# AUDIT-002 — cron_to_human must never raise
# ═══════════════════════════════════════════════════════════════

class TestCronToHumanRobust:
    @pytest.mark.parametrize("cron", [
        "*/5 8 * * *",        # step minute
        "0,30 8 * * *",       # list minute
        "0 0-6/2 * * *",      # range+step hour
        "0 8 * * 1,3,5",      # list day-of-week
        "5,20 4 1,15 * *",    # lists everywhere
        "0 18 * * *",         # simple daily (regression)
        "0 8 * * MON",        # simple dow (regression)
        "0 8 1 * *",          # simple dom (regression)
        "invalid",            # 1 field (regression)
        "60 25 32 13 8",      # out-of-range ints must not raise
    ])
    def test_never_raises(self, cron):
        from core.time_utils import cron_to_human
        out = cron_to_human(cron, "Asia/Kolkata")
        assert isinstance(out, str) and out

    def test_simple_forms_unchanged(self):
        from core.time_utils import cron_to_human
        assert cron_to_human("0 18 * * *", "Asia/Kolkata") == "Daily at 18:00 Kolkata"
        assert cron_to_human("0 8 * * MON", "Asia/Kolkata") == "Every Monday at 08:00 Kolkata"
        assert cron_to_human("0 8 1 * *", "Asia/Kolkata") == "1st of month at 08:00 Kolkata"


# ═══════════════════════════════════════════════════════════════
# AUDIT-003 — bounded cloud stderr
# ═══════════════════════════════════════════════════════════════

class TestBoundedStderr:
    def test_tail_truncates_large_files(self, tmp_path):
        from core.cloud_sync import _read_tail
        p = tmp_path / "big.log"
        p.write_bytes(b"head\n" + b"x" * 200_000 + b"\nTAIL-MARKER")
        text = _read_tail(p, 100_000)
        assert "truncated" in text
        assert "TAIL-MARKER" in text          # end preserved
        assert not text.startswith("head")    # head dropped
        assert len(text) <= 100_000 + 200

    def test_tail_small_file_unchanged(self, tmp_path):
        from core.cloud_sync import _read_tail
        p = tmp_path / "small.log"
        p.write_text("full content")
        assert _read_tail(p, 100_000) == "full content"


# ═══════════════════════════════════════════════════════════════
# NV-02 — verify failure classification
# ═══════════════════════════════════════════════════════════════

class TestVerifyClassification:
    def test_access_error_distinguished(self):
        from core.cloud_verify import _classify_check_failure
        stderr = (
            "ERROR : GCS bucket x: error reading destination root directory: 401\n"
            "NOTICE: 3 files missing\n"
            "NOTICE: 3 differences found\n"
            "NOTICE: 4 errors while checking\n"
        )
        assert _classify_check_failure(1, stderr) == "access_error"

    def test_true_mismatch(self):
        from core.cloud_verify import _classify_check_failure
        stderr = "NOTICE: 2 differences found\n"
        assert _classify_check_failure(1, stderr) == "mismatch"

    def test_exit2_is_error(self):
        from core.cloud_verify import _classify_check_failure
        assert _classify_check_failure(2, "usage error") == "error"

    def test_zero_is_none(self):
        from core.cloud_verify import _classify_check_failure
        assert _classify_check_failure(0, "") is None

    def test_message_labels_access_error(self):
        from core.cloud_verify import _build_error_message
        msg = _build_error_message(1, "access_error")
        assert "NOT a confirmed integrity mismatch" in msg


# ═══════════════════════════════════════════════════════════════
# NA-08 — /health must not disclose the source path
# ═══════════════════════════════════════════════════════════════

class TestHealthDisclosure:
    def test_health_no_path_disclosure(self):
        from fastapi.testclient import TestClient
        import ui
        client = TestClient(ui.app)
        with patch("ui._cfg", return_value=MagicMock(
                paths=MagicMock(source_drive=r"C:\SECRET\DATA"))):
            response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "source_drive" not in data, \
            "NA-08: /health must not disclose the source path"
        assert "source_accessible" in data


# ═══════════════════════════════════════════════════════════════
# Rollover exclusive lock (AUDIT-001 companion)
# ═══════════════════════════════════════════════════════════════

class TestExclusiveLock:
    def test_acquire_release(self, tmp_path):
        from core.process import acquire_exclusive_lock, release_exclusive_lock
        lp = tmp_path / "r.lock"
        assert acquire_exclusive_lock(lp) is True
        release_exclusive_lock(lp)
        assert not lp.exists()

    def test_fresh_lock_blocks_second_acquirer(self, tmp_path):
        """A live holder's lock must NOT be stealable (this is what stops the
        boot-time rollover and the scheduled rollover from running at once).
        Simulated with a real long-lived PID (this test process itself)."""
        from core.process import acquire_exclusive_lock, release_exclusive_lock
        import os
        lp = tmp_path / "r.lock"
        # write a lock owned by THIS process (alive)
        from core.process import _get_create_time
        content = f"{os.getpid()}:{_get_create_time(os.getpid()):.6f}"
        lp.write_text(content)
        # same PID = re-entrant? No — acquire_exclusive_lock does O_EXCL, so a
        # second acquire from the same process fails on FileExistsError, then
        # sees the lock is alive (itself) → returns False.
        assert acquire_exclusive_lock(lp) is False

    def test_stale_lock_is_stolen(self, tmp_path):
        from core.process import acquire_exclusive_lock
        lp = tmp_path / "r.lock"
        # dead PID (very unlikely to exist, and even if it did, create_time
        # won't match → stale)
        lp.write_text("999999:1234567890.123456")
        assert acquire_exclusive_lock(lp) is True

    def test_release_never_deletes_others_lock(self, tmp_path):
        from core.process import release_exclusive_lock
        lp = tmp_path / "r.lock"
        lp.write_text("424242:1234567890.123456")
        release_exclusive_lock(lp)
        assert lp.exists()
