"""Red-first tests for the Opus review must-fix items C1, C2 and the tightened calendar counts (lane A).

C1: the announce mail key is the product's `rollover:done:<to_fy>` (flow.py, the rollover-done notify), not
    `rollover:announce:<fy>`.
C2: edge E1 - on an activation Sunday the 08:00 audit runs after the 06:00 activation but before any backup of the next
    FY, so the next-FY audit rows must allow AUDIT_INCOMPLETE / PENDING_BACKUP (or not exist) and must NOT allow VERIFIED.
Tightened counts (new tests only): exactly one mail per due day and the whole-lane totals.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

START = date(2090, 12, 1)
DAYS = 728


@pytest.fixture(scope="module")
def lane_a():
    from tools.livebox.calendar import build_calendar

    return build_calendar(START, DAYS)


def _mails(day, cls):
    return [m for m in day.mails if m.mail_class == cls]


# ---------------------------------------------------------------- C1
def test_c1_announce_key_is_the_products_rollover_done_key(lane_a):
    announces = [m for d in lane_a for m in _mails(d, "announce")]
    assert len(announces) == 2
    for m in announces:
        assert m.key == f"rollover:done:{m.fy}", m.key


def test_c1_no_announce_key_uses_the_old_name(lane_a):
    assert not [m for d in lane_a for m in d.mails if m.key and m.key.startswith("rollover:announce:")]


# ---------------------------------------------------------------- C2
def test_c2_activation_sunday_exists_in_lane_a(lane_a):
    days = [d for d in lane_a if d.is_activation_day and d.is_sunday]
    assert [d.date for d in days] == [date(2090, 12, 31)]


def test_c2_activation_sunday_next_fy_audit_rows_never_allow_verified(lane_a):
    day = next(d for d in lane_a if d.is_activation_day and d.is_sunday)
    nxt_rows = [r for r in day.rows if r.leg.startswith("audit_") and r.role == "next"]
    for r in nxt_rows:                                   # an empty list (no next-FY audit rows) is also acceptable
        assert "VERIFIED" not in r.statuses, f"{r.leg}: {r.statuses}"
        assert set(r.statuses) & {"AUDIT_INCOMPLETE", "PENDING_BACKUP"}, f"{r.leg}: {r.statuses}"


def test_c2_activation_sunday_configured_audit_rows_still_verified(lane_a):
    day = next(d for d in lane_a if d.is_activation_day and d.is_sunday)
    cfg_rows = [r for r in day.rows if r.leg.startswith("audit_") and r.role == "configured"]
    assert cfg_rows and all("VERIFIED" in r.statuses for r in cfg_rows)


# ---------------------------------------------------------------- tightened counts
def test_exactly_one_weekly_mail_on_each_monday_and_none_elsewhere(lane_a):
    for d in lane_a:
        assert d.mail_count("weekly") == (1 if d.date.weekday() == 0 else 0), d.date


def test_exactly_one_monthly_mail_on_each_first_and_none_elsewhere(lane_a):
    for d in lane_a:
        assert d.mail_count("monthly") == (1 if d.date.day == 1 else 0), d.date


def test_exactly_one_announce_mail_on_each_switch_day_and_none_elsewhere(lane_a):
    for d in lane_a:
        assert d.mail_count("announce") == (1 if d.is_switch_day else 0), d.date


def test_exactly_one_activated_mail_on_each_activation_day_and_none_elsewhere(lane_a):
    for d in lane_a:
        assert d.mail_count("activated") == (1 if d.is_activation_day else 0), d.date


def test_totals_of_lane_a():
    from tools.livebox.calendar import build_calendar, totals

    t = totals(build_calendar(START, DAYS))
    assert t["days"] == 728
    assert t["sundays"] == 104
    assert t["firsts"] == 24
    assert t["activations"] == 2
    assert t["switches"] == 2
    assert t["weekly"] == 104 and t["monthly"] == 24
    assert t["activated"] == 2 and t["announce"] == 2
    assert t["problem_mails"] == 0
