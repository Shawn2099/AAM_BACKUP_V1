"""opus-decision L3: an unreachable share is AAM-NAS-01 ("NAS not reachable"), not AAM-SYS-06.

Live-stack finding (LS11): the failure mail for an unreachable share said "Unexpected program error
(AAM-SYS-06)" and sent the owner to the vendor instead of to the NAS. A raw OSError with a Windows
"network unreachable" error number (53, 67, 64, 59, 121, 1222, 1231, 1232) or a timeout / connection error
that reaches the terminal decision of the LAN leg without a code attached at the raise site is classified
once, centrally, in `faults.classify_run`."""
from __future__ import annotations

import json

import pytest

import flow
from core import faults
from test_a2_interrupted_run_status import _lan_pipeline, _make_config


def _oserror(winerror, cls=OSError):
    exc = cls(f"[WinError {winerror}] network error")
    exc.winerror = winerror
    return exc


@pytest.mark.parametrize("winerror", [53, 67, 64, 59, 121, 1222, 1231, 1232])
def test_a_raw_unreachable_oserror_on_the_lan_leg_is_nas_01(winerror):
    assert faults.classify_run("lan", "LAN_FAILED", exc=_oserror(winerror)) == "AAM-NAS-01"


def test_a_filenotfound_with_winerror_53_is_nas_01_too():
    """Windows raises FileNotFoundError for an unreachable host (core/smb_ready.py)."""
    assert faults.classify_run("lan", "LAN_FAILED", exc=_oserror(53, FileNotFoundError)) == "AAM-NAS-01"


def test_the_cause_chain_is_searched():
    try:
        try:
            raise _oserror(1231)
        except OSError as e:
            raise RuntimeError("walk failed") from e
    except RuntimeError as outer:
        assert faults.classify_run("lan", "LAN_FAILED", exc=outer) == "AAM-NAS-01"


@pytest.mark.parametrize("exc", [TimeoutError("t"), ConnectionError("c"), ConnectionResetError("r")])
def test_timeouts_and_connection_errors_on_the_lan_leg_are_nas_01(exc):
    assert faults.classify_run("lan", "LAN_FAILED", exc=exc) == "AAM-NAS-01"


def test_a_missing_folder_or_rights_problem_is_not_called_unreachable():
    assert faults.classify_run("lan", "LAN_FAILED", exc=_oserror(3, FileNotFoundError)) == "AAM-SYS-06"
    assert faults.classify_run("lan", "LAN_FAILED", exc=PermissionError(13, "denied")) == "AAM-SYS-06"


def test_a_code_attached_at_the_raise_site_still_wins():
    exc = faults.coded(_oserror(53), "AAM-NAS-03")
    assert faults.classify_run("lan", "LAN_FAILED", exc=exc) == "AAM-NAS-03"


def test_the_cloud_leg_is_not_relabelled():
    assert faults.classify_run("cloud", "CLOUD_FAILED", exc=_oserror(53)) == "AAM-SYS-06"


def test_the_lan_pipeline_records_nas_01_for_an_unreachable_share():
    """End to end through the terminal decision of the LAN pipeline: the recorded fault code."""
    with _lan_pipeline("pre", _oserror(53, FileNotFoundError)) as (record_run, _recovery):
        with pytest.raises(OSError):
            flow._run_lan_pipeline(_make_config(), "run-l3", "2026-11-23T21:00:00")
    assert json.loads(record_run.call_args.kwargs["extended_metrics"])["fault_code"] == "AAM-NAS-01"
