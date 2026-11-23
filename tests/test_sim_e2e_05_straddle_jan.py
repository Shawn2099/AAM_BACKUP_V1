"""Card S5 WORST-J (docs/PLAN_E2E_SIMULATION_2026-11-23.md section 1b): the LAN run of Dec 31 straddles
midnight and overruns into the 02:00 cloud slot of Jan 1.

Dec 31 21:00 LAN for FY26-27 (A) runs on past midnight: a hook in its robocopy call moves the clock to
Jan 1 02:30. The Jan 1 cloud slot is then a run that starts at 02:30 and is scheduled for 02:00
(`Ev("cloud", 02:30, sched 02:00)`). The rest of CAL_J follows unchanged.

Notes on the rig use in this card:
  * `play()` refuses an event earlier than the clock, so the 02:00 cloud event of CAL_J is replaced by
    the same event moved to its real start (02:30) with its scheduled instant kept at 02:00.
  * The rig does not derive the lateness from the scheduled instant: `_scheduled_lateness_seconds`
    returns `sim.lateness_s`. The cloud run is therefore played with `lateness_s = 1800` (30 min late,
    as it really starts) and reset to 0 afterwards.
  * Totals and the wake / shutdown count are the card's S1 totals (6 wakes, 6 shutdowns; see S1).
"""
from __future__ import annotations

import pendulum
import pytest

from core.time_utils import IST
from test_support.sim_calendar import CAL_J, Ev, sim_world

FY_A = "FY26-27"
FY_B = "FY27-28"
OK = {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES", "LAN_COMPLETE", "LAN_NO_CHANGES"}
WAKES_TOTAL = 6          # 5 LAN nights + the Jan 3 audit (see S1)
SHUTDOWNS_TOTAL = 6
MAILS_TOTAL = 3          # INFO activation, monthly (Jan 1), weekly (Jan 4)
FAILURE_WORDS = ("FAILURE", "WARNING", "ACTION")
CLOUD_LATE_S = 1800.0    # the 02:00 slot starts at 02:30


def _ist(y, mo, d, h, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


def _snap(sim) -> dict:
    return {
        "runs": sim.runs(),
        "acts": sim.acts(),
        "approvals": sim.approvals(),
        "subjects": sim.subjects(),
        "audits": sim.sql("SELECT mode, fy, status FROM integrity_audits ORDER BY id"),
        "wakes": sim.wakes,
        "shutdowns": sim.shutdowns,
        "errors": len(sim.errors),
    }


def _new_rows(before: dict, after: dict) -> list[dict]:
    seen = {r["id"] for r in before["runs"]}
    return [r for r in after["runs"] if r["id"] not in seen]


def _count(rows, mode, fy) -> int:
    return sum(1 for r in rows if r["mode"] == mode and r["fy"] == fy and r["status"] in OK)


@pytest.fixture(scope="module")
def straddle(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("s5_straddle_jan")
    with pytest.MonkeyPatch.context() as mp, sim_world(tmp, mp) as sim:
        snaps = {}

        def lan_overruns():
            sim.jump(_ist(2027, 1, 1, 2, 30))

        sim.set_answer("lan", FY_A, pendulum.date(2026, 12, 31), hook=lan_overruns)

        sim.play(CAL_J[:4])                       # lan Dec 30, cloud Dec 31, rollover Dec 31, lan Dec 31 (straddles)
        snaps["lan_straddle"] = _snap(sim)

        straddle_cloud = Ev("cloud", _ist(2027, 1, 1, 2, 30), _ist(2027, 1, 1, 2, 0))
        sim.lateness_s = CLOUD_LATE_S
        sim.play([straddle_cloud])
        sim.lateness_s = 0.0
        snaps["cloud_jan1"] = _snap(sim)

        sim.play(CAL_J[5:])                       # rollover Jan 1 06:00 onwards, as in CAL_J
        snaps["end"] = _snap(sim)
        yield {"snaps": snaps, "errors": list(sim.errors)}


# ── the straddling LAN run ───────────────────────────────────────────────────────────────────

def test_lan_straddle_writes_one_row_for_a_started_on_dec_31(straddle):
    rows = straddle["snaps"]["lan_straddle"]["runs"]
    lan_dec31 = [r for r in rows if r["mode"] == "lan" and r["started_at"].startswith("2026-12-31")]
    assert [(r["mode"], r["fy"]) for r in lan_dec31] == [("lan", FY_A)]
    assert lan_dec31[0]["status"] in OK


def test_lan_straddle_creates_no_activation(straddle):
    assert straddle["snaps"]["lan_straddle"]["acts"] == []


# ── the cloud slot that starts at 02:30 ──────────────────────────────────────────────────────

def test_cloud_slot_is_not_missed(straddle):
    rows = straddle["snaps"]["cloud_jan1"]["runs"]
    assert not [r for r in rows if "MISSED" in r["status"]], rows


def test_cloud_jan1_rows_are_b_then_a_and_ok(straddle):
    before = straddle["snaps"]["lan_straddle"]
    after = straddle["snaps"]["cloud_jan1"]
    new = _new_rows(before, after)
    assert [(r["mode"], r["fy"]) for r in new] == [("cloud", FY_B), ("cloud", FY_A)]
    assert all(r["status"] in OK for r in new), new


def test_cloud_jan1_activates_fy27_28_once(straddle):
    acts = straddle["snaps"]["cloud_jan1"]["acts"]
    assert len(acts) == 1, acts
    assert acts[0][0] == FY_B
    assert acts[0][1] == 1                       # created by the app
    assert acts[0][2].startswith("2027-01-01T02:30")


# ── the rest of the calendar, totals as S1 ───────────────────────────────────────────────────

def test_totals_as_in_s1(straddle):
    rows = straddle["snaps"]["end"]["runs"]
    lan_or_cloud = [r for r in rows if r["mode"] in ("lan", "cloud")]
    assert len(lan_or_cloud) == 17
    assert all(r["status"] in OK for r in lan_or_cloud)
    assert _count(rows, "lan", FY_A) == 5
    assert _count(rows, "cloud", FY_A) == 5
    assert _count(rows, "lan", FY_B) == 3
    assert _count(rows, "cloud", FY_B) == 4


def test_wakes_and_shutdowns_as_in_s1(straddle):
    end = straddle["snaps"]["end"]
    assert end["wakes"] == WAKES_TOTAL
    assert end["shutdowns"] == SHUTDOWNS_TOTAL


def test_mails_as_in_s1(straddle):
    subjects = straddle["snaps"]["end"]["subjects"]
    assert len(subjects) == MAILS_TOTAL, subjects
    assert not [s for s in subjects if any(w in s.upper() for w in FAILURE_WORDS)], subjects


def test_no_flow_error_during_the_straddle(straddle):
    assert straddle["errors"] == []
