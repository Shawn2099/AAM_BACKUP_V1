"""A1 - overlap config keys (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, section 7, task A1).

`rollover.overlap_enabled` (default true, a config without the key is enabled) and
`rollover.overlap_activate_on` (MM-DD, default 12-31, a real day in a non-leap year, not 04-01).
"""

import pytest
from pydantic import ValidationError

from core import config_edit, config_help
from models.config import (
    AppConfig, CloudConfig, DashboardConfig, LanConfig, PathsConfig, RolloverConfig, WolConfig,
)


def test_overlap_defaults_are_on_and_31_december():
    cfg = RolloverConfig()
    assert cfg.overlap_enabled is True
    assert cfg.overlap_activate_on == "12-31"


def test_config_without_the_overlap_keys_is_enabled():
    cfg = RolloverConfig(**{"source_placeholder_name": "README_FY.txt"})
    assert cfg.overlap_enabled is True
    assert cfg.overlap_activate_on == "12-31"


def test_overlap_can_be_switched_off():
    assert RolloverConfig(overlap_enabled=False).overlap_enabled is False


@pytest.mark.parametrize("day", ["01-15", "12-01", "12-31", "02-28", "03-31"])
def test_valid_activation_days_are_accepted(day):
    assert RolloverConfig(overlap_activate_on=day).overlap_activate_on == day


@pytest.mark.parametrize("day", ["02-29", "04-01", "04-02", "10-01", "13-01", "12-32", "1231", "00-10", "12/31", "ab-cd"])
def test_invalid_activation_days_are_refused_in_plain_words(day):
    with pytest.raises(ValidationError, match="MM-DD"):
        RolloverConfig(overlap_activate_on=day)


def test_help_text_exists_for_both_keys():
    assert config_help.HELP["rollover.overlap_enabled"][0]
    assert config_help.HELP["rollover.overlap_activate_on"][0]


def test_both_keys_are_risky_settings():
    assert config_edit.is_risky("rollover.overlap_enabled")
    assert config_edit.is_risky("rollover.overlap_activate_on")


def test_owner_layout_loads():
    """Source E:\\FY26-27 with NAS \\\\nas\\backup\\FY26-27 is the reference layout (plan header)."""
    cfg = AppConfig(
        paths=PathsConfig(source_drive=r"E:\FY26-27", lan_destination=r"\\nas\backup\FY26-27"),
        lan=LanConfig(enabled=True),
        cloud=CloudConfig(enabled=False),
        wol=WolConfig(enabled=False),
        dashboard=DashboardConfig(),
    )
    assert cfg.rollover.overlap_enabled is True
    assert cfg.rollover.overlap_activate_on == "12-31"
