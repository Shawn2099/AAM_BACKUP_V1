"""Plan A2 / A3 + design rule 10: the vocabulary the rollover journal reports with."""

import pytest

from core import faults, statuses
from models.config import RolloverConfig


def test_new_rollover_statuses_have_category_label_and_description():
    assert statuses.category("ROLLOVER_RUNNING") == statuses.CATEGORY_RUNNING
    assert statuses.category("ROLLOVER_FAILED") == statuses.CATEGORY_FAILED
    assert statuses.label("ROLLOVER_FAILED") == "FY rollover not finished"
    assert statuses.label("ROLLOVER_RUNNING") == "FY rollover running"
    assert "rollover" in statuses.describe("ROLLOVER_FAILED").lower()
    assert "rollover" in statuses.describe("ROLLOVER_RUNNING").lower()
    assert statuses.RunStatus.ROLLOVER_FAILED in statuses.FAILED_STATUSES


def test_a_rollover_row_never_counts_as_a_leg_success():
    for status in ("ROLLOVER_COMPLETE", "ROLLOVER_FAILED", "ROLLOVER_RUNNING"):
        assert status not in statuses.CLOUD_SUCCESS_STATUSES
        assert status not in statuses.LAN_SUCCESS_STATUSES
    assert not statuses.is_success("ROLLOVER_FAILED")


@pytest.mark.parametrize("code,severity", [("AAM-ROL-04", faults.ACTION),
                                           ("AAM-ROL-05", faults.WARNING)])
def test_rollover_fault_codes(code, severity):
    fault = faults.get(code)
    assert fault is not None and fault.code == code
    assert fault.severity == severity
    assert fault.steps                       # tells a non-technical person what to do
    assert "{" not in fault.title


def test_rol_04_names_the_exact_manual_step_text():
    text = faults.get("AAM-ROL-04").what
    assert "settings file" in text and "nothing was deleted" in text


def test_archive_alert_key_has_default_and_bounds():
    assert "archive_alert_after_days" in RolloverConfig.model_fields
    assert RolloverConfig().archive_alert_after_days == 3
    with pytest.raises(ValueError):
        RolloverConfig(archive_alert_after_days=0)
    with pytest.raises(ValueError):
        RolloverConfig(archive_alert_after_days=400)
