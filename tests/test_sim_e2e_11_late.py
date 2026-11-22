"""Card S11 (docs/PLAN_E2E_SIMULATION_2026-11-23.md): a LAN run that is one night late, at both boundaries.

(J) the Dec 31 2026 21:00 LAN starts Jan 1 2027 21:05 (lateness 86700 s = 24 h 05 min). The regular Jan 1
    21:00 LAN then starts at 21:06 (lateness 360 s).
(A) seed to Mar 28, the Mar 31 21:00 LAN starts Apr 1 21:05 (lateness 90300 s), after the Apr 1 06:00 switch.
    The regular Apr 1 21:00 LAN runs first (the events must stay in time order).

Rig: test_support.sim_calendar.Sim (real flows, DB, notify, SMTP server; scripted run_tool). A late start is
emulated by setting sim.lateness_s, which is what flow._scheduled_lateness_seconds reports to the flow.
Oracles: the hand values below, raw SQL on run_history, the rig's call log (argv per run), the SMTP subjects.
"""
from __future__ import annotations

import pendulum
import pytest

from core.time_utils import IST
from test_support.sim_calendar import A, B, CAL_A, CAL_J, Ev, Sim, sim_world

OK = {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES", "LAN_COMPLETE", "LAN_NO_CHANGES"}


def at(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


# hand values (card S11)
J_LATE_SCHED = at(2026, 12, 31, 21, 0)
J_LATE_START = at(2027, 1, 1, 21, 5)
J_LATE_LATENESS_S = 86700.0            # 24 h 05 min
J_REG_SCHED = at(2027, 1, 1, 21, 0)
J_REG_START = at(2027, 1, 1, 21, 6)
J_REG_LATENESS_S = 360.0               # 6 min
A_LATE_SCHED = at(2027, 3, 31, 21, 0)
A_LATE_START = at(2027, 4, 1, 21, 5)
A_LATE_LATENESS_S = 90300.0            # 25 h 05 min
A_APR1_REG = at(2027, 4, 1, 21, 0)


@pytest.fixture
def sim(tmp_path, monkeypatch):
    with sim_world(tmp_path, monkeypatch) as s:
        yield s


def _mentions(argv, fy: str) -> bool:
    return any(fy in str(a) for a in argv)


def _play_j(sim: Sim) -> None:
    """CAL_J with the Dec 31 21:00 LAN replaced by the late start, and the Jan 1 21:00 LAN by its 21:06 start."""
    events = [e for e in CAL_J if not (e.kind == "lan" and e.sched == J_LATE_SCHED)]
    sim.play([e for e in events if e.at < J_REG_SCHED])
    sim.lateness_s = J_LATE_LATENESS_S
    sim.play([Ev("lan", J_LATE_START, J_LATE_SCHED)])
    sim.lateness_s = J_REG_LATENESS_S
    sim.play([Ev("lan", J_REG_START, J_REG_SCHED)])
    sim.lateness_s = 0.0
    sim.play([e for e in events if e.at > J_REG_SCHED])


def _play_a(sim: Sim) -> None:
    """Seeded Mar 28 world, CAL_A with the Mar 31 21:00 LAN removed and that run started at Apr 1 21:05."""
    sim.seed_to_mar28()
    events = [e for e in CAL_A if not (e.kind == "lan" and e.sched == A_LATE_SCHED)]
    sim.play([e for e in events if e.at <= A_APR1_REG])
    sim.lateness_s = A_LATE_LATENESS_S
    sim.play([Ev("lan", A_LATE_START, A_LATE_SCHED)])
    sim.lateness_s = 0.0
    sim.play([e for e in events if e.at > A_APR1_REG])


def _lan_status_rows(sim: Sim, fy: str) -> list[tuple]:
    return sim.sql("SELECT status, started_at FROM run_history WHERE mode = 'lan' AND fy = ? ORDER BY id", fy)


# ── (J) Dec 31 late, across the Jan 1 activation ────────────────────────────────────────────────

def test_J_late_dec31_lan_is_a_missed_row_for_fy_a_and_the_run_is_refused(sim):
    _play_j(sim)
    # the late run ends non-success: it raises, so Prefect never shows it as completed
    assert [(ev.kind, ev.sched) for ev, _ in sim.errors] == [("lan", J_LATE_SCHED)], sim.errors
    statuses = [s for s, _ in _lan_status_rows(sim, A)]
    assert statuses[0] in OK                      # the Dec 30 night, untouched
    assert statuses[1] == "LAN_MISSED"            # the Dec 31 night, one night late
    assert statuses.count("LAN_MISSED") == 1


def test_J_late_dec31_run_makes_no_robocopy_call_for_fy_a(sim):
    _play_j(sim)
    hits = [c.argv for c in sim.calls
            if c.leg == "lan" and c.run_date == "2026-12-31" and _mentions(c.argv, A)]
    assert hits == []


def test_J_the_missed_night_is_announced_once(sim):
    _play_j(sim)
    missed = [s for s in sim.subjects() if "missed" in s.lower()]
    assert len(missed) == 1, missed


def test_J_jan1_regular_run_at_2106_backs_up_both_folders(sim):
    _play_j(sim)
    assert sorted({c.fy for c in sim.calls if c.leg == "lan" and c.when == J_REG_START}) == sorted([A, B])
    rows = sim.sql("SELECT fy, status FROM run_history WHERE mode = 'lan' AND started_at LIKE ? ORDER BY id",
                   "2027-01-01T21:06%")
    assert sorted(f for f, _ in rows) == sorted([A, B]), rows
    assert all(s in OK for _, s in rows), rows


# ── (A) Mar 31 late, after the Apr 1 06:00 switch ───────────────────────────────────────────────

def test_A_the_switch_happens_before_the_late_run(sim):
    _play_a(sim)
    assert sim.switch_writes == [(B, A)], sim.switch_writes


def test_A_late_mar31_run_writes_no_row_for_the_closed_year(sim):
    _play_a(sim)
    rows = sim.sql("SELECT status, started_at FROM run_history WHERE fy = ? AND started_at >= ?",
                   A, "2027-04-01T21:00")
    assert rows == [], rows


def test_A_late_mar31_run_makes_no_robocopy_call_for_fy_a(sim):
    _play_a(sim)
    hits = [c.argv for c in sim.calls
            if c.leg == "lan" and c.run_date == "2027-03-31" and _mentions(c.argv, A)]
    assert hits == []


def test_A_apr1_2100_regular_run_backs_up_fy_b_ok(sim):
    _play_a(sim)
    rows = sim.sql("SELECT fy, status FROM run_history WHERE mode = 'lan' AND started_at LIKE ? ORDER BY id",
                   "2027-04-01T21:00%")
    assert [f for f, _ in rows] == [B], rows
    assert rows[0][1] in OK, rows
