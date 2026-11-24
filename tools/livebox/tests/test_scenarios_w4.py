"""Tests for tools.livebox.scenarios.w4 (plan section 4, wave 4: X40-X48). Written before the module exists.

ASSUMED CONTRACT: w4.SCENARIOS is a list[Scenario] with ids X40..X48 in that order.
ASSUMED results shape passed to expect(results): an object with list attributes
    .rows   [{"day": "YYYY-MM-DD", "leg": "lan"|"cloud", "status": str, "fault": str|None}]
    .mails  [{"day": "YYYY-MM-DD", "kind": str, "fault": str|None, "subject": str}]
    .notes  [str]
A clean (empty) result must make expect() return a list (possibly with "missing evidence" strings).
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tools.livebox.scenarios.base import Scenario
from tools.livebox.scenarios import w4

PLAN_IDS = [f"X{n}" for n in range(40, 49)]
ALLOWED_RED = {"F-OWN-1", "F-OWN-2", "F-OWN-3", "F-OWN-5"}
WIN_START = dt.date(2090, 12, 1)
WIN_END = WIN_START + dt.timedelta(days=728)


def _clean():
    return SimpleNamespace(rows=[], mails=[], notes=[])


def _by_id(i):
    return next(s for s in w4.SCENARIOS if s.id == i)


def test_ids_match_plan():
    assert [s.id for s in w4.SCENARIOS] == PLAN_IDS


def test_are_scenarios_wave4():
    for s in w4.SCENARIOS:
        assert isinstance(s, Scenario)
        assert s.wave == 4
        assert s.title


def test_lane_names_unique_and_nonempty():
    lanes = [s.lane for s in w4.SCENARIOS]
    assert all(lanes) and len(set(lanes)) == len(lanes)


def test_days_positive():
    assert all(isinstance(s.days, int) and s.days > 0 for s in w4.SCENARIOS)


def test_start_valid_iso_inside_window():
    for s in w4.SCENARIOS:
        d = dt.date.fromisoformat(s.start)
        assert WIN_START <= d <= WIN_END, s.id


def test_window_end_not_exceeded():
    for s in w4.SCENARIOS:
        end = dt.date.fromisoformat(s.start) + dt.timedelta(days=s.days)
        assert end <= WIN_END + dt.timedelta(days=1), s.id


def test_expected_red_only_known_findings():
    for s in w4.SCENARIOS:
        assert s.expected_red is None or s.expected_red in ALLOWED_RED, s.id


def test_callables():
    for s in w4.SCENARIOS:
        assert callable(s.setup) and callable(s.expect)
        assert s.on_day is None or callable(s.on_day)


@pytest.mark.parametrize("sid", PLAN_IDS)
def test_expect_on_clean_results_returns_list(sid):
    out = _by_id(sid).expect(_clean())
    assert isinstance(out, list)
    assert all(isinstance(x, str) for x in out)


def test_x40_locked_file_never_reports_complete():
    s = _by_id("X40")
    bad = _clean()
    bad.rows = [{"day": s.start, "leg": "lan", "status": "LAN_COMPLETE", "fault": None}]
    assert s.expect(bad), "LAN_COMPLETE while a file is locked must be flagged"


def test_x40_honest_partial_not_flagged_for_status():
    s = _by_id("X40")
    ok = _clean()
    ok.rows = [{"day": s.start, "leg": "lan", "status": "LAN_PARTIAL", "fault": "AAM-LAN-02"}]
    ok.mails = [{"day": s.start, "kind": "problem", "fault": "AAM-LAN-02", "subject": "x"}]
    assert not any("COMPLETE" in f for f in s.expect(ok))


def test_x43_silent_success_after_db_corruption_flagged():
    s = _by_id("X43")
    bad = _clean()
    bad.rows = [{"day": s.start, "leg": "lan", "status": "LAN_COMPLETE", "fault": None}]
    assert s.expect(bad), "a success row without AAM-SYS-01 after DB corruption must be flagged"


def test_x44_notification_storm_flagged():
    s = _by_id("X44")
    bad = _clean()
    bad.mails = [
        {"day": s.start, "kind": "problem", "fault": "AAM-CLD-01", "subject": f"fail {i}"}
        for i in range(40)
    ]
    assert s.expect(bad), "40 problem mails in one lane must trip the storm rule"


def test_x45_reverse_switch_or_second_activation_flagged():
    s = _by_id("X45")
    bad = _clean()
    bad.mails = [
        {"day": s.start, "kind": "activated", "fault": None, "subject": "activated"},
        {"day": s.start, "kind": "activated", "fault": None, "subject": "activated again"},
    ]
    assert s.expect(bad), "second activation after clock-back must be flagged"
