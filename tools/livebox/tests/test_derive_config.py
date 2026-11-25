"""Red tests for tools.livebox.derive_config (plan LIVEBOX_TEST_PLAN_2026-11-24.md, section 1.2 and 1.8).

Contract under test (the implementation is written later, by another agent):

    derive_config(prod_raw: dict, lane_dir: pathlib.Path, **overrides) -> dict

- prod_raw is the production config as a plain dict (yaml load, deprecated keys already dropped).
  It is never mutated.
- lane_dir is C:\\AAMLivebox\\<stamp>\\<lane>. The stamp is lane_dir.parent.name.
- Overrides used by these tests (keyword names are the contract for the implementation):
    smtp_port=int                 sandbox fake SMTP port (notifications.smtp_port)
    has_backup_privilege=bool     True if whoami /priv shows SeBackupPrivilege for the ssh user
    retention_days=int            lane retention (run_logs.retention_days and maintenance.db_retention_days)
    log_directory=str             a PRODUCTION-style value; must be refused
    database_path=str             a PRODUCTION-style value; must be refused
    run_logs_dir=str              a PRODUCTION-style value; must be refused
- Refusals raise ValueError or RuntimeError (these tests accept either).

The module is imported lazily inside each helper, so while the module does not exist every test
fails with ImportError at call time (not at collection).
"""

from __future__ import annotations

import copy
import pathlib
import re
from pathlib import PureWindowsPath

import pytest

# ---------------------------------------------------------------------------
# Fixtures: a synthetic production config (never the real config.yaml).
# ---------------------------------------------------------------------------

PROD_SOURCE = r"E:\FY26-27"
PROD_NAS_SHARE = r"\\PRODNAS01\backup$\FY26-27"
PROD_RUNTIME = r"C:\BackupAgent"
PROD_DB = r"C:\BackupAgent\manifest.db"
PROD_LOG_DIR = r"C:\BackupAgent\logs"
PROD_RUN_LOGS_DIR = r"C:\BackupAgent\run_logs"
PROD_GCS_KEY = r"C:\BackupAgent\secrets\prod-key.json"
PROD_BUCKET = "aam-prod-bucket"
PROD_SMTP_HOST = "smtp.prod-mail.example"
PROD_SMTP_USER = "prod-user@prod.example"
PROD_SMTP_PASSWORD = "PROD-SECRET-PW"
PROD_SENDER = "alerts@prod.example"
PROD_RECIPIENT = "owner@prod.example"
PROD_REPORT_RECIPIENT = "partners@prod.example"
PROD_NTP = "time.prod.example"

PROD_RAW: dict = {
    "paths": {
        "source_drive": PROD_SOURCE,
        "lan_destination": PROD_NAS_SHARE,
        "runtime_dir": PROD_RUNTIME,
        "database_path": PROD_DB,
        "log_directory": PROD_LOG_DIR,
        "gcs_key_path": PROD_GCS_KEY,
    },
    "cloud": {
        "enabled": True,
        "bucket": PROD_BUCKET,
        "retry_delay_seconds": 300,
    },
    "lan": {
        "enabled": True,
        "shutdown_after_backup": True,
        "use_backup_mode": True,
        "retry_wait_seconds": 10,
    },
    "wol": {
        "enabled": True,
        "mac_address": "AA:BB:CC:DD:EE:FF",
        "server_ip": "192.168.10.10",
    },
    "notifications": {
        "smtp_host": PROD_SMTP_HOST,
        "smtp_port": 587,
        "smtp_security": "starttls",
        "smtp_auth": True,
        "smtp_username": PROD_SMTP_USER,
        "smtp_password": PROD_SMTP_PASSWORD,
        "sender": PROD_SENDER,
        "recipients": [PROD_RECIPIENT],
        "report_recipients": [PROD_REPORT_RECIPIENT],
        "retry_attempts": 3,
        "retry_base_seconds": 10,
    },
    "guards": {
        "forbid_local_destination": True,
    },
    "health": {
        "ntp_server": PROD_NTP,
        "w32tm_timeout_seconds": 30,
    },
    "run_logs": {
        "dir": PROD_RUN_LOGS_DIR,
        "retention_days": 90,
    },
    "maintenance": {
        "db_retention_days": 90,
        "log_retention_days": 90,
    },
    "rollover": {
        "overlap_enabled": True,
        "overlap_activate_on": "12-31",
    },
    # A section the derivation must keep untouched (rule: keep every production key not listed).
    "audit": {
        "checkers": 4,
    },
}

