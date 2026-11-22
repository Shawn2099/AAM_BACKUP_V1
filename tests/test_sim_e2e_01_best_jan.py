"""Card S1 BEST-J (docs/PLAN_E2E_SIMULATION_2026-11-23.md section 1b): the best-case January.

Dec 30 21:00 -> Jan 4 09:00 (IST), one office, one FY26-27 source file, every tool call succeeds.

The calendar is replayed ONCE (module fixture): each event of CAL_J is played on its own and a snapshot
of every oracle is taken right after it. The tests only read those snapshots, the SMTP server and the
raw DB, so one assertion group can be xfailed without touching the others.

Hand values (the card's table; FY26-27 = A is configured, FY27-28 = B is activated by the first backup
of 1 Jan 2027, 02:00):
  * wakes: the LAN leg runs on five nights (Dec 30, Dec 31, Jan 1, Jan 2, Jan 3 21:00) and each night
    wakes the NAS once and powers it off once. The Sunday audit of Jan 3 08:00 also finds the NAS asleep
    (the Jan 2 night shut it down), wakes it, and powers it off after its LAN part. 5 + 1 = 6 wakes and
    6 shutdowns. The card's "wakes 5, shutdowns 5" left the audit out.
  * mails: the INFO activation mail, the monthly report (Jan 1) and the weekly report (Jan 4) = 3.
"""
from __future__ import annotations

import copy
import email
import re

import pendulum
import pytest

from core.time_utils import IST
from test_support.sim_calendar import CAL_J, sim_world

