"""Card S4 (docs/PLAN_E2E_SIMULATION_2026-11-23.md section 1b): WORST-J, cloud auth failure on the
activation night while the mail server is down.

Hand table (IST, derived from CAL_J and the plan):
  Jan 1 00:00   SMTP goes down (connection refused) until Jan 2 00:00.
  Jan 1 02:00   activation of FY27-28: exactly 1 overlap activation row, folder + placeholder created.
                cloud B and cloud A: rclone exits 1 with the U14 case (c) log (401 Invalid Credentials,
                fatalError=true) -> CLOUD_FAILED each, not retried: exactly 1 `sync` call per FY.
  Jan 1 09:00   dashboard shows the undelivered-alert item (product view).
  Jan 1 21:00   lan B and lan A OK (LAN is not affected by the cloud auth failure).
  Jan 2 02:00   cloud B and cloud A OK again; RECOVERED mails for both FYs reach the server.
Mails: nothing reaches the server while the port is closed.
Open question (plan section 5.2 / card): whether the INFO activation mail is ever delivered after the
outage. That is RECORDED as finding E2E-MAIL-1, not asserted.
"""

from __future__ import annotations

import pendulum
import pytest

from core.time_utils import IST
from test_support.sim_calendar import CAL_J, A, B, cloud, sim_world
from tests.test_break_release_cloudlog import CASE_C_LOG

OK = {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES", "LAN_COMPLETE", "LAN_NO_CHANGES"}   # copied by value (plan 0)
PLACEHOLDER = "README_FY.txt"            # rollover.source_placeholder_name default (hand value)


@pytest.fixture
def sim(tmp_path, monkeypatch):
    with sim_world(tmp_path, monkeypatch) as s:
        yield s


def _at(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


def _rows(sim, mode: str, day: str) -> list[dict]:
    return [r for r in sim.runs() if r["mode"] == mode and r["started_at"][:10] == day]


def test_s4_cloud_401_on_activation_night_with_smtp_outage(sim):
    jan1 = pendulum.date(2027, 1, 1)
    activation_run = cloud(jan1)
    # U14 case (c): 401 Invalid Credentials, fatalError=true, exit 1 (the exact lines of that test)
    for fy in (A, B):
        sim.set_answer("cloud", fy, jan1, exit_code=1, log=CASE_C_LOG)

    outage_on, outage_off = _at(2027, 1, 1, 0), _at(2027, 1, 2, 0)
    status_at = _at(2027, 1, 1, 9)

    # Dec 30 .. Jan 1 00:00 with a working mail server
    sim.play([e for e in CAL_J if e.at < outage_on])

    sim.go(outage_on)
    sim.smtp_down()
    msgs_at_down = len(sim.srv.messages)

    # Jan 1 00:00 .. 09:00: activation, cloud failure, rollover, monthly report (all during the outage)
    sim.play([e for e in CAL_J if outage_on <= e.at < status_at])
    status = sim.status(status_at)                                            # product-view
    # Jan 1 09:00 .. Jan 2 00:00: LAN night of Jan 1 (still during the outage)
    sim.play([e for e in CAL_J if status_at <= e.at < outage_off])

    # no mail reached the server during the outage (the port was closed the whole time)
    assert len(sim.srv.messages) == msgs_at_down, sim.subjects()[msgs_at_down:]

    # hand: activation row exactly 1, created by the app; folder + placeholder exist
    assert [(fy, created) for fy, created, _ in sim.acts()] == [(B, 1)]
    assert (sim.src_root / B / PLACEHOLDER).exists()

    # hand: cloud B and A both CLOUD_FAILED on Jan 1 (the U14 case (c) verdict)
    cloud_jan1 = _rows(sim, "cloud", "2027-01-01")
    assert sorted(r["fy"] for r in cloud_jan1) == sorted([A, B]), cloud_jan1
    assert all(r["status"] == "CLOUD_FAILED" for r in cloud_jan1), cloud_jan1

    # hand: exactly one rclone sync per FY that night; an auth failure is not retried
    syncs = [c for c in sim.calls if c.leg == "cloud" and c.run_date == "2027-01-01" and "sync" in c.argv]
    assert sorted(c.fy for c in syncs) == sorted([A, B]), [(c.fy, c.argv) for c in syncs]

    # hand: the failed cloud flow raised (a failure never returns as COMPLETED)
    assert any(ev.kind == "cloud" and ev.at == activation_run.at for ev, _ in sim.errors), sim.errors

    # product-view: the dashboard shows the undelivered-alert item on Jan 1 09:00
    codes = [item["code"] for item in status["attention"]]
    assert "alerts_undelivered" in codes, codes

    # the mail server comes back on Jan 2 00:00
    sim.go(outage_off)
    sim.smtp_up()
    msgs_at_up = len(sim.srv.messages)
    assert msgs_at_up == msgs_at_down

    # Jan 2 02:00 onward: cloud B and A OK again; the RECOVERED mails arrive
    sim.play([e for e in CAL_J if e.at >= outage_off])
    cloud_jan2 = _rows(sim, "cloud", "2027-01-02")
    assert sorted(r["fy"] for r in cloud_jan2) == sorted([A, B]), cloud_jan2
    assert all(r["status"] in OK for r in cloud_jan2), cloud_jan2

    after = sim.subjects()[msgs_at_up:]
    for fy in (A, B):
        recovered = [s for s in after if f"[{fy}] RESOLVED: Online backup" in s]
        assert len(recovered) >= 1, (fy, after)

    # E2E-MAIL-1 (recorded, not asserted): was the INFO activation mail (Jan 1 02:00, during the
    # outage) ever delivered? Printed for the report; the test does not decide it.
    info_b = [s for s in sim.subjects() if " INFO: " in s and f"[{B}]" in s]
    print(f"E2E-MAIL-1 observation: INFO activation mails for {B} delivered = {bool(info_b)}; {info_b}")
