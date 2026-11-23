"""Two recipient lists (plan docs/PLAN_RECIPIENTS_AND_SCHEDULE_ALERTS_2026-10-22.md, step A1).

`notifications.recipients` = the ALERTS list (unchanged key, so existing configs load);
`notifications.report_recipients` = the REPORTS list. The same address may sit in both lists, never
twice in one (case-insensitive)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from core import config_edit, config_help
from models.config import NotificationConfig


def _n(**kw):
    return NotificationConfig(**kw)


def test_report_list_defaults_to_empty_and_alerts_list_is_unchanged():
    n = _n(recipients=["a@x.com"])
    assert n.report_recipients == []
    assert n.recipients == ["a@x.com"]


def test_same_address_may_be_in_both_lists():
    n = _n(recipients=["a@x.com", "b@x.com"], report_recipients=["a@x.com", "c@x.com"])
    assert n.recipients == ["a@x.com", "b@x.com"]
    assert n.report_recipients == ["a@x.com", "c@x.com"]


@pytest.mark.parametrize("field", ["recipients", "report_recipients"])
def test_an_address_twice_in_one_list_is_rejected_ignoring_case(field):
    with pytest.raises(ValidationError) as e:
        _n(**{field: ["a@x.com", "A@X.com"]})
    assert "duplicate" in str(e.value).lower()


@pytest.mark.parametrize("field", ["recipients", "report_recipients"])
def test_a_malformed_address_is_rejected_in_either_list(field):
    with pytest.raises(ValidationError) as e:
        _n(**{field: ["not-an-address"]})
    assert "not-an-address" in str(e.value)


def test_the_error_names_the_list():
    with pytest.raises(ValidationError) as e:
        _n(report_recipients=["bad"])
    assert "report" in str(e.value).lower()


def test_addresses_are_trimmed():
    assert _n(report_recipients=["  r@x.com "]).report_recipients == ["r@x.com"]


def test_the_new_key_is_a_basic_email_list_setting_with_help():
    key = "notifications.report_recipients"
    assert key in config_edit.BASIC_KEYS
    assert config_edit._WIDGETS[key] == "email_list"
    assert key in config_edit._EMAIL_DELIVERY
    name, _help = config_help.HELP[key]
    assert name and "report" in name.lower()
    # the alerts key is renamed so the two boxes cannot be confused
    assert "alert" in config_help.HELP["notifications.recipients"][0].lower()
