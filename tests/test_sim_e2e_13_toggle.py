"""Card S13 (docs/PLAN_E2E_SIMULATION_2026-11-23.md): the overlap switch toggled off and on at both boundaries.

(J) rollover.overlap_enabled False on Dec 31 18:00, True on Jan 2 12:00 (CAL_J, Dec 30 2026 .. Jan 4 2027).
(A) seed to Mar 28, False on Mar 30 12:00, True on Mar 31 12:00, then CAL_A through the Apr 1 switch.

Toggle mechanism as in tests/test_ov_timeline_def.py (T-e1 / T-e2): the config attribute is set directly at the
boundary instant. The rig's flows read the same config object, so the next event sees the new value.
Each test replays its timeline up to a checkpoint, so one assertion group can be read on its own.
Oracles: hand values below, raw SQL, the rig's call log (argv per run), the SMTP server's subjects, folders.
"""
from __future__ import annotations

import pendulum
import pytest

from core.guards import MOUNT_MARKER
from core.time_utils import IST
from test_support.sim_calendar import A, B, CAL_A, CAL_J, Sim, sim_world


def at(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


# hand values (card S13)
J_OFF = at(2026, 12, 31, 18, 0)
J_ON = at(2027, 1, 2, 12, 0)
J_JAN3_CLOUD = at(2027, 1, 3, 2, 0)
J_JAN3_LAN = at(2027, 1, 3, 21, 0)
A_OFF = at(2027, 3, 30, 12, 0)
A_ON = at(2027, 3, 31, 12, 0)
INFO_TITLE = f"{B} is now backed up as well"


@pytest.fixture
def sim(tmp_path, monkeypatch):
    with sim_world(tmp_path, monkeypatch) as s:
        yield s


def _toggle(sim: Sim, when, value: bool):
    """The toggle as a timeline action: the clock reaches the boundary, then the setting changes."""
    def act():
        sim.go(when)                              # the clock reaches the boundary, as a scheduled read would
        sim.cfg.rollover.overlap_enabled = value
    return act


def _staff_saves_in_fy_b(sim: Sim):
    """Setup (card S13 (A) needs a sibling alarm, and the alarm counts real files only, not the placeholder):
    a staff member saves a file into the next year's folder while the overlap is off."""
    def act():
        (sim.src_root / B / "staff_note_saved_while_off.xlsx").write_text("saved while off", encoding="utf-8")
    return act


def _timeline(sim: Sim, events, actions, until) -> None:
    """Play `events` and run the `actions` ((instant, callable) pairs) in time order, up to and including `until`."""
    steps = [(e.at, 0, e) for e in events if e.at <= until]
    steps += [(when, 1, act) for when, act in actions if when <= until]
    for _when, kind, item in sorted(steps, key=lambda s: (s[0], s[1])):
        if kind == 0:
            sim.play([item])
        else:
            item()


def _subjects_with(sim: Sim, *needles: str) -> list[str]:
    return [s for s in sim.subjects() if all(n in s for n in needles)]


def _fys_called(sim: Sim, leg: str, when) -> set[str]:
    return {c.fy for c in sim.calls if c.leg == leg and c.when == when}


# ── (J) off Dec 31 18:00, on Jan 2 12:00 ───────────────────────────────────────────────────────

def test_J_off_through_jan2_only_fy_a_is_backed_up_and_no_activation(sim):
    _timeline(sim, CAL_J, [(J_OFF, _toggle(sim, J_OFF, False)), (J_ON, _toggle(sim, J_ON, True))], until=J_ON)
    assert sim.acts() == [], sim.acts()
    assert not (sim.src_root / B).exists()
    assert {c.fy for c in sim.calls} == {A}, sorted({c.fy for c in sim.calls})
    assert sim.sql("SELECT COUNT(*) FROM run_history WHERE fy = ?", B) == [(0,)]


def test_J_on_by_jan3_0200_activation_one_info_mail_and_both_folders_backed_up(sim):
    _timeline(sim, CAL_J, [(J_OFF, _toggle(sim, J_OFF, False)), (J_ON, _toggle(sim, J_ON, True))],
              until=J_JAN3_CLOUD)
    assert len(sim.acts()) == 1, sim.acts()
    assert sim.acts()[0][0] == B
    assert len(_subjects_with(sim, INFO_TITLE)) == 1, sim.subjects()
    assert _fys_called(sim, "cloud", J_JAN3_CLOUD) == {A, B}


def test_J_nas_b_folder_exists_after_the_jan3_2100_lan(sim):
    _timeline(sim, CAL_J, [(J_OFF, _toggle(sim, J_OFF, False)), (J_ON, _toggle(sim, J_ON, True))],
              until=J_JAN3_LAN)
    assert _fys_called(sim, "lan", J_JAN3_LAN) == {A, B}
    assert (sim.nas_root / B / MOUNT_MARKER).exists()


# ── (A) off Mar 30 12:00, on Mar 31 12:00, then the Apr 1 switch ────────────────────────────────

def _seeded_then(sim: Sim, until) -> None:
    sim.seed_to_mar28()
    _timeline(sim, CAL_A, [(A_OFF, _toggle(sim, A_OFF, False)), (A_OFF, _staff_saves_in_fy_b(sim)),
                           (A_ON, _toggle(sim, A_ON, True))], until=until)


def test_A_off_mar30_2100_and_mar31_0200_are_fy_a_only_with_a_sibling_alarm(sim):
    _seeded_then(sim, until=at(2027, 3, 31, 6, 0))
    assert _fys_called(sim, "lan", at(2027, 3, 30, 21, 0)) == {A}
    assert _fys_called(sim, "cloud", at(2027, 3, 31, 2, 0)) == {A}
    assert _subjects_with(sim, B, "not backed up yet"), sim.subjects()


def test_A_on_mar31_2100_backs_up_both_and_sends_no_second_info(sim):
    _seeded_then(sim, until=at(2027, 3, 31, 21, 0))
    assert _fys_called(sim, "lan", at(2027, 3, 31, 21, 0)) == {A, B}
    assert len(_subjects_with(sim, INFO_TITLE)) == 1, sim.subjects()


def test_A_apr1_switch_happens_once_and_the_activation_row_is_single(sim):
    _seeded_then(sim, until=CAL_A[-1].at)
    assert sim.switch_writes == [(B, A)], sim.switch_writes
    assert [c["old_fy"] for c in sim.archive_calls] == [A], sim.archive_calls
    assert sim.sql("SELECT COUNT(*) FROM overlap_activations") == [(1,)]
