"""Card S3 (docs/PLAN_E2E_SIMULATION_2026-11-23.md section 1b): WORST-J, NAS down on Dec 31.

Hand table (IST, derived from CAL_J and the plan, never copied from product output):
  Dec 30 21:00  lan A       OK (one folder; NAS powered off after the complete mirror)
  Dec 31 21:00  lan A       NAS unreachable: the wake answers False and the LAN preflight fails.
                            -> exactly 1 lan row for Dec 31 (FY26-27), status not OK, the flow raises
                               (a non-success never returns normally), 0 robocopy calls, >= 1 FAILURE
                               mail naming LAN.
  Jan 1 02:00   activation  FY27-28 activated exactly as in S1: 1 overlap activation row
                            (created_by_app=1), cloud B and cloud A OK.
  Jan 1 21:00   lan B, A    OK; NAS folder FY27-28 with the mount marker is created; exactly 1 RECOVERED
                            mail for lan A, none for lan B.
Only the Dec 31 night raises.

Card deviation (recorded in the report): the card's stub "returns {ok: False, ...}" would NOT fail the
run, because the real lan_preflight_task RAISES when the dry run is not ok (flow.py). The stub therefore
raises the same coded error the real task raises (AAM-LAN-01). The wake answer is set as the card says
(sim.wake_ok = False); the real wol_check_task ignores that return value.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pendulum
import pytest

import flow
from core import faults
from core.time_utils import IST
from test_support.sim_calendar import CAL_J, A, B, lan, sim_world

OK = {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES", "LAN_COMPLETE", "LAN_NO_CHANGES"}   # copied by value (plan 0)
PLACEHOLDER = "README_FY.txt"            # rollover.source_placeholder_name default (hand value)
MARKER = ".AAM_TARGET_MOUNTED"           # core.guards.MOUNT_MARKER (hand value)


@pytest.fixture
def sim(tmp_path, monkeypatch):
    with sim_world(tmp_path, monkeypatch) as s:
        yield s


def _at(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


def _rows(sim, mode: str, day: str) -> list[dict]:
    """run rows of `mode` whose start date (first 10 characters, IST) is `day`, in id order."""
    return [r for r in sim.runs() if r["mode"] == mode and r["started_at"][:10] == day]


def _failing_preflight(sim, when):
    """The LAN preflight stand-in: passes everywhere, and at `when` raises what the real task raises."""

    def answer(config, artefacts_dir=None):
        if sim.now == when:
            raise faults.coded(RuntimeError("LAN preflight failed: NAS unreachable"), "AAM-LAN-01")
        return {"ok": True, "exit_code": 0, "error": None}

    stub = MagicMock(side_effect=answer)
    stub.with_options.return_value = stub
    return stub


def test_s3_nas_down_on_dec31_fails_one_run_and_jan1_recovers(sim):
    dec31_lan = lan(pendulum.date(2026, 12, 31))
    jan1_lan = lan(pendulum.date(2027, 1, 1))
    sim.mp.setattr(flow, "lan_preflight_task", _failing_preflight(sim, dec31_lan.at))

    # Dec 30 21:00 and the Dec 31 cloud / rollover events, before the failing night
    sim.play([e for e in CAL_J if e.at < dec31_lan.at])
    argv_before = len(sim.argv("lan"))
    errors_before = len(sim.errors)

    # Dec 31 21:00: the NAS does not answer the wake
    sim.wake_ok = False
    sim.play([dec31_lan])
    sim.wake_ok = True

    # hand: one lan row for Dec 31, FY26-27, not OK; the flow raised; no robocopy call
    assert len(sim.errors) == errors_before + 1, sim.errors
    assert sim.errors[-1][0].at == dec31_lan.at
    dec31 = _rows(sim, "lan", "2026-12-31")
    assert len(dec31) == 1, dec31
    assert dec31[0]["fy"] == A
    assert dec31[0]["status"] not in OK, dec31[0]["status"]
    assert len(sim.argv("lan")) == argv_before, "robocopy was called on a night the NAS is down"
    # the subject words: FAILURE is worded PROBLEM and the LAN leg is "Office NAS backup" (email_compose)
    assert any(f"[{A}] PROBLEM: Office NAS backup" in s for s in sim.subjects()), sim.subjects()

    # Jan 1 02:00 and the rest of the day, before the NAS is back at 21:00
    tail = [e for e in CAL_J if e.at > dec31_lan.at]
    sim.play([e for e in tail if e.at < jan1_lan.at])

    # hand: activation unaffected (1 row, created by the app); cloud B and A OK
    assert [(fy, created) for fy, created, _ in sim.acts()] == [(B, 1)]
    assert (sim.src_root / B / PLACEHOLDER).exists()
    cloud_jan1 = _rows(sim, "cloud", "2027-01-01")
    assert sorted(r["fy"] for r in cloud_jan1) == sorted([A, B]), cloud_jan1
    assert all(r["status"] in OK for r in cloud_jan1), cloud_jan1

    # Jan 1 21:00: the NAS answers; lan B and lan A OK, the FY27-28 NAS folder + marker created
    sim.play([jan1_lan])
    lan_jan1 = _rows(sim, "lan", "2027-01-01")
    assert [r["fy"] for r in lan_jan1] == [B, A], lan_jan1      # new FY first (plan 5.3)
    assert all(r["status"] in OK for r in lan_jan1), lan_jan1
    assert (sim.nas_root / B / MARKER).exists()
    recovered_a = [s for s in sim.subjects() if f"[{A}] RESOLVED: Office NAS backup" in s]
    recovered_b = [s for s in sim.subjects() if f"[{B}] RESOLVED: Office NAS backup" in s]
    assert len(recovered_a) == 1, sim.subjects()
    assert len(recovered_b) == 0, sim.subjects()

    # the rest of CAL_J: nothing else raises
    sim.play([e for e in tail if e.at > jan1_lan.at])
    assert [e.at for e, _ in sim.errors] == [dec31_lan.at], sim.errors
