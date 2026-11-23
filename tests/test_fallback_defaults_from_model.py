"""Function fallback defaults come from the config model (owner rule: every
tunable's default lives in models/config.py).

Found by the 2026-10-16 sweep: several core functions repeated a config
default as a literal, and five had drifted - ManifestDB vacuum threshold
1000 (model 10000), WoL magic-packet rounds 3 x 5 s (model 5 x 10 s), log
retention 30 days (model 90), rclone error tail 10 lines (model 100).
Production passes config values, so only direct callers saw the wrong
numbers - but a fallback that disagrees with the model is a trap.
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest

from core import cloud_sync, fy_rollover, health, lan_preflight, lan_sync, logging, manifest, wol
from models.config import (
    CloudConfig, HealthConfig, LanConfig, MaintenanceConfig, WolConfig, model_default,
)

CASES = [
    (manifest.ManifestDB.__init__, "busy_timeout_ms", MaintenanceConfig, "sqlite_busy_timeout_ms"),
    (manifest.ManifestDB.__init__, "vacuum_freelist_threshold", MaintenanceConfig,
     "sqlite_vacuum_freelist_threshold"),
    (manifest.ManifestDB.__init__, "synchronous", MaintenanceConfig, "sqlite_synchronous"),
    (manifest.ManifestDB.purge_old_runs, "retention_days", MaintenanceConfig, "db_retention_days"),
    (logging.configure, "log_retention_days", MaintenanceConfig, "log_retention_days"),
    (lan_sync.cleanup_orphaned_robocopy_logs, "max_age_hours", MaintenanceConfig,
     "orphan_robocopy_log_max_age_hours"),
    (wol._send_magic_packet, "repeat", WolConfig, "wake_retry_count"),
    (wol._send_magic_packet, "interval", WolConfig, "wake_retry_interval_seconds"),
    (wol._smb_port_open, "timeout", WolConfig, "smb_connect_timeout_seconds"),
    (wol.wait_for_server, "connect_timeout", WolConfig, "smb_connect_timeout_seconds"),
    (cloud_sync.scan_rclone_log_for_errors, "max_lines", CloudConfig, "error_tail_lines"),
    (cloud_sync.run_cloud_sync, "timeout", CloudConfig, "subprocess_timeout_seconds"),
    (cloud_sync.run_cloud_sync, "retries", CloudConfig, "retry_count"),
    (cloud_sync.run_cloud_sync, "transfers", CloudConfig, "transfers"),
    (cloud_sync.run_cloud_sync, "checkers", CloudConfig, "checkers"),
    (cloud_sync.run_cloud_sync, "bwlimit", CloudConfig, "bandwidth_limit"),
    (cloud_sync.run_cloud_sync, "buffer_size", CloudConfig, "buffer_size"),
    (cloud_sync.run_cloud_sync, "location", CloudConfig, "location"),
    (cloud_sync.run_cloud_sync, "error_tail_lines", CloudConfig, "error_tail_lines"),
    (cloud_sync.build_rclone_sync_command, "retries", CloudConfig, "retry_count"),
    (fy_rollover.run_archive_transition, "auth_timeout", HealthConfig, "rollover_auth_timeout_seconds"),
    (fy_rollover.run_archive_transition, "archive_timeout", HealthConfig,
     "rollover_archive_timeout_seconds"),
    (health.check_source_drive, "min_free_gb", HealthConfig, "min_free_source_gb"),
    (health.pre_backup_health, "max_clock_skew_seconds", HealthConfig, "max_clock_skew_seconds"),
    (health.pre_backup_health, "ntp_server", HealthConfig, "ntp_server"),
    (health.pre_backup_health, "w32tm_timeout_seconds", HealthConfig, "w32tm_timeout_seconds"),
    (lan_preflight.run_lan_dry_run, "timeout", LanConfig, "dry_run_timeout_seconds"),
    (lan_preflight.run_lan_dry_run, "smb_attempts", LanConfig, "preflight_smb_attempts"),
    (lan_preflight.run_lan_dry_run, "log_tail_kb", LanConfig, "log_tail_kb"),
]


@pytest.mark.parametrize("fn,param,model,field", CASES,
                         ids=[f"{c[0].__qualname__}.{c[1]}" for c in CASES])
def test_fallback_equals_model_default(fn, param, model, field):
    assert inspect.signature(fn).parameters[param].default == model_default(model, field)


def test_orphan_log_cleanup_age_comes_from_config():
    import flow
    cfg = MagicMock()
    cfg.cloud.enabled = True
    cfg.maintenance.orphan_robocopy_log_max_age_hours = 7
    with patch("flow.load_config", return_value=cfg), \
         patch("flow.configure_logging"), patch("flow.configure_prefect_bridge"), \
         patch("flow.cleanup_orphaned_robocopy_logs") as cleanup, \
         patch("flow._run_cloud_pipeline", return_value={"status": "CLOUD_COMPLETE"}), \
         patch("flow._run_lan_pipeline"):
        try:
            flow.backup.fn(config_path="x.yaml", mode="cloud")
        except Exception:  # noqa: BLE001 - only the cleanup argument matters here
            pass
    cleanup.assert_called_once_with(max_age_hours=7)


def test_sync_command_flags_come_from_the_parameters(tmp_path):
    (tmp_path / "a.txt").write_text("x")              # G1: the source must hold files
    cmd = cloud_sync.build_rclone_sync_command(
        str(tmp_path), "bkt", "FY26-27", "rc.conf", "STANDARD",
        modify_window_seconds=3, retries_sleep_seconds=45, stats_interval_seconds=120)
    assert cmd[cmd.index("--modify-window") + 1] == "3s"
    assert cmd[cmd.index("--retries-sleep") + 1] == "45s"
    assert cmd[cmd.index("--stats") + 1] == "120s"


def test_sync_flag_defaults_are_the_model_defaults():
    params = inspect.signature(cloud_sync.run_cloud_sync).parameters
    assert params["modify_window_seconds"].default == model_default(CloudConfig, "modify_window_seconds") == 2
    assert params["retries_sleep_seconds"].default == model_default(CloudConfig, "sync_retries_sleep_seconds") == 30
    assert params["stats_interval_seconds"].default == model_default(CloudConfig, "stats_interval_seconds") == 60


def test_flow_passes_the_sync_flag_keys():
    src = inspect.getsource(__import__("flow"))
    # the guard dry-run and the real sync (+ verify and diff for modify-window)
    for key, n in (("modify_window_seconds=config.cloud.modify_window_seconds", 4),
                   ("retries_sleep_seconds=config.cloud.sync_retries_sleep_seconds", 2),
                   ("stats_interval_seconds=config.cloud.stats_interval_seconds", 2)):
        assert src.count(key) == n, key


# Code review 2026-10-16: --modify-window now also drives the nightly upload.
# rclone sync treats a same-size file whose time is within the window as
# unchanged, so a wide window can silently skip a real edit (data loss).
@pytest.mark.parametrize("model,field", [(CloudConfig, "modify_window_seconds"),
                                         (__import__("models.config", fromlist=["AuditConfig"]).AuditConfig,
                                          "modify_window_seconds")])
def test_modify_window_is_bounded_to_a_safe_range(model, field):
    from pydantic import ValidationError
    extra = {"bucket": "aam-test-bucket", "project_number": "123456"} if model is CloudConfig else {}
    model(**extra, **{field: 10})
    with pytest.raises(ValidationError):
        model(**extra, **{field: 11})


def test_modify_window_changes_need_the_typed_confirmation():
    from core.config_edit import is_risky
    assert is_risky("cloud.modify_window_seconds")
    assert is_risky("audit.modify_window_seconds")
