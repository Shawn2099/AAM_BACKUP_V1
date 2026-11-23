"""Manual-LAN step A: lan.scheduled / lan.on_demand_warn_after_days (owner 2026-11-23)."""
import pytest
from pydantic import ValidationError

from core import config_edit
from models.config import LanConfig


def test_defaults_keep_todays_behaviour():
    lan = LanConfig()
    assert lan.scheduled is True and lan.on_demand_warn_after_days == 30
    assert lan.nightly is True and lan.manual_only is False


def test_manual_only_and_off_flags():
    assert LanConfig(scheduled=False).manual_only is True
    assert LanConfig(scheduled=False).nightly is False
    off = LanConfig(enabled=False, scheduled=False)
    assert off.manual_only is False and off.nightly is False


@pytest.mark.parametrize("days,ok", [(0, False), (1, True), (30, True), (365, True), (366, False)])
def test_warn_days_bounds(days, ok):
    if ok:
        assert LanConfig(on_demand_warn_after_days=days).on_demand_warn_after_days == days
    else:
        with pytest.raises(ValidationError):
            LanConfig(on_demand_warn_after_days=days)


def test_keys_are_labelled_risky_and_start_only():
    assert "lan.scheduled" in config_edit._RISKY_KEYS
    assert "lan.scheduled" in config_edit._START_ONLY_KEYS
