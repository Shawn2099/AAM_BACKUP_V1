"""Card S12 (docs/PLAN_E2E_SIMULATION_2026-11-23.md section 1b): WORST, three failing LAN nights across Jan 1.

Setup: the robocopy of every LAN run on Dec 31, Jan 1 and Jan 2 (all FYs) exits 16 (fatal, nothing
copied). Jan 3 is a normal night. Cloud is OK every night. Calendar: CAL_J.

Hand table (IST, derived from the event list and the plan):
  lan rows (run date, FY, OK?):
    Dec 30  A  OK     (the last LAN success for FY26-27: 2026-12-30 21:00)
    Dec 31  A  fail
    Jan 1   B  fail   Jan 1   A  fail
    Jan 2   B  fail   Jan 2   A  fail
    Jan 3   B  OK     Jan 3   A  OK
  Every failed flow raises (never COMPLETED): the errors are exactly the three failing LAN events.
  Shutdowns: Dec 30 (complete mirror) = 1; the three failing nights never power the NAS off (they vote
  no); the Jan 3 08:00 audit wakes the NAS and powers it off after auditing (shutdown_after_backup
  default true) = 1; the Jan 3 21:00 LAN (complete, both folders vote yes) = 1. Total 3. (The card
  listed 2 and left out the audit shutdown; the derivation from flow.py is recorded in the report.)
  FAILURE mails per (LAN, FY): at least 1, at most 1 + ceil(48 / repeat_cooldown_hours), where 48 h is
  Dec 31 21:00 .. Jan 2 21:00 and the cooldown is read from the sim config.
  Stale check (watchdog._check_stale_legs):
    Jan 2 09:00  lan is overdue (last OK Dec 30 21:00), cloud is not (OK Jan 2 02:00)
    Jan 3 09:00  lan is still overdue (the Jan 3 21:00 run has not happened yet), cloud is not
  Exactly one RECOVERED mail per FY for LAN, on Jan 3.
"""

from __future__ import annotations

import math

import pendulum
import pytest

import watchdog
from core.attention import compute_attention
from core.manifest import ManifestDB
from core.time_utils import IST
from test_support.fake_tools import robocopy_log
from test_support.sim_calendar import CAL_J, A, B, lan, sim_world

OK = {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES", "LAN_COMPLETE", "LAN_NO_CHANGES"}   # copied by value (plan 0)
WINDOW_HOURS = 48                                                                # Dec 31 21:00 .. Jan 2 21:00
EXPECTED_SHUTDOWNS = 3                                                           # see the module docstring


@pytest.fixture
def sim(tmp_path, monkeypatch):
    with sim_world(tmp_path, monkeypatch) as s:
        yield s


def _at(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


def _stale(sim, at):
    """(legs from watchdog._check_stale_legs, product-view `stale:<fy>:<leg>` codes) at `at`."""
    sim.go(at)
    legs = watchdog._check_stale_legs(sim.cfg)
    db = ManifestDB.from_config(sim.cfg)
    try:
        codes = [i["code"] for i in compute_attention(db, sim.cfg) if i["code"].startswith("stale:")]  # product-view
    finally:
        db.close()
    return legs, codes


def test_s12_three_failing_lan_nights_across_jan1(sim):
    dec31, jan1, jan2 = pendulum.date(2026, 12, 31), pendulum.date(2027, 1, 1), pendulum.date(2027, 1, 2)
    failing = robocopy_log(total=1, copied=0, failed=1)
    for day in (dec31, jan1, jan2):
        for fy in (A, B):
            sim.set_answer("lan", fy, day, exit_code=16, log=failing, log_encoding="utf-16")

    check1, check2 = _at(2027, 1, 2, 9), _at(2027, 1, 3, 9)
    sim.play([e for e in CAL_J if e.at < check1])
    legs1, codes1 = _stale(sim, check1)
    sim.play([e for e in CAL_J if check1 <= e.at < check2])
    legs2, codes2 = _stale(sim, check2)
    sim.play([e for e in CAL_J if e.at >= check2])

    # hand: lan rows by (run date, FY, OK?)
    lan_rows = sorted((r["started_at"][:10], r["fy"], r["status"] in OK)
                      for r in sim.runs() if r["mode"] == "lan")
    expected = sorted([
        ("2026-12-30", A, True),
        ("2026-12-31", A, False),
        ("2027-01-01", B, False), ("2027-01-01", A, False),
        ("2027-01-02", B, False), ("2027-01-02", A, False),
        ("2027-01-03", B, True), ("2027-01-03", A, True),
    ])
    assert lan_rows == expected

    # the cloud leg was fine every night
    cloud_rows = [r for r in sim.runs() if r["mode"] == "cloud"]
    assert cloud_rows and all(r["status"] in OK for r in cloud_rows), cloud_rows

    # every failed flow raised; the three failing nights are the only errors
    assert sorted(ev.at for ev, _ in sim.errors) == sorted(
        [lan(dec31).at, lan(jan1).at, lan(jan2).at]), sim.errors
    assert all(ev.kind == "lan" for ev, _ in sim.errors), sim.errors

    # the NAS: one power-off per successful night (Dec 30, Jan 3 audit, Jan 3 LAN), none on failing nights
    assert sim.shutdowns == EXPECTED_SHUTDOWNS, sim.shutdowns

    # FAILURE mails per (LAN, FY): bounded by the sim config's repeat cooldown
    cooldown = sim.cfg.alerts.repeat_cooldown_hours
    bound = 1 + math.ceil(WINDOW_HOURS / cooldown)
    subjects = sim.subjects()
    for fy in (A, B):
        n = sum(1 for s in subjects if f"[{fy}] PROBLEM: Office NAS backup" in s)
        assert 1 <= n <= bound, (fy, n, bound, subjects)

    # stale check: lan is overdue at both times, cloud never; the product view names lan A
    assert legs1 == ["lan"], legs1
    assert "stale:FY26-27:lan" in codes1, codes1                       # product-view
    assert legs2 == ["lan"], legs2
    assert "stale:FY26-27:lan" in codes2, codes2                       # product-view

    # exactly one RECOVERED mail per LAN FY, from the Jan 3 success
    for fy in (A, B):
        n = sum(1 for s in subjects if f"[{fy}] RESOLVED: Office NAS backup" in s)
        assert n == 1, (fy, subjects)
