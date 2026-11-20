"""OV-5.2 - the first run of a leg in an FY (no guard baseline) with planned deletes > 0 is a DECISION (G3 block /
approval), so a destination that is already filled - `gs://bucket/FY27-28` or a NAS folder somebody put files in -
is never trimmed unasked (plan 5.5)."""
import inspect

import flow
from core import guards as g


def g3(planned, dest, first_run, **kw):
    return g.g3_mass_delete(planned, dest, 500, 10, first_run=first_run, **kw)


def test_first_run_with_planned_deletes_blocks_even_within_the_limits():
    r = g3(1, 1000, True)                        # 1 of 1000 would pass every normal limit
    assert not r.passed and r.fault_code == "AAM-DEL-01"
    assert "first backup" in r.reason.lower()
    assert r.metrics["first_run"] is True and r.metrics["planned_deletes"] == 1


def test_first_run_without_planned_deletes_passes():
    assert g3(0, 1000, True).passed
    assert g3(0, 0, True).passed                 # an empty destination: nothing to trim


def test_not_the_first_run_keeps_the_normal_rule():
    assert g3(1, 1000, False).passed
    assert g3(100, 1000, False).passed
    assert not g3(101, 1000, False).passed


def test_first_run_is_off_by_default_for_direct_callers():
    assert g.g3_mass_delete(1, 1000, 500, 10).passed


def test_the_first_run_block_is_approvable_like_any_g3_block():
    """The same G3 name and metrics, so the approval card / evaluate_request work unchanged."""
    r = g3(5, 1000, True)
    assert r.guard == "G3_MASS_DELETE"
    from core import approvals as ap
    request = {"approval_id": "a", "leg": "cloud", "fy": "FY27-28", "approved_at": "2027-01-05T03:00:00+05:30",
               "expires_at": "2027-01-06T03:00:00+05:30", "used_at": None, "dismissed_at": None,
               "guards": [{"guard": "G3_MASS_DELETE", "metrics": r.metrics}]}
    assert ap.evaluate_request(request, leg="cloud", blocked=[r], fy="FY27-28",
                               now_iso="2027-01-05T04:00:00+05:30", tolerance_percent=0).allowed


def test_flow_tells_g3_when_there_is_no_baseline_for_the_leg_and_fy():
    src = inspect.getsource(flow)
    assert src.count("first_run=not baseline") == 2            # cloud and LAN guard measurements


# ── the real guard tasks, no baseline ─────────────────────────────────────────

def test_cloud_first_run_into_a_filled_prefix_is_a_decision(tmp_path):
    from tests.test_m2_guard_wiring import _cloud_cfg, _run_cloud_guards
    exc, _ = _run_cloud_guards(_cloud_cfg(tmp_path), 400, 1000, 3, baseline=None)
    assert isinstance(exc, g.GuardBlocked) and "first backup of this folder" in str(exc)
    # nothing to delete: the first run goes ahead
    ok, _ = _run_cloud_guards(_cloud_cfg(tmp_path), 400, 1000, 0, baseline=None)
    assert isinstance(ok, dict)
    # a baseline of a successful earlier run: the ordinary rule (3 of 1000 is fine)
    ok, _ = _run_cloud_guards(_cloud_cfg(tmp_path), 400, 1000, 3,
                              baseline={"fy": "FY26-27", "source_file_count": 400})
    assert isinstance(ok, dict)


def test_lan_first_run_into_a_filled_nas_folder_is_a_decision(tmp_path):
    import pytest

    from tests.test_m2_guard_wiring import _counts, _lan_cfg, _run_lan_guards
    with pytest.raises(g.GuardBlocked) as ei:
        _run_lan_guards(_lan_cfg(tmp_path), _counts(990, 3), 1000)          # 3 extras, no baseline
    assert "first backup of this folder" in str(ei.value)
    _run_lan_guards(_lan_cfg(tmp_path), _counts(990, 3), 1000,
                    baseline={"fy": "FY26-27", "source_file_count": 993})    # not a first run: passes
