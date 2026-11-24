r"""Tests for tools.livebox.scenarios.w1 (plan section 4, wave 1, rows X01-X19). Written before the module exists.

ASSUMED CONTRACT
    tools.livebox.scenarios.w1.SCENARIOS : list[Scenario]  (Scenario from tools.livebox.scenarios.base)
    wave-1 ids: X01 .. X19 (19 scenarios, each its own lane).

ASSUMED RESULTS SHAPE passed to expect(results) (attribute access; missing attributes must be tolerated
by the pack with getattr defaults, but these tests always supply all of them):
    rows                list[dict]  run_history rows: day (ISO), leg, fy, status, fault
    mails               list[dict]  sent mails: day (ISO), severity (INFO/WARNING/ACTION_REQUIRED/FAILURE), code, key
    config_fy_by_day    dict[str, str]  configured FY at the end of each ISO day
    activated_on        list[str]   ISO days on which the next FY was activated
    switched_on         list[str]   ISO days on which the configured FY switched
    clock_proven_by_day dict[str, bool]
    created_paths       list[str]   paths created under the lane source / NAS by rollover
    junction_target_entries list[str]  entries found in the junction target dir (must stay empty)
    approvals           list[dict]  approval attempts: phrase, http_status, applied (bool)
"""
from __future__ import annotations

import datetime as dt
import importlib
import re
from types import SimpleNamespace

import pytest

from tools.livebox import calendar as cal

WAVE1_IDS = [f"X{n:02d}" for n in range(1, 20)]
KNOWN_RED = {"F-OWN-1", "F-OWN-2", "F-OWN-3", "F-OWN-5", "F-P1-ESCALATION-SUPPRESSED"}


def w1():
    return importlib.import_module("tools.livebox.scenarios.w1")


def by_id():
    return {s.id: s for s in w1().SCENARIOS}


def fake(**kw):
    base = dict(rows=[], mails=[], config_fy_by_day={}, activated_on=[], switched_on=[],
                clock_proven_by_day={}, created_paths=[], junction_target_entries=[], approvals=[])
    base.update(kw)
    return SimpleNamespace(**base)


def mail(day, severity, code, key=""):
    return {"day": day, "severity": severity, "code": code, "key": key}


# ---------------------------------------------------------------- structure
def test_ids_exactly_wave1_list():
    ids = [s.id for s in w1().SCENARIOS]
    assert sorted(ids) == WAVE1_IDS
    assert len(ids) == len(set(ids))


def test_lane_names_unique_and_nonempty():
    lanes = [s.lane for s in w1().SCENARIOS]
    assert all(isinstance(x, str) and x for x in lanes)
    assert len(lanes) == len(set(lanes))


def test_wave_is_one_and_days_positive():
    for s in w1().SCENARIOS:
        assert s.wave == 1, s.id
        assert isinstance(s.days, int) and s.days > 0, s.id
        assert s.title, s.id


def test_callables_present():
    for s in w1().SCENARIOS:
        assert callable(s.setup), s.id
        assert callable(s.expect), s.id
        assert s.on_day is None or callable(s.on_day), s.id


def test_start_valid_iso_inside_calendar_window():
    lo = cal.LANE_A_START
    hi = lo + dt.timedelta(days=cal.LANE_A_DAYS)
    for s in w1().SCENARIOS:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", s.start), s.id
        start = dt.date.fromisoformat(s.start)
        end = start + dt.timedelta(days=s.days)
        assert lo <= start, s.id
        assert end <= hi, f"{s.id} runs past the 728-day window"


def _covers(s, month, day):
    start = dt.date.fromisoformat(s.start)
    return any((start + dt.timedelta(days=i)).month == month and (start + dt.timedelta(days=i)).day == day
               for i in range(s.days))


@pytest.mark.parametrize("sid", ["X01", "X04", "X08", "X10", "X13"])
def test_activation_scenarios_cover_dec_31(sid):
    assert _covers(by_id()[sid], 12, 31)


@pytest.mark.parametrize("sid", ["X02", "X03", "X05", "X09", "X14", "X15", "X16", "X17", "X18"])
def test_switch_scenarios_cover_apr_1(sid):
    assert _covers(by_id()[sid], 4, 1)


def test_expected_red_only_known_findings():
    for s in w1().SCENARIOS:
        assert s.expected_red is None or s.expected_red in KNOWN_RED, s.id


def test_known_expected_reds():
    sc = by_id()
    assert sc["X09"].expected_red == "F-OWN-1"
    assert sc["X18"].expected_red == "F-OWN-2"


def test_expect_on_clean_results_returns_list():
    for s in w1().SCENARIOS:
        out = s.expect(fake())
        assert isinstance(out, list), s.id
        assert all(isinstance(x, str) for x in out), s.id


# ---------------------------------------------------------------- behaviour (deliberate violations)
def test_x01_flags_duplicate_activation_warning():
    sc = by_id()["X01"]
    key = "rollover:activation:FY91-92"
    bad = fake(mails=[mail("2090-12-31", "WARNING", "AAM-ROL-07", key),
                      mail("2091-01-01", "WARNING", "AAM-ROL-07", key),
                      mail("2091-01-01", "ACTION_REQUIRED", "AAM-ROL-07", key),
                      mail("2091-01-02", "INFO", "", "rollover:activated:FY91-92")],
               activated_on=["2091-01-02"])
    out = sc.expect(bad)
    assert out, "two WARNING mails for one activation must be flagged"


def test_x02_flags_switch_while_clock_unproven():
    sc = by_id()["X02"]
    bad = fake(switched_on=["2091-04-01"],
               config_fy_by_day={"2091-04-01": "FY91-92"},
               clock_proven_by_day={"2091-04-01": False})
    out = sc.expect(bad)
    assert out
    assert any("switch" in x.lower() or "unproven" in x.lower() or "ROL-03" in x for x in out)


def test_x10_flags_write_through_junction():
    sc = by_id()["X10"]
    bad = fake(junction_target_entries=["FY91-92", "placeholder.txt"], activated_on=["2090-12-31"])
    out = sc.expect(bad)
    assert out
    assert any("junction" in x.lower() or "link" in x.lower() for x in out)


def test_x13_flags_activation_on_skewed_clock():
    sc = by_id()["X13"]
    bad = fake(activated_on=["2090-12-31"], created_paths=["src/FY91-92"],
               clock_proven_by_day={"2090-12-31": False})
    assert sc.expect(bad)


def test_x14_flags_wrong_phrase_accepted():
    sc = by_id()["X14"]
    bad = fake(approvals=[{"phrase": "approve ", "http_status": 200, "applied": True}])
    out = sc.expect(bad)
    assert out
    assert any("phrase" in x.lower() or "approve" in x.lower() for x in out)
