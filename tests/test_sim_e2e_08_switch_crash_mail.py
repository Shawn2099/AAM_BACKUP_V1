"""Card S8 (docs/PLAN_E2E_SIMULATION_2026-11-23.md): the process dies while the config switch is written
on 1 Apr 06:00, and the mail server is down from 1 Apr 00:00 to 2 Apr 12:00.

Mechanism copied from tests/test_ov_timeline_def.py T-f1 (write_then_die): config.yaml is pointed at
FY27-28, then _Crash. No reboot (T-f1 has none; the card does not ask for one), so the next morning's
check resumes the journal in the same process: R3 in flow._rollover_resume closes the switch step
WITHOUT a second write, because the final backup of FY26-27 had already finished.

Hand expectations: card S8 and PLAN_FY_OVERLAP section 4 (1 Apr rows, 'switch blocked' and 'after the
switch' rows). Oracles: raw SQL, the rig's switch/archive lists, the SMTP server's own messages.
The attention code 'alerts_undelivered' is a product view (# product-view).
"""
from __future__ import annotations

import pendulum
import pytest

import flow
from core.manifest import ManifestDB
from core.time_utils import IST
from test_support.sim_calendar import A, B, CAL_A, _Crash, sim_world
GUARD = [{"guard": "G3_MASS_DELETE", "reason": "x", "metrics": {"planned_deletes": 500, "delete_percent": 5.0}}]

