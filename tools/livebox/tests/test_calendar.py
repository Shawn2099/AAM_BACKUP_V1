"""Calendar assertions for live-box lane A (plan section 2, L5b).

Assumed API of the module under test, `tools.livebox.calendar` (not written yet; L5a).
The plan names no functions, so this is the contract this file assumes:

    build_calendar(start: date, days: int) -> list[Day]

    Day:
        date: datetime.date
        events: list                      (not asserted here)
        expected_rows: list[Row]          one entry per new run_history row of that day
        expected_mails: list[Mail]        one entry per expected mail of that day

    Row:  leg: str   ("cloud" | "lan")
          fy: str    ("FY90-91" style label of the FY the pass backs up)
          role: str  ("configured" | "next")

    Mail: kind: str  ("audit" | "weekly" | "monthly" | "activated" | "announce" | ...)
          fy: str | None

The import is lazy (inside the fixture), so until the module exists every test
errors with ImportError, which is the intended red state.

Rules encoded here (plan section 2, dc50265 semantics):
- Cloud 02:01 runs before the 06:00 rollover check, so on the activation day the
  cloud has only the configured pass, and on the switch day the cloud still has
  two passes with the old configured FY (the switch takes effect at 06:00).
- LAN 21:01 runs after the 06:00 check, so the LAN configured pass is the FY of
  the calendar day, and the LAN next pass exists on activation day.
"""

from datetime import date, timedelta

import pytest

START = date(2090, 12, 1)  # Fri, day 0 of lane A
DAYS = 728  # exactly 104 weeks: every weekday occurs 104 times
LAST = date(2092, 11, 27)  # Thu

ACTIVATIONS = {
    date(2090, 12, 31): "FY91-92",  # Sun; activation at 06:00
    date(2091, 12, 31): "FY92-93",  # Mon; activation 2
}
SWITCHES = {date(2091, 4, 1), date(2092, 4, 1)}  # Sun, Tue

# LAN next-FY pass: activation day through the day before the switch.
LAN_NEXT_WINDOWS = [
    (date(2090, 12, 31), date(2091, 3, 31)),  # 91 days
    (date(2091, 12, 31), date(2092, 3, 31)),  # 92 days
]
# Cloud next-FY pass: starts the day after activation (02:01 runs before 06:00),
# runs up to and including the switch day (still two passes at 02:01).
CLOUD_NEXT_WINDOWS = [
    (date(2091, 1, 1), date(2091, 4, 1)),  # 91 days
    (date(2092, 1, 1), date(2092, 4, 1)),  # 92 days
]


def _days_in(windows):
    out = set()
    for first, last in windows:
        d = first
        while d <= last:
            out.add(d)
            d += timedelta(days=1)
    return out


def _fy_of(d):
    start_year = d.year if d.month >= 4 else d.year - 1
    return f"FY{start_year % 100:02d}-{(start_year + 1) % 100:02d}"


def _expected_cloud_configured_fy(d):
    # Switch takes effect at 06:00, after the 02:01 cloud pass.
    if d in SWITCHES:
        return _fy_of(d - timedelta(days=1))
    return _fy_of(d)


def _next_fy(fy):
    start_year = int(fy[2:4]) + 2000 + 1
    return f"FY{start_year % 100:02d}-{(start_year + 1) % 100:02d}"


@pytest.fixture(scope="module")
def lane_a():
    from tools.livebox.calendar import build_calendar

    return build_calendar(START, DAYS)


def _dates_with_mail(days, kind):
    return {d.date for d in days if any(m.kind == kind for m in d.expected_mails)}


def _dates_with_next_row(days, leg):
    return {
        d.date
        for d in days
        if any(r.leg == leg and r.role == "next" for r in d.expected_rows)
    }


def test_lane_a_spans_728_consecutive_days_without_gap_or_duplicate(lane_a):
    dates = [d.date for d in lane_a]
    assert len(dates) == DAYS
    assert len(set(dates)) == DAYS
    assert all(b - a == timedelta(days=1) for a, b in zip(dates, dates[1:]))


def test_lane_a_first_and_last_day(lane_a):
    assert lane_a[0].date == date(2090, 12, 1)
    assert lane_a[0].date.weekday() == 4  # Friday
    assert lane_a[-1].date == LAST
    assert lane_a[-1].date.weekday() == 3  # Thursday