LANE_STAMP = "20261124-0100"
LANE_DIR = pathlib.Path(r"C:\AAMLivebox\20261124-0100\A")
SMTP_PORT = 2525

EXPECTED_SOURCE = r"C:\AAMLivebox\20261124-0100\A\src\FY90-91"
EXPECTED_LAN_DESTINATION = r"\\127.0.0.1\C$\AAMLivebox\20261124-0100\A\nas\backup\FY90-91"
EXPECTED_RUNTIME = r"C:\AAMLivebox\20261124-0100\A\runtime"
EXPECTED_GCS_KEY = r"C:\AAMLivebox\20261124-0100\A\runtime\dummy-key.json"
EXPECTED_BUCKET = "aam-livebox-local-20261124-0100"

TEST_FY_LEAVES = {"FY90-91", "FY91-92", "FY92-93", "FY93-94"}
REFUSAL = (ValueError, RuntimeError)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derive(**overrides) -> dict:
    """Call the future implementation on a deep copy of the production fixture."""
    from tools.livebox.derive_config import derive_config  # lazy: red with ImportError now

    return derive_config(copy.deepcopy(PROD_RAW), LANE_DIR, **overrides)


def _get(cfg: dict, dotted: str):
    node = cfg
    for part in dotted.split("."):
        node = node[part]
    return node


def _has(cfg: dict, dotted: str) -> bool:
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _same_win_path(actual, expected: str) -> bool:
    return PureWindowsPath(str(actual)) == PureWindowsPath(expected)


