"""F7 + F13: schedule_problems includes rollover_cron; _RISKY_KEYS includes new safety/rclone keys."""
from __future__ import annotations

from core import budget, config_edit
from models.config import load_config


def test_f7_schedule_problems_catches_cloud_colliding_with_rollover_cron():
    cfg = load_config()
    data = cfg.model_dump()
    data["schedule"]["lan_cron"] = "0 20 * * *"
    data["schedule"]["cloud_cron"] = "1 2 * * *"
    data["budget"]["cloud_max_hours"] = 6
    data["schedule"]["rollover_cron"] = "0 6 * * *"
    updated = type(cfg).model_validate(data)
    warnings = budget.rollover_schedule_warnings(updated)
    assert any("rollover" in w for w in warnings)


def test_f7_schedule_problems_catches_rollover_colliding_with_audit_cron():
    cfg = load_config()
    data = cfg.model_dump()
    data["schedule"]["rollover_cron"] = "30 9 * * *"
    data["schedule"]["audit_cron"] = "0 10 * * SUN"
    updated = type(cfg).model_validate(data)
    warnings = budget.rollover_schedule_warnings(updated)
    assert any("rollover" in w for w in warnings)


def test_f13_risky_keys_include_new_safety_and_rclone_keys():
    expected = {
        "lan.shutdown_only_if_woken",
        "cloud.verify_recheck_enabled",
        "cloud.track_renames",
        "lan.track_renames",
    }
    missing = expected - config_edit._RISKY_KEYS
    assert not missing, f"Missing from _RISKY_KEYS: {missing}"
