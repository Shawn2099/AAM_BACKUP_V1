"""A6 - the per-leg `scheduled` switch (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, sections 5 and 7, task A6).

`enabled: false` still means off everywhere. `enabled: true, scheduled: false` = manual only: the leg keeps its
cron in the config but no schedule runs it. Part 1 (this file) covers the config model and validation; the
registration and behaviour of a manual-only leg are tasks C1-C2 (more tests in this file later).
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from core import config_edit, config_help
from core import statuses
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, ScheduleConfig, WolConfig

REPO = Path(__file__).resolve().parents[1]


def _config(cloud=True, lan=True, cloud_scheduled=True, lan_scheduled=True, schedule=None):
    return AppConfig(
        paths=PathsConfig(source_drive=r"E:\FY26-27", lan_destination=r"\\nas\backup\FY26-27",
                          gcs_key_path=r"C:\keys\aam-key.json"),
        lan=LanConfig(enabled=lan, scheduled=lan_scheduled),
        cloud=CloudConfig(enabled=cloud, scheduled=cloud_scheduled, bucket="aam-test-bucket",
                          project_number="123456789012"),
        wol=WolConfig(enabled=False),
        schedule=schedule or ScheduleConfig(),
    )


# --- defaults and the keys ----------------------------------------------------------------------------

def test_scheduled_defaults_to_true_for_both_legs():
    assert LanConfig().scheduled is True
    assert CloudConfig(bucket="aam-test-bucket", project_number="123456789012").scheduled is True


def test_a_config_without_the_keys_is_scheduled():
    raw = {"paths": {"source_drive": r"E:\FY26-27", "lan_destination": r"\\nas\backup\FY26-27",
                     "gcs_key_path": r"C:\keys\aam-key.json"},
           "cloud": {"bucket": "aam-test-bucket", "project_number": "123456789012"},
           "lan": {}, "wol": {"enabled": False}}
    cfg = AppConfig.model_validate(raw)
    assert cfg.cloud.scheduled is True and cfg.lan.scheduled is True


def test_scheduled_must_be_a_boolean():
    with pytest.raises(ValidationError):
        LanConfig(scheduled="sometimes")


# --- at least one enabled leg must be scheduled -------------------------------------------------------

def test_both_legs_manual_only_is_refused():
    with pytest.raises(ValidationError) as err:
        _config(cloud_scheduled=False, lan_scheduled=False)
    assert "scheduled" in str(err.value)


def test_the_only_enabled_leg_manual_only_is_refused():
    with pytest.raises(ValidationError):
        _config(cloud=True, lan=False, cloud_scheduled=False)


def test_one_scheduled_leg_is_enough_next_to_a_manual_one():
    cfg = _config(cloud=True, lan=True, cloud_scheduled=True, lan_scheduled=False)
    assert cfg.lan.scheduled is False and cfg.cloud.scheduled is True


def test_a_disabled_scheduled_leg_does_not_count():
    with pytest.raises(ValidationError):
        _config(cloud=False, lan=True, lan_scheduled=False)


# --- the budget validator skips an unscheduled leg ----------------------------------------------------

def test_schedule_that_only_fits_without_the_manual_leg_is_accepted():
    # both legs at the same start time collide when both are scheduled
    collide = ScheduleConfig(cloud_cron="0 2 * * *", lan_cron="0 2 * * *")
    with pytest.raises(ValidationError):
        _config(schedule=collide)
    cfg = _config(lan_scheduled=False, schedule=collide)
    assert cfg.lan.scheduled is False


# --- help, risky keys, shipped configs ----------------------------------------------------------------

@pytest.mark.parametrize("key", ["cloud.scheduled", "lan.scheduled"])
def test_help_and_risky_lists_know_the_switch(key):
    assert key in config_help.HELP
    assert config_help.HELP[key][0]
    assert key in config_edit._RISKY_KEYS


def test_example_config_writes_both_switches_explicitly():
    raw = yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8"))
    assert raw["cloud"]["scheduled"] is True
    assert raw["lan"]["scheduled"] is True


def test_install_config_writes_both_switches_explicitly():
    raw = yaml.safe_load((REPO / "deploy" / "config.install.yaml").read_text(encoding="utf-8"))
    assert raw["cloud"]["scheduled"] is True
    assert raw["lan"]["scheduled"] is True


# --- the final-copy status set (defined with the A4 lookup; re-checked here as the A6 item) -----------

def test_final_copy_statuses_exclude_legacy_and_partial_values():
    assert statuses.FINAL_COPY_STATUSES == {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE", "LAN_COMPLETE"}
    assert not (statuses.FINAL_COPY_STATUSES & {"SUCCESS", "LAN_NO_CHANGES_COMPLETE",
                                                 "CLOUD_PARTIAL", "LAN_PARTIAL", "CLOUD_VERIFY_FAILED"})
