"""OV-2.1 - the FY overlap config keys (plan D-1, 5.2, 5.7): defaults, bounds, Advanced + risky, labels."""
import pytest
from pydantic import ValidationError

from core import config_edit, config_help
from models.config import RolloverConfig

KEYS = ("rollover.overlap_enabled", "rollover.early_start_month",
        "rollover.activation_alert_after_hours", "rollover.sibling_ignore")


def test_defaults():
    r = RolloverConfig()
    assert r.overlap_enabled is True
    assert r.early_start_month == 1
    assert r.activation_alert_after_hours == 24
    assert r.sibling_ignore == []


@pytest.mark.parametrize("month,ok", [(0, False), (1, True), (2, True), (3, True), (4, False)])
def test_early_start_month_bounds(month, ok):
    if ok:
        assert RolloverConfig(early_start_month=month).early_start_month == month
    else:
        with pytest.raises(ValidationError):
            RolloverConfig(early_start_month=month)


@pytest.mark.parametrize("hours,ok", [(0, False), (1, True), (24, True), (168, True), (169, False)])
def test_activation_alert_hours_bounds(hours, ok):
    if ok:
        assert RolloverConfig(activation_alert_after_hours=hours).activation_alert_after_hours == hours
    else:
        with pytest.raises(ValidationError):
            RolloverConfig(activation_alert_after_hours=hours)


def test_sibling_ignore_takes_plain_folder_names_only():
    assert RolloverConfig(sibling_ignore=["FY 2020", "Old stuff"]).sibling_ignore == ["FY 2020", "Old stuff"]
    for bad in ([""], ["a\\b"], ["a/b"], ["x:y"]):
        with pytest.raises(ValidationError):
            RolloverConfig(sibling_ignore=bad)


def test_unknown_key_still_refused():
    with pytest.raises(ValidationError):
        RolloverConfig(overlap_enabeld=True)


@pytest.mark.parametrize("key", KEYS)
def test_advanced_and_labelled(key):
    assert key not in config_edit.BASIC_KEYS
    assert key in config_help.HELP
    label, _help = config_help.label_and_help(key, "technical")
    assert label and label != key


@pytest.mark.parametrize("key", KEYS)
def test_overlap_keys_are_risky(key):
    assert key in config_edit._RISKY_KEYS
