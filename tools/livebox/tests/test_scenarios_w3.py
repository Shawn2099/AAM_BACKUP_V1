"""Tests for scenario pack wave 3 (plan 2026-11-24 section 4, X34-X39). Written before w3.py exists.

ASSUMED CONTRACT: tools.livebox.scenarios.w3.SCENARIOS is a list[Scenario] (see scenarios/base.py) holding exactly
X34..X39, wave 3. The behavior tests assume `expect(results)` reads these attributes from the results object
(missing attributes must be treated as "nothing observed" and never raise):

    results.archive_calls      list of archive-step calls seen during the lane (X35: must stay empty)
    results.robocopy_runs      list of dicts {"start": iso, "end": iso, "argv": str} (X37/X38: never overlapping)
    results.lan_runs_0600      int, LAN runs started inside a 06:00 rollover check (X36: must be 0 with
                               lan.scheduled false)
    results.switch_before_manual_lan  bool, the switch happened before a manual LAN press (X36: must be False)
    results.orphan_robocopy    list of robocopy argv left running after the kill (X39: must be empty)
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tools.livebox.scenarios.base import Scenario

W3_IDS = ["X34", "X35", "X36", "X37", "X38", "X39"]
WINDOW_START = dt.date(2090, 12, 1)
WINDOW_DAYS = 728
WINDOW_END = WINDOW_START + dt.timedelta(days=WINDOW_DAYS)
ALLOWED_RED = {"F-OWN-1", "F-OWN-2", "F-OWN-3", "F-OWN-5"}


def _scenarios():
    from tools.livebox.scenarios import w3
    return w3.SCENARIOS


def _by_id(sid):
    return {s.id: s for s in _scenarios()}[sid]


def _clean():
    return SimpleNamespace(
        archive_calls=[], robocopy_runs=[], lan_runs_0600=0,
        switch_before_manual_lan=False, orphan_robocopy=[],
    )


def test_exposes_scenario_list():
    sc = _scenarios()
    assert isinstance(sc, list) and sc
    assert all(isinstance(s, Scenario) for s in sc)


def test_ids_match_plan_wave_3():
    assert [s.id for s in _scenarios()] == W3_IDS


def test_lane_names_unique_and_nonempty():
    lanes = [s.lane for s in _scenarios()]
    assert all(isinstance(x, str) and x.strip() for x in lanes)
    assert len(set(lanes)) == len(lanes)


def test_wave_is_3_and_days_positive():
    for s in _scenarios():
        assert s.wave == 3, s.id
        assert isinstance(s.days, int) and s.days > 0, s.id


def test_start_is_valid_iso_inside_window():
    for s in _scenarios():
        d = dt.date.fromisoformat(s.start)
        assert WINDOW_START <= d, s.id
        assert d + dt.timedelta(days=s.days) <= WINDOW_END, s.id


def test_expected_red_only_known_findings():
    for s in _scenarios():
        assert s.expected_red is None or s.expected_red in ALLOWED_RED, s.id


def test_x36_marks_owner_finding_f_own_1():
    assert _by_id("X36").expected_red == "F-OWN-1"


def test_callables_present():
    for s in _scenarios():
        assert callable(s.setup) and callable(s.expect), s.id
        assert s.on_day is None or callable(s.on_day), s.id


@pytest.mark.parametrize("sid", W3_IDS)
def test_expect_on_clean_results_returns_list(sid):
    out = _by_id(sid).expect(_clean())
    assert isinstance(out, list)
    assert all(isinstance(x, str) for x in out)


def test_x35_flags_archive_call_in_lan_only_lane():
    res = _clean()
    res.archive_calls = [("archive", "FY90-91")]
    assert _by_id("X35").expect(res)


def test_x37_x38_flag_overlapping_robocopy():
    res = _clean()
    res.robocopy_runs = [
        {"start": "2091-01-10T21:01:00", "end": "2091-01-10T21:30:00", "argv": "robocopy a b"},
        {"start": "2091-01-10T21:10:00", "end": "2091-01-10T21:40:00", "argv": "robocopy a b"},
    ]
    assert _by_id("X37").expect(res)
    assert _by_id("X38").expect(res)


def test_x36_flags_lan_run_at_0600_and_early_switch():
    res = _clean()
    res.lan_runs_0600 = 1
    assert _by_id("X36").expect(res)
    res = _clean()
    res.switch_before_manual_lan = True
    assert _by_id("X36").expect(res)


def test_x39_flags_orphan_robocopy():
    res = _clean()
    res.orphan_robocopy = ["robocopy src dst /MIR"]
    assert _by_id("X39").expect(res)
