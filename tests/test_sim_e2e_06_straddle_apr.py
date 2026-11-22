"""Card S6 WORST-A: the 31 March / 1 April straddle, cloud overruns into the rollover
(docs/PLAN_E2E_SIMULATION_2026-11-23.md, S6).

Setup: seed Jan 5 .. Mar 28, then play CAL_A with three scripted events:
  - Mar 31 21:00 LAN for FY27-28 (its robocopy hook moves the clock to Apr 1 00:30), so FY26-27's LAN run of the
    same night starts after midnight;
  - Apr 1 02:00 cloud for FY26-27 (its hook moves the clock to Apr 1 06:30: the cloud overran the 06:00 rollover);
  - the Apr 1 rollover is played with slot_busy=True (the backup slot is never free for it), at 06:30.

Rig note (not a card change): play() refuses an event earlier than the clock, so the rollover that the overrun
reaches is played at its real start, 06:30, with scheduled time 06:00 (Ev(at=06:30, sched=06:00)).

Hand expectations come from the card and PLAN_FY_OVERLAP section 4 ("1 Apr onwards, switch pending": both folders
backed up nightly; the 06:00 rollover does the final backup, switch and archive). Oracles: raw SQL and the rig's
switch_writes / archive_calls. The two known defects are separate xfail tests at the bottom (D14, D7).
"""

from __future__ import annotations

import datetime as _dt

import pendulum
import pytest

from core.time_utils import IST
from test_support.sim_calendar import A, B, CAL_A, OK, Ev, sim_world

WINDOW_FROM = "2027-03-29T21:00"


@pytest.fixture
def sim(tmp_path, monkeypatch):
    with sim_world(tmp_path, monkeypatch) as s:
        yield s


