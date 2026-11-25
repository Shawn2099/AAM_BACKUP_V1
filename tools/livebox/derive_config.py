"""Derive the sandbox (livebox) config from the production config dict.

Plan LIVEBOX_TEST_PLAN_2026-11-24.md section 1.2 (table) and 1.8 (safety).
Pure function: no file reads, no writes, never mutates its input.
"""

from __future__ import annotations

import copy
import pathlib
from pathlib import PureWindowsPath

TEST_FY = "FY90-91"
_GUARDED_OVERRIDES = ("log_directory", "database_path", "run_logs_dir")


def _under(path: str, root: PureWindowsPath) -> bool:
    try:
        return PureWindowsPath(path).is_relative_to(root)
    except (TypeError, ValueError):
        return False


def derive_config(
    prod_raw: dict,
    lane_dir: pathlib.Path,
    *,
    smtp_port: int | None = None,
    has_backup_privilege: bool | None = None,
    retention_days: int | None = None,
    log_directory: str | None = None,
    database_path: str | None = None,
    run_logs_dir: str | None = None,
    db_retention_days: int | None = None,
    run_logs_retention_days: int | None = None,
    log_retention_days: int | None = None,
    test_fy: str = TEST_FY,
) -> dict:
    """Return the lane config (a new dict). Raises ValueError on any safety refusal."""
    lane = PureWindowsPath(str(lane_dir))
    stamp = lane.parent.name
    lane_name = lane.name
    if not stamp or not lane_name or not lane.drive:
        raise ValueError(f"lane_dir must look like <drive>\\AAMLivebox\\<stamp>\\<lane>, got {lane_dir!s}")
    if smtp_port is None:
        raise ValueError("smtp_port (the lane's fake SMTP server port) is required")

    # Refuse production-style paths for keys that must be removed (a copied log_directory
    # would let a far-future clock purge production service logs).
    for name, value in (
        ("log_directory", log_directory),
        ("database_path", database_path),
        ("run_logs_dir", run_logs_dir),
    ):
        if value is not None and not _under(str(value), lane):
            raise ValueError(f"{name}={value!r} is not under the lane folder {lane}; refusing")

    cfg = copy.deepcopy(prod_raw)

    runtime = lane / "runtime"
    drive_share = lane.drive.rstrip(":") + "$"
    nas_local = lane / "nas" / "backup" / test_fy
    nas_unc = "\\\\127.0.0.1\\" + drive_share + "\\" + "\\".join(nas_local.parts[1:])

    paths = cfg.setdefault("paths", {})
    paths["source_drive"] = str(lane / "src" / test_fy)
    paths["lan_destination"] = nas_unc
    paths["runtime_dir"] = str(runtime)
    paths["gcs_key_path"] = str(runtime / "dummy-key.json")
    paths.pop("database_path", None)
    paths.pop("log_directory", None)

    cfg.setdefault("run_logs", {}).pop("dir", None)

    cfg.setdefault("cloud", {})["bucket"] = f"aam-livebox-local-{stamp}"
    cfg["cloud"]["retry_delay_seconds"] = 60

    n = cfg.setdefault("notifications", {})
    n.update(
        smtp_host="127.0.0.1",
        smtp_port=int(smtp_port),
        smtp_security="none",
        smtp_auth=False,
        smtp_username="",
        smtp_password="",
        sender="livebox@livebox.invalid",
        recipients=["ops@livebox.invalid"],
        report_recipients=["partners@livebox.invalid"],
        retry_attempts=1,
        retry_base_seconds=0,
    )

    cfg["wol"] = {
        **cfg.get("wol", {}),
        "enabled": True,
        "mac_address": "02:00:00:00:00:00",
        "server_ip": "127.0.0.1",
    }

    lan = cfg.setdefault("lan", {})
    for deprecated in (
        "mt_threads",
        "log_tail_kb",
        "dry_run_timeout_seconds",
        "use_backup_mode",
        "privilege_check_timeout_seconds",
    ):
        lan.pop(deprecated, None)
    lan.setdefault("transfers", 4)
    lan.setdefault("checkers", 4)
    lan.setdefault("modify_window_seconds", 2)
    lan.setdefault("verify_timeout_seconds", 1800)
    lan.setdefault("error_tail_lines", 100)
    lan.setdefault("arrival_hash_transferred", True)
    lan.setdefault("arrival_hash_max_gb", 50)
    lan.setdefault("arrival_hash_timeout_seconds", 3600)
    lan["retry_wait_seconds"] = 1
    # shutdown_after_backup: as production (the LAN copier no longer has a backup-mode key).

    guards = cfg.setdefault("guards", {})
    guards["forbid_local_destination"] = False
    guards.setdefault("min_delete_floor_files", 20)

    cfg.setdefault("maintenance", {}).pop("orphan_robocopy_log_max_age_hours", None)

    health = cfg.setdefault("health", {})
    health["ntp_server"] = "127.0.0.1"
    health["w32tm_timeout_seconds"] = 5

    cfg.setdefault("rollover", {})["overlap_enabled"] = True

    # per-key keyword wins over retention_days; unnamed keys keep the production value
    run_logs_days = run_logs_retention_days if run_logs_retention_days is not None else retention_days
    db_days = db_retention_days if db_retention_days is not None else retention_days
    log_days = log_retention_days if log_retention_days is not None else retention_days
    if log_retention_days is None and log_days is not None:
        log_days = min(log_days, 365)   # product model caps maintenance.log_retention_days at 365 (le=365)
    if run_logs_days is not None:
        cfg.setdefault("run_logs", {})["retention_days"] = run_logs_days
    if db_days is not None:
        cfg.setdefault("maintenance", {})["db_retention_days"] = db_days
    if log_days is not None:
        cfg.setdefault("maintenance", {})["log_retention_days"] = log_days

    return cfg
