"""Card S7 (docs/PLAN_E2E_SIMULATION_2026-11-23.md): power loss during the final cloud backup of the
closing year (FY26-27) on 1 Apr 06:00, reboot at 06:20, then the calendar continues.

Hand expectations come from the card and from PLAN_FY_OVERLAP_2026-11-20.md section 4 (1 Apr rows):
the switch waits for the final backup, the active set is [B, A] while the switch is pending, and the
06:00 check of the next day completes the switch once (archive FY26-27 once, no second activation).

Oracles: the snapshot and manifest.db (raw SQL), the argv / call list of the scripted tools, the SMTP
server's own messages. Product views are not asserted here.
"""
from __future__ import annotations

import sqlite3

import pendulum
import pytest

from core.time_utils import IST
from test_support.sim_calendar import A, B, CAL_A, _Crash, sim_world

OK = {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES", "LAN_COMPLETE", "LAN_NO_CHANGES"}
CUT_AT = pendulum.datetime(2027, 4, 1, 6, 0, tz=IST)      # the final cloud backup of FY26-27
REBOOT_AT = pendulum.datetime(2027, 4, 1, 6, 20, tz=IST)  # the office restarts 20 minutes later


@pytest.fixture
def sim(tmp_path, monkeypatch):
    with sim_world(tmp_path, monkeypatch) as s:
        yield s


def _t(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


def _fys_at(sim, leg: str, when) -> set[str]:
    """The FYs the scripted tool was called for by one scheduled event (exact instant; the 06:00
    rollover's final backups are called at 06:00 and so never count as a 21:00 or 02:00 run)."""
    return {c.fy for c in sim.calls if c.leg == leg and c.when == when}


def _cut_index() -> int:
    return next(i for i, e in enumerate(CAL_A) if e.kind == "rollover" and e.at == CUT_AT)


def _snap_sql(sim, query: str, *args) -> list[tuple]:
    con = sqlite3.connect(sim.tmp / "snap.db")
    try:
        return [tuple(r) for r in con.execute(query, args).fetchall()]
    finally:
        con.close()


def test_s7_power_cut_in_the_final_cloud_backup_then_reboot_and_the_switch_still_completes(sim):
    sim.seed_to_mar28()
    k = _cut_index()
    cut_day = pendulum.date(2027, 4, 1)

    def cut_during_final_backup():
        # the 02:00 cloud run of 1 Apr uses the same key: only the 06:00 final backup dies
        if sim.now == CUT_AT:
            # a dead process sends no mail; the rig's in-process failure alert must not reach the server
            sim.smtp_down()
            sim.power_cut()                              # copies manifest.db, then raises _Crash

    sim.set_answer("cloud", A, cut_day, hook=cut_during_final_backup)

    sim.play(CAL_A[:k])                                  # up to 31 Mar + the 1 Apr 02:00 runs
    with pytest.raises(_Crash):
        sim.play([CAL_A[k]])                             # 1 Apr 06:00 rollover: the final cloud backup dies

    # --- what the disk held at the instant of the cut (the snapshot) ---
    final_at_cut = _snap_sql(sim, "SELECT status FROM run_history WHERE mode='cloud' AND fy=? "
                                  "AND run_id LIKE '%final%' ORDER BY id", A)
    assert final_at_cut == [("CLOUD_RUNNING",)], final_at_cut
    assert sim.switch_writes == [], "no config switch may happen before the final backup completes"
    assert sim.archive_calls == []

    # --- the reboot: the office restarts at 06:20; the sweep marks what the cut left RUNNING ---
    sim.smtp_up()
    sim.reboot(REBOOT_AT)

    final_rows = sim.sql("SELECT status FROM run_history WHERE mode='cloud' AND fy=? "
                         "AND run_id LIKE '%final%' ORDER BY id", A)
    assert final_rows == [("CLOUD_ABANDONED",)], final_rows
    assert sim.sql("SELECT COUNT(*) FROM run_history WHERE status LIKE '%_RUNNING'") == [(0,)]
    assert sim.switch_writes == [], "the reboot must not switch the config"
    assert sim.archive_calls == []
    # the sweep's mail for the abandoned cloud run (its subject says 'interrupted', not 'abandoned')
    cloud_mails = [s for s in sim.subjects() if "Google Cloud" in s and "interrupted" in s]
    assert len(cloud_mails) == 1, sim.subjects()
    assert A in cloud_mails[0], cloud_mails[0]

    # --- the rest of the calendar: 1 Apr 08:30 monthly, 21:00 LAN, 2 Apr 02:00 cloud, 06:00 check ... ---
    sim.play(CAL_A[k + 1:])

    lan_1apr = _fys_at(sim, "lan", _t(2027, 4, 1, 21))
    assert lan_1apr == {A, B}, lan_1apr                  # the switch is pending: both folders
    cloud_2apr = _fys_at(sim, "cloud", _t(2027, 4, 2, 2))
    assert cloud_2apr == {A, B}, cloud_2apr

    # the next morning's check re-runs the final backup of FY26-27 (a new row), then switches once
    finals = sim.sql("SELECT status FROM run_history WHERE mode='cloud' AND fy=? "
                     "AND run_id LIKE '%final%' ORDER BY id", A)
    assert len(finals) == 2 and finals[0] == ("CLOUD_ABANDONED",), finals
    assert finals[1][0] in OK, finals
    assert sim.switch_writes == [(B, A)], sim.switch_writes
    assert [c["old_fy"] for c in sim.archive_calls] == [A]

    states = sim.sql("SELECT s.state FROM rollover_journal_steps s JOIN rollover_journal j "
                     "ON j.id = s.journal_id WHERE j.from_fy = ? AND j.to_fy = ?", A, B)
    assert states and all(s in ("done", "skipped") for (s,) in states), states

    assert sim.acts() and [a[0] for a in sim.acts()] == [B], sim.acts()   # one activation, ever
    # (the former 'backup.lock absent' check was trivially true: the rig fakes the slot, so no lock is ever
    # written; replaced by what the sweep and the re-run really leave: no row is still RUNNING at the end)
    assert sim.sql("SELECT COUNT(*) FROM run_history WHERE status LIKE '%_RUNNING'") == [(0,)]

    # after the switch only FY27-28 is backed up
    late = sim.sql("SELECT fy, status FROM run_history WHERE mode IN ('lan','cloud') "
                   "AND run_id NOT LIKE '%final%' AND started_at >= '2027-04-02T06:00' ORDER BY id")
    assert late, "no runs after the switch"
    assert {fy for fy, _ in late} == {B}, late
    assert all(status in OK for _, status in late), late