def _all_strings(obj):
    """Yield every string in a nested dict/list structure (keys and values)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _all_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_strings(v)
    elif isinstance(obj, str):
        yield obj
    elif obj is not None and not isinstance(obj, (bool, int, float)):
        yield str(obj)


def _all_values_text(obj) -> str:
    return "\n".join(_all_strings(obj))


# ---------------------------------------------------------------------------
# Table 1.2, row by row
# ---------------------------------------------------------------------------

def test_row_paths_source_drive_is_sandbox_fy90_91_under_lane_src():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _same_win_path(_get(cfg, "paths.source_drive"), EXPECTED_SOURCE)


def test_row_paths_lan_destination_is_loopback_admin_share_fy90_91():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _same_win_path(_get(cfg, "paths.lan_destination"), EXPECTED_LAN_DESTINATION)


def test_row_paths_runtime_dir_is_lane_runtime():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _same_win_path(_get(cfg, "paths.runtime_dir"), EXPECTED_RUNTIME)


def test_row_paths_database_path_removed_so_it_derives_under_runtime():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert not _has(cfg, "paths.database_path")


def test_row_paths_log_directory_removed_so_no_service_logs_are_purged():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert not _has(cfg, "paths.log_directory")


def test_row_run_logs_dir_removed_so_it_derives_under_runtime():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert not _has(cfg, "run_logs.dir")


def test_row_paths_gcs_key_path_is_dummy_key_in_lane_runtime():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _same_win_path(_get(cfg, "paths.gcs_key_path"), EXPECTED_GCS_KEY)


def test_row_cloud_bucket_is_sentinel_name_that_cannot_exist_on_gcs():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "cloud.bucket") == EXPECTED_BUCKET
    assert _get(cfg, "cloud.bucket") != PROD_BUCKET


def test_row_notifications_smtp_host_is_loopback_and_port_is_lane_fake_server():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "notifications.smtp_host") == "127.0.0.1"
    assert _get(cfg, "notifications.smtp_port") == SMTP_PORT


def test_row_notifications_security_auth_and_credentials_are_empty():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "notifications.smtp_security") == "none"
    assert _get(cfg, "notifications.smtp_auth") is False
    assert _get(cfg, "notifications.smtp_username") == ""
    assert _get(cfg, "notifications.smtp_password") == ""


def test_row_notifications_sender_and_two_recipient_lists_are_fixed_invalid_addresses():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "notifications.sender") == "livebox@livebox.invalid"
    assert _get(cfg, "notifications.recipients") == ["ops@livebox.invalid"]
    assert _get(cfg, "notifications.report_recipients") == ["partners@livebox.invalid"]


def test_row_notifications_retry_attempts_and_base_at_minimum_bounds():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "notifications.retry_attempts") == 1
    assert _get(cfg, "notifications.retry_base_seconds") == 0


def test_row_wol_enabled_with_dummy_mac_and_loopback_server_ip():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "wol.enabled") is True
    assert _get(cfg, "wol.mac_address") == "02:00:00:00:00:00"
    assert _get(cfg, "wol.server_ip") == "127.0.0.1"


def test_row_lan_shutdown_after_backup_kept_as_production_value():
    # Table 1.2 says "as production (true): decision tested, action is a spy".
    # The caller's brief said "False"; that conflicts with the plan and is reported to the caller.
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "lan.shutdown_after_backup") is True


def test_row_deprecated_lan_and_maintenance_keys_are_dropped():
    from tools.livebox.derive_config import derive_config

    prod = copy.deepcopy(PROD_RAW)
    prod["lan"].update({
        "mt_threads": 16,
        "log_tail_kb": 128,
        "dry_run_timeout_seconds": 900,
        "use_backup_mode": True,
        "privilege_check_timeout_seconds": 30,
    })
    prod.setdefault("maintenance", {})["orphan_robocopy_log_max_age_hours"] = 48
    cfg = derive_config(prod, LANE_DIR, smtp_port=SMTP_PORT, has_backup_privilege=True)
    for k in ("mt_threads", "log_tail_kb", "dry_run_timeout_seconds",
              "use_backup_mode", "privilege_check_timeout_seconds"):
        assert k not in cfg["lan"]
    assert "orphan_robocopy_log_max_age_hours" not in cfg["maintenance"]


def test_row_rclone_lan_and_guard_keys_are_emitted():
    cfg = _derive(smtp_port=SMTP_PORT)
    for k in ("transfers", "checkers", "modify_window_seconds", "verify_timeout_seconds",
              "error_tail_lines", "arrival_hash_transferred", "arrival_hash_max_gb",
              "arrival_hash_timeout_seconds"):
        assert k in cfg["lan"], f"missing lan.{k}"
    assert "min_delete_floor_files" in cfg["guards"]


def test_row_guards_forbid_local_destination_is_false_for_this_machine_target():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "guards.forbid_local_destination") is False


def test_row_health_ntp_loopback_and_w32tm_timeout_five_seconds():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "health.ntp_server") == "127.0.0.1"
    assert _get(cfg, "health.w32tm_timeout_seconds") == 5


def test_row_retry_waits_at_model_minimum_bounds():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "lan.retry_wait_seconds") == 1      # LanConfig ge=1
    assert _get(cfg, "cloud.retry_delay_seconds") == 60  # CloudConfig ge=60


def test_row_retention_keeps_production_values_in_lane_a():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "run_logs.retention_days") == 90
    assert _get(cfg, "maintenance.db_retention_days") == 90
    assert _get(cfg, "maintenance.log_retention_days") == 90


def test_row_retention_lane_a730_uses_730_days():
    cfg = _derive(smtp_port=SMTP_PORT, retention_days=730)
    assert _get(cfg, "run_logs.retention_days") == 730
    assert _get(cfg, "maintenance.db_retention_days") == 730
    # the product model caps maintenance.log_retention_days at 365: 730 must not reach it (box lt1 failure)
    assert _get(cfg, "maintenance.log_retention_days") == 365


def test_row_rollover_overlap_enabled_and_activate_on_as_production():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert _get(cfg, "rollover.overlap_enabled") is True
    assert _get(cfg, "rollover.overlap_activate_on") == "12-31"


# ---------------------------------------------------------------------------
# Safety rows and cross-cutting rules of section 1.2 / 1.8
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "override_name, prod_value",
    [
        ("log_directory", PROD_LOG_DIR),
        ("database_path", PROD_DB),
        ("run_logs_dir", PROD_RUN_LOGS_DIR),
    ],
)
def test_refuses_when_production_log_database_or_run_logs_path_is_copied_into_output(
    override_name, prod_value
):
    with pytest.raises(REFUSAL):
        _derive(smtp_port=SMTP_PORT, **{override_name: prod_value})


def test_output_contains_no_production_smtp_host_user_or_password():
    cfg = _derive(smtp_port=SMTP_PORT)
    text = _all_values_text(cfg)
    assert PROD_SMTP_HOST not in text
    assert PROD_SMTP_USER not in text
    assert PROD_SMTP_PASSWORD not in text
    assert PROD_SENDER not in text
    assert PROD_RECIPIENT not in text
    assert PROD_REPORT_RECIPIENT not in text


def test_output_contains_no_production_path_bucket_nas_or_ntp_value():
    cfg = _derive(smtp_port=SMTP_PORT)
    text = _all_values_text(cfg)
    for forbidden in (
        PROD_SOURCE,
        PROD_NAS_SHARE,
        PROD_RUNTIME,
        PROD_DB,
        PROD_LOG_DIR,
        PROD_RUN_LOGS_DIR,
        PROD_GCS_KEY,
        PROD_BUCKET,
        PROD_NTP,
        "PRODNAS01",
        "secrets",
    ):
        assert forbidden not in text, f"production value leaked into output: {forbidden!r}"


def test_recipients_and_sender_are_loopback_only_invalid_domains():
    cfg = _derive(smtp_port=SMTP_PORT)
    addresses = [
        _get(cfg, "notifications.sender"),
        *_get(cfg, "notifications.recipients"),
        *_get(cfg, "notifications.report_recipients"),
    ]
    for address in addresses:
        assert address.rsplit("@", 1)[1].endswith(".invalid"), address
    assert _get(cfg, "notifications.smtp_host") == "127.0.0.1"


def test_fy_leaves_are_test_years_only():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert PureWindowsPath(_get(cfg, "paths.source_drive")).name in TEST_FY_LEAVES
    assert PureWindowsPath(str(_get(cfg, "paths.lan_destination"))).name in TEST_FY_LEAVES
    found = set(re.findall(r"FY\d\d-\d\d", _all_values_text(cfg)))
    assert found <= TEST_FY_LEAVES, found
    assert "FY26-27" not in _all_values_text(cfg)


def test_source_and_destination_never_equal_or_sit_under_production_paths():
    cfg = _derive(smtp_port=SMTP_PORT)
    production_roots = [
        PureWindowsPath(r"C:\AAMBackup"),
        PureWindowsPath(r"C:\BackupAgent"),
        PureWindowsPath("E:\\"),
        PureWindowsPath(PROD_NAS_SHARE),
    ]
    for key in ("paths.source_drive", "paths.lan_destination"):
        candidate = PureWindowsPath(str(_get(cfg, key)))
        assert candidate != PureWindowsPath(PROD_SOURCE), key
        assert candidate != PureWindowsPath(PROD_NAS_SHARE), key
        for root in production_roots:
            assert not candidate.is_relative_to(root), f"{key} sits under production root {root}"


def test_local_sandbox_paths_are_under_the_lane_folder():
    cfg = _derive(smtp_port=SMTP_PORT)
    lane = PureWindowsPath(str(LANE_DIR))
    for key in ("paths.source_drive", "paths.runtime_dir", "paths.gcs_key_path"):
        assert PureWindowsPath(str(_get(cfg, key))).is_relative_to(lane), key


def test_unlisted_production_keys_are_kept_unchanged():
    cfg = _derive(smtp_port=SMTP_PORT)
    assert cfg["audit"] == {"checkers": 4}
    assert _get(cfg, "cloud.enabled") is True
    assert _get(cfg, "lan.enabled") is True


def test_production_input_dict_is_not_mutated():
    from tools.livebox.derive_config import derive_config  # lazy: red with ImportError now

    snapshot = copy.deepcopy(PROD_RAW)
    derive_config(PROD_RAW, LANE_DIR, smtp_port=SMTP_PORT)
    assert PROD_RAW == snapshot