def test_104_sundays_run_the_audit_and_send_no_mail(lane_a):
    # Box code (flow.py integrity_audit_flow): a clean audit sends NO mail; only a failed one alerts.
    sundays = {d.date for d in lane_a if d.date.weekday() == 6}
    assert len(sundays) == 104
    assert {d.date for d in lane_a if any(e.kind == "audit" for e in d.events)} == sundays
    assert _dates_with_mail(lane_a, "audit") == set()


def test_104_mondays_get_the_weekly_report(lane_a):
    mondays = {d.date for d in lane_a if d.date.weekday() == 0}
    assert len(mondays) == 104
    assert _dates_with_mail(lane_a, "weekly") == mondays


def test_24_firsts_of_month_get_the_monthly_report(lane_a):
    firsts = {d.date for d in lane_a if d.date.day == 1}
    assert len(firsts) == 24
    assert _dates_with_mail(lane_a, "monthly") == firsts


def test_183_overlap_days_have_a_next_fy_lan_pass(lane_a):
    expected = _days_in(LAN_NEXT_WINDOWS)
    assert len(expected) == 183
    assert _dates_with_next_row(lane_a, "lan") == expected
    assert min(expected) == date(2090, 12, 31)
    assert max(expected) == date(2092, 3, 31)


def test_overlap_days_have_a_next_fy_cloud_pass_except_activation_day(lane_a):
    expected = _days_in(CLOUD_NEXT_WINDOWS)
    assert len(expected) == 183
    assert _dates_with_next_row(lane_a, "cloud") == expected
    assert date(2090, 12, 31) not in expected
    assert date(2091, 4, 1) in expected  # switch day: two cloud passes


def test_activations_on_2090_12_31_and_2091_12_31(lane_a):
    activated = {}
    for d in lane_a:
        for m in d.expected_mails:
            if m.kind == "activated":
                activated[d.date] = m.fy
    assert activated == ACTIVATIONS


def test_switches_on_2091_04_01_and_2092_04_01(lane_a):
    announced = _dates_with_mail(lane_a, "announce")
    assert announced == SWITCHES

    by_date = {d.date: d for d in lane_a}
    for switch in SWITCHES:
        day = by_date[switch]
        normal = [r for r in day.expected_rows if "rollover-final" not in r.run_suffix]
        finals = [r for r in day.expected_rows if "rollover-final" in r.run_suffix]
        cloud = [r for r in normal if r.leg == "cloud"]
        lan = [r for r in normal if r.leg == "lan"]
        # the rollover's two final backups of the closing FY (06:00) are extra rows (round 4, F3)
        assert sorted(r.run_suffix for r in finals) == ["-rollover-final-cloud", "-rollover-final-lan"]
        # 02:01 cloud still runs both passes; 21:01 LAN runs only the new FY.
        assert len(cloud) == 2
        assert len(lan) == 1
        assert lan[0].role == "configured"
        assert lan[0].fy == _fy_of(switch)


def test_leap_day_2092_02_29_is_present_and_ordinary(lane_a):
    by_date = {d.date: d for d in lane_a}
    leap = by_date[date(2092, 2, 29)]
    assert leap.date.weekday() == 4  # Friday
    assert any(r.leg == "cloud" and r.role == "configured" for r in leap.expected_rows)
    assert any(r.leg == "lan" and r.role == "configured" for r in leap.expected_rows)
    assert any(r.leg == "cloud" and r.role == "next" for r in leap.expected_rows)
    assert any(r.leg == "lan" and r.role == "next" for r in leap.expected_rows)


def test_every_day_has_one_configured_cloud_and_one_configured_lan_run(lane_a):
    for day in lane_a:
        rows = [r for r in day.expected_rows if "rollover-final" not in r.run_suffix]   # finals: round 4, F3
        assert rows, f"no rows expected on {day.date}"
        cloud = [r for r in rows if r.leg == "cloud" and r.role == "configured"]
        lan = [r for r in rows if r.leg == "lan" and r.role == "configured"]
        assert len(cloud) == 1, day.date
        assert len(lan) == 1, day.date
        assert cloud[0].fy == _expected_cloud_configured_fy(day.date), day.date
        assert lan[0].fy == _fy_of(day.date), day.date


def test_next_fy_rows_always_back_the_fy_after_the_configured_one(lane_a):
    for day in lane_a:
        configured = {
            r.leg: r.fy for r in day.expected_rows if r.role == "configured"
        }
        for r in day.expected_rows:
            if r.role == "next":
                assert r.fy == _next_fy(configured[r.leg]), day.date
