"""Card S0 self-tests for the calendar simulation rig (test_support/sim_calendar.py).

(1) the moved clock agrees across pendulum, datetime, time.time() and core.time_utils.now_iso();
(2) jump() inside a running hook moves now_iso();
(3) one lan() event = 1 run row, 1 NAS wake, 1 NAS shutdown;
(4) every event finishes in under 5 s; plus a smoke run of CAL_J.
Oracles: the hand value 2026-12-31T15:30:00Z, raw SQL, the rig's own counters.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

import pendulum
import pytest

from core.time_utils import IST, now_iso
from test_support.sim_calendar import CAL_J, A, lan, sim_world

HAND_UTC = datetime(2026, 12, 31, 15, 30, 0, tzinfo=UTC)   # = 2026-12-31 21:00 IST
HAND_ISO = "2026-12-31T21:00:00+05:30"
MAX_EVENT_S = 5.0


@pytest.fixture
def sim(tmp_path, monkeypatch):
    with sim_world(tmp_path, monkeypatch) as s:
        yield s


def _at(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


def test_1_the_moved_clock_reads_the_hand_value_everywhere(sim):
    sim.go(_at(2026, 12, 31, 21, 0))
    hand_ts = HAND_UTC.timestamp()
    readings = {
        "pendulum.now": pendulum.now().timestamp(),
        "datetime.now(utc)": datetime.now(UTC).timestamp(),
        "time.time()": time.time(),
        "core.time_utils.now_iso()": datetime.fromisoformat(now_iso()).timestamp(),
    }
    for name, ts in readings.items():
        assert abs(ts - hand_ts) <= 1.0, f"{name} reads {ts - hand_ts:+.3f} s off {HAND_UTC.isoformat()}"
    assert now_iso() == HAND_ISO                      # the rig's string, not only the instant


def test_2_jump_inside_a_running_hook_moves_now_iso(sim):
    seen = {}

    def hook():
        sim.jump(_at(2027, 1, 1, 2, 30))
        seen["iso"] = now_iso()

    sim.set_answer("lan", A, pendulum.date(2026, 12, 31), hook=hook)
    sim.play([lan(pendulum.date(2026, 12, 31))])
    assert sim.errors == [], sim.errors
    assert seen["iso"] == "2027-01-01T02:30:00+05:30"
    assert now_iso() == "2027-01-01T02:30:00+05:30"    # the clock stays where the hook left it


def test_3_one_lan_event_is_one_row_one_wake_one_shutdown(sim):
    sim.play([lan(pendulum.date(2026, 12, 30))])
    assert sim.errors == [], sim.errors
    rows = sim.runs()
    assert [(r["mode"], r["fy"], r["status"]) for r in rows] == [("lan", A, "LAN_COMPLETE")]
    assert sim.wakes == 1
    assert sim.shutdowns == 1


def test_4_each_event_finishes_under_five_seconds(sim):
    sim.play(CAL_J[:4])                               # lan, cloud, rollover, lan
    assert sim.errors == [], sim.errors
    assert len(sim.event_seconds) == 4
    slow = [(i, s) for i, s in enumerate(sim.event_seconds) if s >= MAX_EVENT_S]
    assert slow == [], f"events over {MAX_EVENT_S} s: {slow}"


def test_smoke_play_of_cal_j_runs_without_error(sim):
    sim.play(CAL_J)
    assert sim.errors == [], sim.errors
    assert max(sim.event_seconds) < MAX_EVENT_S
    assert sim.now == CAL_J[-1].at
