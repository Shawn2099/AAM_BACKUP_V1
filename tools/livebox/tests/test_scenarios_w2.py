r"""Tests for tools.livebox.scenarios.w2 (plan section 4, wave 2). Written before the module exists.

ASSUMED CONTRACT: module exposes SCENARIOS: list[Scenario] (see scenarios/base.py) with exactly the wave-2 ids
X20 X20b X21 X22 X23 X24 X25 X26 X27 X28 X29 X30 X31 X32 X33.

ASSUMED `results` shape handed to expect(): an object (SimpleNamespace in these tests) with attributes
    rows      list[dict]  run_history rows: day (ISO str), leg ("cloud"|"lan"), fy, status, fault (str|None)
    mails     list[dict]  sent mails: day, severity, subject, fault (str|None), key (str|None)
    approvals list[dict]  approval requests: fy, leg, status, http (int|None)
    notes     dict        free-form scenario observations (counts, booleans); missing keys must not raise
expect() must return list[str]; clean/empty results may or may not yield failures, but must never raise.
"""
from __future__ import annotations

import datetime as dt
import importlib
from types import SimpleNamespace

import pytest

WAVE2_IDS = ["X20", "X20b", "X21", "X22", "X23", "X24", "X25", "X26", "X27", "X28", "X29", "X30", "X31", "X32", "X33"]
ALLOWED_RED = {"F-OWN-1", "F-OWN-2", "F-OWN-3", "F-OWN-5"}
WINDOW_START = dt.date(2090, 12, 1)
WINDOW_DAYS = 728


def w2():
    return importlib.import_module("tools.livebox.scenarios.w2")


def by_id(sid):
    return next(s for s in w2().SCENARIOS if s.id == sid)


def clean():
    return SimpleNamespace(rows=[], mails=[], approvals=[], notes={})


def test_ids_exactly_wave2():
    assert sorted(s.id for s in w2().SCENARIOS) == sorted(WAVE2_IDS)
    assert len(w2().SCENARIOS) == len(WAVE2_IDS)


def test_lane_names_unique():
    lanes = [s.lane for s in w2().SCENARIOS]
    assert all(lanes) and len(set(lanes)) == len(lanes)


def test_wave_and_days():
    for s in w2().SCENARIOS:
        assert s.wave == 2, s.id
        assert s.days > 0, s.id
        assert s.title, s.id


def test_start_inside_window():
    end = WINDOW_START + dt.timedelta(days=WINDOW_DAYS)
    for s in w2().SCENARIOS:
        d = dt.date.fromisoformat(s.start)
        assert WINDOW_START <= d, s.id
        assert d + dt.timedelta(days=s.days) <= end, s.id


def test_expected_red_only_known_findings():
    for s in w2().SCENARIOS:
        assert s.expected_red is None or s.expected_red in ALLOWED_RED, s.id


def test_callables_present():
    for s in w2().SCENARIOS:
        assert callable(s.setup) and callable(s.expect), s.id
        assert s.on_day is None or callable(s.on_day), s.id


@pytest.mark.parametrize("sid", WAVE2_IDS)
def test_expect_on_clean_results_returns_list(sid):
    out = by_id(sid).expect(clean())
    assert isinstance(out, list)
    assert all(isinstance(x, str) for x in out)


# ---- behaviour: expect() flags a deliberate violation --------------------------------------------------------------

def test_x21_flags_c_pass_run_by_n_approval():
    """G5 (X21): an approval of the N pass must never run C; a C row blocked, or approval fy mismatch, is a failure."""
    s = by_id("X21")
    bad = clean()
    bad.rows = [
        {"day": "2091-02-10", "leg": "lan", "fy": "FY90-91", "status": "LAN_BLOCKED", "fault": "AAM-NAS-10"},
        {"day": "2091-02-10", "leg": "lan", "fy": "FY91-92", "status": "LAN_BLOCKED", "fault": "AAM-NAS-10"},
    ]
    assert s.expect(bad), "C pass blocked by the N marker must be flagged"


def test_x24_flags_missing_g1_block():
    """G1 (X24): wiped C source must give AAM-SRC-03 BLOCKED; a COMPLETE row for C after the wipe is a failure."""
    s = by_id("X24")
    bad = clean()
    bad.rows = [{"day": "2091-02-20", "leg": "cloud", "fy": "FY90-91", "status": "CLOUD_COMPLETE", "fault": None},
                {"day": "2091-02-20", "leg": "lan", "fy": "FY90-91", "status": "LAN_COMPLETE", "fault": None}]
    assert s.expect(bad)


def test_x29_flags_stale_approval_not_refused():
    """X29: approval of a closed FY after the switch must be refused (409); 200 is a failure."""
    s = by_id("X29")
    bad = clean()
    bad.approvals = [{"fy": "FY90-91", "leg": "lan", "status": "APPROVED", "http": 200}]
    assert s.expect(bad)


def test_x32_flags_activation_when_overlap_disabled():
    """X32: overlap_enabled false -> never activated; an N-FY row (FY91-92) is a failure."""
    s = by_id("X32")
    bad = clean()
    bad.rows = [{"day": "2090-12-31", "leg": "lan", "fy": "FY91-92", "status": "LAN_COMPLETE", "fault": None}]
    assert s.expect(bad)
