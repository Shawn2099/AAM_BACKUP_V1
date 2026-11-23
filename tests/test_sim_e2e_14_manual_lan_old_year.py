"""Card S14 (finding FV-1, owner decision 2026-11-23): manual-only LAN, NAS off at the 1 April rollover.

A financial year is CLOSED (tracking stopped) only once every enabled leg has fully copied it. The rollover
still SWITCHES on 1 April (owner decision, unchanged), but FY26-27 stays tracked, so the manual LAN button can
still copy it once the NAS is on; only then is it closed.

Hand table (IST), written from the owner decision and PLAN_FY_OVERLAP section 4, never from product output:
  Mar 29 .. Apr 1 02:00  cloud nightly (FY27-28 + FY26-27 overlap); no LAN runs (manual only)
  Apr 1 06:00 rollover   NAS does not wake: final LAN backup = LAN_FAILED (NAS unreachable, expected); the
                         switch goes ahead (config -> FY27-28); cloud final OK. FY26-27 is NOT closed.
  Apr 1 08:30            the monthly report says 'FY26-27 is not yet fully copied to LAN' (never 'all OK')
  Apr 2 02:00 cloud      both folders, FY27-28 first (the old year is still tracked)
  Apr 5 / Apr 6          manual LAN presses that fail (NAS off / robocopy error): the year stays tracked
  Apr 10 manual LAN      NAS back: LAN_COMPLETE for FY27-28 AND FY26-27 (real rows, started after Apr 1)
  Apr 11 06:00 rollover  the daily check closes FY26-27 (tombstone, one RESOLVED mail); it is no longer
                         tracked: active set = [FY27-28]; the Apr 12 cloud night backs up FY27-28 only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pendulum
import pytest

import ui
from core import fy_overlap as ov
from core.manifest import ManifestDB
from test_support.sim_calendar import A, B, CAL_A, _Crash, cloud, lan, monthly, rollover, sim_world

OK = {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE", "LAN_COMPLETE"}
CLOSED_KEY = f"fy_copy_closed:{A}"


@pytest.fixture
def sim(tmp_path, monkeypatch):
    with sim_world(tmp_path, monkeypatch) as s:
        yield s


def _d(m, d):
    return pendulum.date(2027, m, d)


def _before(kind, day):
    """CAL_A events up to (not including) the `kind` event of `day`; LAN nights are left out (manual only)."""
    stop = next(i for i, e in enumerate(CAL_A) if e.kind == kind and e.at.date() == day)
    return [e for e in CAL_A[:stop] if e.kind != "lan"]


def _tracked(sim, day) -> list[str]:
    db = ManifestDB.from_config(sim.cfg)
    try:
        return ov.active_folders(sim.cfg, db, day)
    finally:
        db.close()


def _lan_rows(sim, after: str):
    return [(r["fy"], r["status"]) for r in sim.runs() if r["mode"] == "lan" and r["started_at"] >= after]


def _bodies(sim) -> str:
    return "\n".join(m["data"] for m in sim.srv.messages)


def test_s14_manual_lan_nas_off_at_rollover_then_the_old_year_is_copied_and_closed(sim):
    sim.seed_to_mar28()
    sim.cfg.lan.scheduled = False                                  # the office copy is started by hand

    # Mar 29 .. Apr 1 02:00: nothing but cloud nights and idle rollover checks
    sim.play(_before("rollover", _d(4, 1)))
    assert not sim.errors, sim.errors
    sim.nas_unreachable = True
    rolled = rollover(_d(4, 1))
    sim.play([rolled])
    assert not sim.errors, sim.errors                              # the rollover completes: no block

    # the switch went ahead; the closing year is NOT closed and stays tracked (new year first)
    assert sim.switch_writes == [(B, A)]
    assert _tracked(sim, _d(4, 2)) == [B, A]
    assert sim.sql("SELECT value FROM db_meta WHERE key = ?", CLOSED_KEY) == []
    final = {(r["mode"], r["fy"]): r["status"] for r in sim.runs() if r["run_id"].find("-final-") > 0}
    assert final[("cloud", A)] in OK and final[("lan", A)] == "LAN_FAILED", final

    # dashboard: both cards, the old one says plainly what is missing; the amber item; truth, never 'all OK'
    sim.status(pendulum.datetime(2027, 4, 1, 7, tz=rolled.at.timezone))
    data = sim.status_reads[-1]
    cards = {f["fy"]: f for f in data["folders"]}
    assert list(cards) == [B, A]
    assert cards[A]["role"] == "old" and cards[B]["role"] == "current"
    assert cards[A]["copy_note"] == f"{A} is not yet fully copied to LAN"
    assert cards[B]["copy_note"] is None
    old_items = [i for i in data["attention"] if i["code"] == f"old_year:{A}"]
    assert len(old_items) == 1 and old_items[0]["level"] != "red"
    assert f"{A} is not yet fully copied to LAN" in old_items[0]["message"]

    # exactly one warning mail, truthful (no 'will be brought up to date'), then the monthly report
    warn = [s for s in sim.subjects() if f"{A} is not yet fully copied to LAN" in s]
    assert len(warn) == 1 and f"[{A}] WARNING" in warn[0], sim.subjects()
    sim.play([monthly(_d(4, 1))])
    assert not sim.errors, sim.errors
    assert _bodies(sim).count("is not yet fully copied to LAN") >= 2        # warning mail + monthly report
    assert "up to date" not in _bodies(sim).split("not yet fully copied")[1][:600]

    # the old year is still backed up by the nightly cloud leg, new year first
    sim.play([cloud(_d(4, 2))])
    night = [(r["fy"], r["status"]) for r in sim.runs() if r["mode"] == "cloud" and r["started_at"].startswith("2027-04-02")]
    assert night == [(B, "CLOUD_COMPLETE"), (A, "CLOUD_COMPLETE")], night

    # both folders are accepted by the dashboard (?fy= of a switched-but-not-fully-copied year) and the flow
    db = ManifestDB.from_config(sim.cfg)
    sim.mp.setattr(ui, "get_db", lambda: db)
    try:
        for raw in (A, B, A.lower()):
            req = SimpleNamespace(query_params={"fy": raw})
            assert ui._requested_fy(req) in (A, B)
        with pytest.raises(Exception, match="not backed up at the moment"):
            ui._requested_fy(SimpleNamespace(query_params={"fy": "FY25-26"}))     # an arbitrary old year: refused
    finally:
        db.close()

    # manual LAN presses that do not copy the old year: the NAS is still off, then a robocopy failure
    errors_before = len(sim.errors)
    sim.play([lan(_d(4, 5))])
    assert len(sim.errors) == errors_before + 1                     # a failed press raises, as always
    sim.nas_unreachable = False
    sim.set_answer("lan", A, _d(4, 6), exit_code=16, log="")
    sim.play([lan(_d(4, 6))])
    old_year_presses = [st for fy, st in _lan_rows(sim, "2027-04-05") if fy == A]
    assert old_year_presses and all(st not in OK for st in old_year_presses), old_year_presses
    assert _tracked(sim, _d(4, 7)) == [B, A], "a failed or partial sync must not close the year"
    assert sim.sql("SELECT value FROM db_meta WHERE key = ?", CLOSED_KEY) == []

    # the NAS is back: the manual LAN press copies BOTH folders for real, then the daily check closes FY26-27
    sim.play([lan(_d(4, 10))])
    ok_rows = [(r["fy"], r["status"]) for r in sim.runs()
               if r["mode"] == "lan" and r["started_at"].startswith("2027-04-10")]
    assert ok_rows == [(B, "LAN_COMPLETE"), (A, "LAN_COMPLETE")], ok_rows
    assert any(c.leg == "lan" and c.fy == A and c.when.date() == _d(4, 10) for c in sim.calls), \
        "robocopy must really have been called for FY26-27 (no fake rows)"
    assert _tracked(sim, _d(4, 11)) == [B]                          # derived: no longer tracked at once
    assert sim.sql("SELECT value FROM db_meta WHERE key = ?", CLOSED_KEY) == []      # not yet written

    sim.play([rollover(_d(4, 11))])
    assert sim.sql("SELECT value FROM db_meta WHERE key = ?", CLOSED_KEY) != []      # closed by the daily check
    assert any(f"[{A}] RESOLVED" in s for s in sim.subjects()), sim.subjects()
    assert _tracked(sim, _d(4, 12)) == [B]
    sim.status(pendulum.datetime(2027, 4, 11, 9, tz=rolled.at.timezone))
    assert [f["fy"] for f in sim.status_reads[-1]["folders"]] == [B]
    assert not [i for i in sim.status_reads[-1]["attention"] if i["code"].startswith("old_year:")]
    sim.play([cloud(_d(4, 12))])
    assert [r["fy"] for r in sim.runs() if r["mode"] == "cloud" and r["started_at"].startswith("2027-04-12")] == [B]


def test_s14_a_restart_between_the_switch_and_the_copy_keeps_the_old_year_tracked(sim):
    sim.seed_to_mar28()
    sim.cfg.lan.scheduled = False
    sim.play(_before("rollover", _d(4, 1)))
    sim.nas_unreachable = True
    sim.play([rollover(_d(4, 1))])
    assert _tracked(sim, _d(4, 2)) == [B, A]
    with pytest.raises(_Crash):
        sim.power_cut()                                                                  # what is on disk survives
    sim.reboot(pendulum.datetime(2027, 4, 2, 9, tz=rollover(_d(4, 1)).at.timezone))
    assert _tracked(sim, _d(4, 3)) == [B, A]
    sim.play([rollover(_d(4, 3))])                                                       # daily checks do not close it
    assert _tracked(sim, _d(4, 4)) == [B, A]
    assert sim.sql("SELECT value FROM db_meta WHERE key = ?", CLOSED_KEY) == []


def test_s14_the_normal_rollover_still_closes_the_year_at_the_switch(sim):
    sim.seed_to_mar28()
    sim.play([e for e in CAL_A if e.at <= rollover(_d(4, 1)).at])
    assert not sim.errors, sim.errors
    assert sim.switch_writes == [(B, A)]
    assert sim.sql("SELECT value FROM db_meta WHERE key = ?", CLOSED_KEY) != []
    assert _tracked(sim, _d(4, 2)) == [B]
    assert not [s for s in sim.subjects() if "not yet fully copied" in s]


def test_s14_scheduled_lan_with_the_nas_down_still_blocks_the_switch(sim):
    sim.seed_to_mar28()                                              # LAN stays scheduled (nightly)
    sim.play([e for e in CAL_A if e.at < rollover(_d(4, 1)).at])
    sim.nas_unreachable = True
    sim.play([rollover(_d(4, 1))])
    assert sim.switch_writes == [], "the switch must stay blocked for a nightly LAN leg (unchanged)"
    assert _tracked(sim, _d(4, 2)) == [B, A]                         # overlap window, not an old year
    assert sim.sql("SELECT value FROM db_meta WHERE key = ?", CLOSED_KEY) == []
    assert json.dumps(sim.approvals()).count("rollover") >= 1