FY_A = "FY26-27"          # configured (source_drive = .../FY26-27)
FY_B = "FY27-28"          # the early folder, activated 1 Jan 2027 02:00
OK = {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES", "LAN_COMPLETE", "LAN_NO_CHANGES"}
PLACEHOLDER = "README_FY.txt"        # RolloverConfig.source_placeholder_name default (not overridden by the rig)
MARKER = ".AAM_TARGET_MOUNTED"       # core.guards.MOUNT_MARKER

# Hand table of CAL_J: (kind, instant IST). Checked against the rig's list before anything else.
HAND = [
    ("lan", "2026-12-30 21:00"),
    ("cloud", "2026-12-31 02:00"), ("rollover", "2026-12-31 06:00"), ("lan", "2026-12-31 21:00"),
    ("cloud", "2027-01-01 02:00"), ("rollover", "2027-01-01 06:00"), ("monthly", "2027-01-01 08:30"),
    ("lan", "2027-01-01 21:00"),
    ("cloud", "2027-01-02 02:00"), ("rollover", "2027-01-02 06:00"), ("lan", "2027-01-02 21:00"),
    ("cloud", "2027-01-03 02:00"), ("rollover", "2027-01-03 06:00"), ("audit", "2027-01-03 08:00"),
    ("lan", "2027-01-03 21:00"),
    ("cloud", "2027-01-04 02:00"), ("rollover", "2027-01-04 06:00"), ("weekly", "2027-01-04 08:00"),
]
IDX = {  # position of the events the tests look at, in CAL_J
    "lan_dec30": 0, "cloud_dec31": 1, "roll_dec31": 2, "lan_dec31": 3, "cloud_jan1": 4, "roll_jan1": 5,
    "monthly_jan1": 6, "lan_jan1": 7, "audit_jan3": 13, "weekly_jan4": 17,
}
WAKES_TOTAL = 6          # 5 LAN nights + the Jan 3 audit (see module docstring)
SHUTDOWNS_TOTAL = 6
MAILS_TOTAL = 3          # INFO activation, monthly (Jan 1), weekly (Jan 4)
FAILURE_WORDS = ("FAILURE", "WARNING", "ACTION")

EMPTY = {"runs": [], "subjects": [], "acts": [], "audits": [], "wakes": 0, "shutdowns": 0,
         "argv": {"cloud": [], "lan": [], "audit": []}, "approvals": [], "errors": 0}


def _snap(sim) -> dict:
    return {
        "runs": sim.runs(),
        "acts": sim.acts(),
        "approvals": sim.approvals(),
        "subjects": sim.subjects(),
        "bodies": [_body(m["data"]) for m in sim.srv.messages],
        "audits": sim.sql("SELECT mode, fy, status FROM integrity_audits ORDER BY id"),
        "src_b": sim.tree(sim.src_root / FY_B),
        "nas_b": sim.tree(sim.nas_root / FY_B),
        "wakes": sim.wakes,
        "shutdowns": sim.shutdowns,
        "argv": {leg: copy.deepcopy(sim.argv(leg)) for leg in ("cloud", "lan", "audit")},
        "errors": len(sim.errors),
    }


def _body(raw: str) -> str:
    msg = email.message_from_string(raw)
    texts = []
    for part in msg.walk():
        if part.get_content_maintype() == "text":
            payload = part.get_payload(decode=True) or b""
            texts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(texts)


def _new_rows(before: dict, after: dict) -> list[dict]:
    seen = {r["id"] for r in before["runs"]}
    return [r for r in after["runs"] if r["id"] not in seen]


def _new_subjects(before: dict, after: dict) -> list[str]:
    return after["subjects"][len(before["subjects"]):]


def _count(rows, mode, fy=None, ok_only=True) -> int:
    return sum(1 for r in rows if r["mode"] == mode and (fy is None or r["fy"] == fy)
               and (not ok_only or r["status"] in OK))


def _fy_dest(argv: list[str]) -> str:
    """The destination of a cloud sync call: the one argv element that ends in /FYnn-nn (not the last element)."""
    dests = [a for a in argv if re.search(r"/FY\d\d-\d\d$", a)]
    assert len(dests) == 1, f"expected one destination element in {argv}"
    return dests[0]


@pytest.fixture(scope="module")
def best_jan(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("s1_best_jan")
    with pytest.MonkeyPatch.context() as mp, sim_world(tmp, mp) as sim:
        snaps = []
        for ev in CAL_J:
            sim.play([ev])
            snaps.append(_snap(sim))
        status = sim.status(pendulum.datetime(2027, 1, 4, 9, 0, tz=IST))   # product-view
        snaps.append(_snap(sim))
        yield {"snaps": snaps, "status": status}


def _at(best_jan, name: str) -> dict:
    return best_jan["snaps"][IDX[name]]


def _before(best_jan, name: str) -> dict:
    i = IDX[name]
    return best_jan["snaps"][i - 1] if i else EMPTY


# ── the calendar and the first night ─────────────────────────────────────────────────────────

def test_calendar_is_the_hand_table(best_jan):
    got = [(e.kind, e.at.format("YYYY-MM-DD HH:mm")) for e in CAL_J]
    assert got == HAND


def test_all_events_ran_without_a_flow_error(best_jan):
    assert best_jan["snaps"][-1]["errors"] == 0


def test_dec30_2100_one_lan_row_for_a(best_jan):
    new = _new_rows(_before(best_jan, "lan_dec30"), _at(best_jan, "lan_dec30"))
    assert [(r["mode"], r["fy"]) for r in new] == [("lan", FY_A)]
    assert new[0]["status"] in OK


def test_dec31_0200_one_cloud_row_for_a(best_jan):
    new = _new_rows(_before(best_jan, "cloud_dec31"), _at(best_jan, "cloud_dec31"))
    assert [(r["mode"], r["fy"]) for r in new] == [("cloud", FY_A)]
    assert new[0]["status"] in OK


def test_dec31_0600_rollover_adds_no_row_no_mail_no_activation(best_jan):
    before, after = _before(best_jan, "roll_dec31"), _at(best_jan, "roll_dec31")
    assert [r for r in _new_rows(before, after) if r["mode"] in ("lan", "cloud")] == []
    assert _new_subjects(before, after) == []
    assert after["acts"] == []


def test_dec31_2100_one_lan_row_for_a_and_no_activation(best_jan):
    before, after = _before(best_jan, "lan_dec31"), _at(best_jan, "lan_dec31")
    new = _new_rows(before, after)
    assert [(r["mode"], r["fy"]) for r in new] == [("lan", FY_A)]
    assert new[0]["status"] in OK
    assert after["acts"] == []


# ── 1 January: activation, first cloud night ─────────────────────────────────────────────────

def test_jan1_0200_activates_fy27_28_exactly_once(best_jan):
    acts = _at(best_jan, "cloud_jan1")["acts"]
    assert len(acts) == 1, acts
    fy, created_by_app, activated_at = acts[0]
    assert fy == FY_B
    assert created_by_app == 1            # the folder did not exist: the app created it
    assert activated_at.startswith("2027-01-01T02:00")


def test_jan1_0200_source_folder_holds_only_the_placeholder(best_jan):
    assert set(_at(best_jan, "cloud_jan1")["src_b"]) == {PLACEHOLDER}


def test_jan1_0200_cloud_rows_are_b_then_a(best_jan):
    new = _new_rows(_before(best_jan, "cloud_jan1"), _at(best_jan, "cloud_jan1"))
    assert [(r["mode"], r["fy"]) for r in new] == [("cloud", FY_B), ("cloud", FY_A)]
    assert all(r["status"] in OK for r in new)
    assert all(r["started_at"].startswith("2027-01-01T02:00") for r in new)


def test_jan1_0200_cloud_argv_destinations_end_in_each_fy(best_jan):
    before, after = _before(best_jan, "cloud_jan1"), _at(best_jan, "cloud_jan1")
    new_calls = after["argv"]["cloud"][len(before["argv"]["cloud"]):]
    assert len(new_calls) == 2, new_calls
    dests = [_fy_dest(argv) for argv in new_calls]
    assert dests[0].endswith(f"/{FY_B}") and dests[1].endswith(f"/{FY_A}")


def test_jan1_0200_one_mail_and_it_names_fy27_28(best_jan):
    before, after = _before(best_jan, "cloud_jan1"), _at(best_jan, "cloud_jan1")
    subjects = _new_subjects(before, after)
    assert len(subjects) == 1, subjects
    assert FY_B in subjects[0]


def test_jan1_0830_one_monthly_mail(best_jan):
    before, after = _before(best_jan, "monthly_jan1"), _at(best_jan, "monthly_jan1")
    subjects = _new_subjects(before, after)
    assert len(subjects) == 1, subjects
    assert not any(w in subjects[0].upper() for w in FAILURE_WORDS), subjects


def test_jan1_2100_nas_b_folder_marker_rows_and_one_wake_one_shutdown(best_jan):
    before, after = _before(best_jan, "lan_jan1"), _at(best_jan, "lan_jan1")
    assert MARKER in after["nas_b"], f"no mount marker in NAS {FY_B}: {sorted(after['nas_b'])}"
    new = _new_rows(before, after)
    assert [(r["mode"], r["fy"]) for r in new] == [("lan", FY_B), ("lan", FY_A)]
    assert all(r["status"] in OK for r in new)
    assert after["wakes"] - before["wakes"] == 1
    assert after["shutdowns"] - before["shutdowns"] == 1


def test_jan3_0800_audit_has_rows_for_both_fys(best_jan):
    before, after = _before(best_jan, "audit_jan3"), _at(best_jan, "audit_jan3")
    new = after["audits"][len(before["audits"]):]
    assert {fy for _mode, fy, _status in new} == {FY_A, FY_B}
    assert not [a for a in new if a[2] in ("VERIFICATION_FAILED", "AUDIT_INCOMPLETE")], new


def test_jan4_0800_weekly_mail_body_names_both_fys(best_jan):
    after = _at(best_jan, "weekly_jan4")
    before = _before(best_jan, "weekly_jan4")
    subjects = _new_subjects(before, after)
    assert len(subjects) == 1, subjects
    body = after["bodies"][-1]
    assert FY_A in body and FY_B in body


def test_totals_after_the_calendar(best_jan):
    rows = best_jan["snaps"][-1]["runs"]
    lan_or_cloud = [r for r in rows if r["mode"] in ("lan", "cloud")]
    assert len(lan_or_cloud) == 17
    assert all(r["status"] in OK for r in lan_or_cloud)
    assert _count(rows, "lan", FY_A) == 5
    assert _count(rows, "cloud", FY_A) == 5
    assert _count(rows, "lan", FY_B) == 3
    assert _count(rows, "cloud", FY_B) == 4


def test_wakes_and_shutdowns_after_the_calendar(best_jan):
    last = best_jan["snaps"][-1]
    assert last["wakes"] == WAKES_TOTAL
    assert last["shutdowns"] == SHUTDOWNS_TOTAL


def test_mails_after_the_calendar(best_jan):
    subjects = best_jan["snaps"][-1]["subjects"]
    assert len(subjects) == MAILS_TOTAL, subjects
    assert not [s for s in subjects if any(w in s.upper() for w in FAILURE_WORDS)], subjects


def test_no_approvals_after_the_calendar(best_jan):
    assert best_jan["snaps"][-1]["approvals"] == []


def test_status_shows_two_folders(best_jan):   # product-view
    folders = best_jan["status"]["folders"]
    assert {f["fy"] for f in folders} == {FY_A, FY_B}
    assert len(folders) == 2