def _at(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


def _idx(kind: str, month: int, day: int) -> int:
    return next(i for i, e in enumerate(CAL_A) if e.kind == kind and (e.at.month, e.at.day) == (month, day))


def _events() -> list[Ev]:
    """CAL_A with the 1 April rollover moved to its real start: the cloud overrun ends at 06:30."""
    events = list(CAL_A)
    i = _idx("rollover", 4, 1)
    events[i] = Ev("rollover", _at(2027, 4, 1, 6, 30), _at(2027, 4, 1, 6, 0))
    return events


def _hooks(sim) -> None:
    sim.set_answer("lan", B, _dt.date(2027, 3, 31),
                   hook=lambda: sim.jump(_at(2027, 4, 1, 0, 30)))            # FY27-28 LAN runs, clock -> 00:30
    sim.set_answer("cloud", A, _dt.date(2027, 4, 1),
                   hook=lambda: sim.jump(_at(2027, 4, 1, 6, 30)))            # FY26-27 cloud overruns to 06:30


def _other_errors(sim) -> list:
    """Flow failures of every event except the 1 April rollover, whose slot timeout is the scenario."""
    return [(ev.kind, ev.at, repr(e)) for ev, e in sim.errors if not (ev.kind == "rollover" and ev.sched.day == 1
                                                                       and ev.sched.month == 4)]


def _night(sim, mode: str, prefix: str) -> list[dict]:
    return [r for r in sim.runs() if r["mode"] == mode and r["started_at"].startswith(prefix)]


def _journal_steps(sim) -> list[tuple]:
    return sim.sql(
        "SELECT s.step, s.state FROM rollover_journal_steps s "
        "JOIN rollover_journal j ON j.id = s.journal_id WHERE j.from_fy = ? AND j.to_fy = ? ORDER BY s.step",
        A, B)


def _play_to_straddle(sim, stop_after_kind_day: tuple[str, int, int]) -> None:
    """Seed, install the hooks, play CAL_A up to and including the named event."""
    sim.seed_to_mar28()
    _hooks(sim)
    events = _events()
    kind, month, day = stop_after_kind_day
    stop = _idx(kind, month, day)
    sim.play(events[:stop + 1])


def test_s6_straddle_hand_table(sim):
    sim.seed_to_mar28()
    _hooks(sim)
    events = _events()
    i_lan_mar31 = _idx("lan", 3, 31)
    i_cloud_apr1 = _idx("cloud", 4, 1)
    i_roll_apr1 = _idx("rollover", 4, 1)
    i_cloud_apr2 = _idx("cloud", 4, 2)
    i_roll_apr2 = _idx("rollover", 4, 2)

    # 1. the 31 March night: FY27-28 starts at 21:00, FY26-27 starts after midnight
    sim.play(events[:i_lan_mar31 + 1])
    assert sim.errors == [], sim.errors
    assert sorted(r["fy"] for r in _night(sim, "lan", "2027-03-31T21:")) == [B]
    a_lan_after_midnight = [r for r in sim.runs()
                            if r["mode"] == "lan" and r["fy"] == A and r["started_at"].startswith("2027-04-01T00:")]
    assert len(a_lan_after_midnight) == 1, [(r["fy"], r["started_at"]) for r in sim.runs() if r["mode"] == "lan"]

    # 2. the 1 April 02:00 cloud: both FYs; FY26-27's overruns to 06:30
    sim.play(events[i_lan_mar31 + 1:i_roll_apr1])
    assert sim.errors == [], sim.errors
    assert sorted(r["fy"] for r in _night(sim, "cloud", "2027-04-01T02:")) == sorted([A, B])

    # 3. the 1 April rollover at 06:30 (scheduled 06:00) with no free backup slot: no switch, no archive
    sim.slot_busy = True
    sim.play([events[i_roll_apr1]])
    sim.slot_busy = False
    # how the rollover reports the timeout (a G6 block today, see D7) is not asserted here
    assert sim.switch_writes == [], sim.switch_writes
    assert sim.archive_calls == [], sim.archive_calls

    # 4. the nights after: both folders still backed up (switch pending)
    sim.play(events[i_roll_apr1 + 1:i_cloud_apr2 + 1])          # through the 2 April 02:00 cloud
    assert _other_errors(sim) == [], _other_errors(sim)
    assert sorted(r["fy"] for r in _night(sim, "lan", "2027-04-01T21:")) == sorted([A, B])
    assert sorted(r["fy"] for r in _night(sim, "cloud", "2027-04-02T02:")) == sorted([A, B])
    assert all(r["status"] in OK for r in _night(sim, "lan", "2027-04-01T21:") + _night(sim, "cloud", "2027-04-02T02:"))

    # 5. the 2 April 06:00 rollover: the switch and the archive, once each
    sim.play(events[i_cloud_apr2 + 1:i_roll_apr2 + 1])
    assert _other_errors(sim) == [], _other_errors(sim)
    assert sim.switch_writes == [(B, A)], sim.switch_writes
    assert len(sim.archive_calls) == 1 and sim.archive_calls[0]["old_fy"] == A, sim.archive_calls
    steps = dict(_journal_steps(sim))
    assert steps.get("config_switch") == "done", steps
    assert all(state in ("done", "skipped") for state in steps.values()), steps

    # 6. the rest of the calendar: FY27-28 only
    sim.play(events[i_roll_apr2 + 1:])
    assert _other_errors(sim) == [], _other_errors(sim)
    after = [r for r in sim.runs() if r["mode"] in ("lan", "cloud") and r["started_at"] > "2027-04-02T06:30"]
    assert sorted(r["mode"] for r in after) == ["cloud"] * 3 + ["lan"] * 3, [(r["mode"], r["started_at"]) for r in after]
    assert {r["fy"] for r in after} == {B}
    assert len(sim.switch_writes) == 1 and len(sim.archive_calls) == 1


@pytest.mark.xfail(strict=False, reason="D14: the second folder's run row is dated at its own pipeline start "
                                        "(2027-04-01 00:30), not at its scheduled night (2027-03-31)")
def test_d14_row_a_of_mar31_is_dated_mar31(sim):
    _play_to_straddle(sim, ("lan", 3, 31))
    rows = [r for r in sim.runs() if r["mode"] == "lan" and r["fy"] == A and r["started_at"] >= WINDOW_FROM]
    assert rows, "the FY26-27 LAN row of the 31 March night is missing"
    assert rows[-1]["started_at"][:10] == "2027-03-31", rows[-1]["started_at"]


@pytest.mark.xfail(strict=False, reason="D7: a final-backup slot timeout on 1 April asks for a G6 "
                                        "'roll over anyway' approval; the owner has not decided")
def test_d7_slot_timeout_requests_no_approval(sim):
    _play_to_straddle(sim, ("cloud", 4, 1))
    events = _events()
    sim.slot_busy = True
    sim.play([events[_idx("rollover", 4, 1)]])
    sim.slot_busy = False
    assert sim.approvals() == [], sim.approvals()