OK = {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES", "LAN_COMPLETE", "LAN_NO_CHANGES"}


def _t(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


@pytest.fixture
def sim(tmp_path, monkeypatch):
    with sim_world(tmp_path, monkeypatch) as s:
        yield s


def _fys_at(sim, leg: str, when) -> set[str]:
    return {c.fy for c in sim.calls if c.leg == leg and c.when == when}


def _index(kind: str, at) -> int:
    return next(i for i, e in enumerate(CAL_A) if e.kind == kind and e.at == at)


def test_s8_config_switch_crash_while_the_mail_server_is_down(sim):
    sim.seed_to_mar28()

    # a PENDING approval of the closing year, as in T-f1 (it must be CLOSED by the switch)
    db = ManifestDB.from_config(sim.cfg)
    try:
        approval_id = db.open_approval_request(
            leg="lan", blocked_run_id=f"blocked-{A}-lan", fy=A, guards=GUARD, phrase="APPROVE",
            created_at=_t(2027, 3, 30, 3).isoformat())
    finally:
        db.close()
    assert sim.approvals() == [("lan", A, "PENDING")]

    def write_then_die(config_path, src_root, lan_root, to_fy, expect_fy=None, **kw):
        sim.switch_writes.append((to_fy, expect_fy))     # the compare-and-set write happened ...
        sim.point_config_at(to_fy)                       # ... config.yaml now points at FY27-28 ...
        raise _Crash("power cut")                        # ... then the process dies

    sim.mp.setattr(flow.fy_rollover, "update_config_yaml", write_then_die)

    k_cloud = _index("cloud", _t(2027, 4, 1, 2))
    k_cut = _index("rollover", _t(2027, 4, 1, 6))
    k_check = _index("rollover", _t(2027, 4, 2, 6))

    sim.play(CAL_A[:k_cloud])                            # up to the 31 Mar 21:00 LAN
    sim.smtp_down()                                      # 1 Apr 00:00 -> 2 Apr 12:00
    msgs_before_outage = len(sim.srv.messages)
    sim.play(CAL_A[k_cloud:k_cut])                       # 1 Apr 02:00 cloud, both folders
    with pytest.raises(_Crash):
        sim.play([CAL_A[k_cut]])                         # 1 Apr 06:00: final backup done, switch dies

    st = _journal_steps(sim)
    assert st["final_backups"] in ("done", "skipped"), st
    assert st["config_switch"] not in ("done", "skipped"), st
    assert sim.switch_writes == [(B, A)], sim.switch_writes
    assert sim.archive_calls == []
    assert sim.approvals() == [("lan", A, "PENDING")]

    # 1 Apr 08:30 monthly, 21:00 LAN (config already says FY27-28: one folder), 2 Apr 02:00 cloud, 06:00 check
    sim.play(CAL_A[k_cut + 1:k_check + 1])
    # (the 06:00 final LAN backup of FY26-27 is called at 06:00, not 21:00: excluded by the instant)
    lan_1apr = _fys_at(sim, "lan", _t(2027, 4, 1, 21))
    assert lan_1apr == {B}, lan_1apr

    check_errors = [repr(e) for ev, e in sim.errors if ev.kind == "rollover"]
    assert check_errors == [], check_errors

    # 2 Apr 06:00 check: the switch is closed WITHOUT a second write, the archive runs once
    assert sim.switch_writes == [(B, A)], sim.switch_writes
    assert [c["old_fy"] for c in sim.archive_calls] == [A]
    st = _journal_steps(sim)
    assert all(s in ("done", "skipped") for s in st.values()), st
    assert sim.approvals() == [("lan", A, "CLOSED")]

    # the undelivered-alert item at 9:00 on 2 Apr (product view, hand text 'alerts_undelivered')
    data = sim.status(_t(2027, 4, 2, 9))
    codes = [item.get("code") for item in data.get("attention", [])]  # product-view
    assert "alerts_undelivered" in codes, codes

    # hand: the INFO "new year started" alert of the 2 Apr 06:00 check was attempted while the server was
    # down and is RECORDED as undelivered (notify() recorded the outcome; a swallow without a record fails)
    assert sim.sql("SELECT status FROM notifications WHERE event_key = 'rollover:done:FY27-28'")         == [("FAILED",)]

    # no mail reached the server while it was down
    sim.go(_t(2027, 4, 2, 12))
    assert len(sim.srv.messages) == msgs_before_outage, sim.subjects()[msgs_before_outage:]
    sim.smtp_up()

    # the rest of the calendar, from 2 Apr 21:00 on: FY27-28 only
    sim.play(CAL_A[k_check + 1:])
    assert sim.acts() and [a[0] for a in sim.acts()] == [B], sim.acts()
    late = sim.sql("SELECT fy, status FROM run_history WHERE mode IN ('lan','cloud') "
                   "AND run_id NOT LIKE '%final%' AND started_at >= '2027-04-02T06:00' ORDER BY id")
    assert late, "no runs after the switch"
    assert {fy for fy, _ in late} == {B}, late
    assert all(status in OK for _, status in late), late
    assert [c["old_fy"] for c in sim.archive_calls] == [A]
    assert sim.switch_writes == [(B, A)], sim.switch_writes

    # hand: no status differs from the quiet office (S2 hand table) after the 2 Apr 06:00 check
    from tests.test_sim_e2e_02_best_apr import NIGHTS
    expected_after = [(m, p, fys) for m, p, fys in NIGHTS if p > "2027-04-02T06"]
    assert len(expected_after) == 6, expected_after
    for mode, prefix, fys in expected_after:
        rows = [r for r in sim.runs() if r["mode"] == mode and r["started_at"].startswith(prefix)]
        assert sorted(r["fy"] for r in rows) == sorted(fys), (mode, prefix, rows)
        assert all(r["status"] in OK for r in rows), (mode, prefix, [(r["fy"], r["status"]) for r in rows])
    rows_after = [r for r in sim.runs() if r["mode"] in ("lan", "cloud") and r["started_at"] > "2027-04-02T06:30"]
    assert len(rows_after) == len(expected_after), [(r["mode"], r["started_at"]) for r in rows_after]


def test_s8_a_failed_close_is_retried_by_the_next_daily_check(sim):
    """Non-vacuity M6: db.close_fy fails once after the 1 Apr 06:00 switch (it must not stop the switch); the
    approval of the closed year stays PENDING until the 2 Apr 06:00 check retries the close, at the start of the
    check (flow._close_finished_old_fys)."""
    sim.seed_to_mar28()
    db = ManifestDB.from_config(sim.cfg)
    try:
        db.open_approval_request(leg="lan", blocked_run_id=f"blocked-{A}-lan", fy=A, guards=GUARD,
                                 phrase="APPROVE", created_at=_t(2027, 3, 30, 3).isoformat())
    finally:
        db.close()
    real_close = ManifestDB.close_fy
    calls = []

    def flaky(self_, fy, now=None):
        calls.append(fy)
        if len(calls) == 1:
            raise RuntimeError("disk busy")
        return real_close(self_, fy, now=now)

    sim.mp.setattr(ManifestDB, "close_fy", flaky)
    k_1apr = _index("rollover", _t(2027, 4, 1, 6))
    k_2apr = _index("rollover", _t(2027, 4, 2, 6))
    sim.play(CAL_A[:k_1apr + 1])                          # through the 1 Apr 06:00 switch (the first close fails)
    assert sim.switch_writes == [(B, A)], sim.switch_writes
    assert calls == [A]
    assert sim.approvals() == [("lan", A, "PENDING")], sim.approvals()
    sim.play(CAL_A[k_1apr + 1:k_2apr + 1])                # the 2 Apr 06:00 check retries the close
    assert sim.approvals() == [("lan", A, "CLOSED")], sim.approvals()
    assert len(calls) == 2


def _journal_steps(sim) -> dict:
    rows = sim.sql("SELECT s.step, s.state FROM rollover_journal_steps s JOIN rollover_journal j "
                   "ON j.id = s.journal_id WHERE j.from_fy = ? AND j.to_fy = ?", A, B)
    return dict(rows)
