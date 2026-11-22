"""Card S2 BEST-A: the 1 April changeover on a quiet office (docs/PLAN_E2E_SIMULATION_2026-11-23.md, S2).

Setup: seed Jan 5 .. Mar 28 (seed_to_mar28), then play CAL_A (Mar 29 08:00 .. Apr 5 08:00) with every tool
answer OK. Hand table below is written from the card and the plan (PLAN_FY_OVERLAP section 4), never from
the product output. Oracles: raw SQL on run_history / rollover_journal / integrity_audits, the rig's
switch_writes and archive_calls lists, folder hashes, and the SMTP server's own messages.

The golden rollover subjects are the only product-view values (label below).
"""

from __future__ import annotations

import pendulum
import pytest

from core.time_utils import IST
from test_support.sim_calendar import A, B, CAL_A, OK, sim_world

# product-view: the Subjects the SMTP server received during the 1 April 06:00 rollover, recorded once from
# the product and judged by the reviewer against PLAN_FY_OVERLAP section 4 ("1 Apr onwards" row).
GOLDEN_ROLLOVER_SUBJECTS: list[str] = [
    "[AAM Backup] INFO: New financial year FY27-28 started",
]

# Hand table: (mode, started_at prefix of the scheduled night, FYs that must have exactly one row each).
NIGHTS = [
    ("lan", "2027-03-29T21:", {A, B}),
    ("lan", "2027-03-30T21:", {A, B}),
    ("lan", "2027-03-31T21:", {A, B}),
    ("cloud", "2027-03-30T02:", {A, B}),
    ("cloud", "2027-03-31T02:", {A, B}),
    ("cloud", "2027-04-01T02:", {A, B}),
    ("cloud", "2027-04-01T06:", {A}),          # final backup of the closing FY (run_id contains "final")
    ("lan", "2027-04-01T06:", {A}),            # final backup of the closing FY (run_id contains "final")
    ("lan", "2027-04-01T21:", {B}),
    ("cloud", "2027-04-02T02:", {B}),
    ("lan", "2027-04-02T21:", {B}),
    ("cloud", "2027-04-03T02:", {B}),
    ("lan", "2027-04-03T21:", {B}),
    ("cloud", "2027-04-04T02:", {B}),
    ("lan", "2027-04-04T21:", {B}),
    ("cloud", "2027-04-05T02:", {B}),
]
WINDOW_FROM = "2027-03-29T21:00"               # rows of the calendar window (seeded rows are older)
WINDOW_TOTALS = {("lan", A): 4, ("cloud", A): 4, ("lan", B): 7, ("cloud", B): 7}


@pytest.fixture
def sim(tmp_path, monkeypatch):
    with sim_world(tmp_path, monkeypatch) as s:
        yield s


def _rollover_apr1_index() -> int:
    return next(i for i, e in enumerate(CAL_A) if e.kind == "rollover" and (e.at.month, e.at.day) == (4, 1))


def _night(sim, mode: str, prefix: str) -> list[dict]:
    return [r for r in sim.runs() if r["mode"] == mode and r["started_at"].startswith(prefix)]


def _window(sim) -> list[dict]:
    """The backup rows (lan / cloud) of the calendar window; rollover-check rows are not backups."""
    return [r for r in sim.runs() if r["mode"] in ("lan", "cloud") and r["started_at"] >= WINDOW_FROM]


def _journal_steps(sim) -> list[tuple]:
    return sim.sql(
        "SELECT s.step, s.state FROM rollover_journal_steps s "
        "JOIN rollover_journal j ON j.id = s.journal_id WHERE j.from_fy = ? AND j.to_fy = ? ORDER BY s.step",
        A, B)


def test_s2_best_apr_hand_table(sim):
    assert sim.seed_to_mar28() > 0
    src_before = sim.tree(sim.src_root / A)
    nas_before = sim.tree(sim.nas_root / A)
    assert src_before and nas_before, "the FY26-27 folders must hold the seeded files before the changeover"

    cut = _rollover_apr1_index()
    sim.play(CAL_A[:cut + 1])                  # through the 1 April 06:00 rollover
    assert sim.errors == [], sim.errors

    # the 1 April 06:00 rollover: final backup of FY26-27 on both legs, then the switch and the archive
    apr1_rows = _night(sim, "cloud", "2027-04-01T06:") + _night(sim, "lan", "2027-04-01T06:")
    assert sorted((r["mode"], r["fy"]) for r in apr1_rows) == [("cloud", A), ("lan", A)]
    assert all("final" in r["run_id"] for r in apr1_rows), [r["run_id"] for r in apr1_rows]
    assert sim.switch_writes == [(B, A)]
    assert len(sim.archive_calls) == 1 and sim.archive_calls[0]["old_fy"] == A
    steps = _journal_steps(sim)
    assert steps, "the rollover journal row for FY26-27 -> FY27-28 must exist"
    assert all(state in ("done", "skipped") for _, state in steps), steps
    max_a_id = max(r["id"] for r in sim.runs())

    sim.play(CAL_A[cut + 1:])                  # the rest of the calendar, to Apr 5 08:00
    assert sim.errors == [], sim.errors

    # every night of the window, by hand
    for mode, prefix, expected in NIGHTS:
        rows = _night(sim, mode, prefix)
        assert sorted(r["fy"] for r in rows) == sorted(expected), (mode, prefix, [(r["fy"], r["status"]) for r in rows])
        assert all(r["status"] in OK for r in rows), (mode, prefix, [(r["fy"], r["status"]) for r in rows])

    # window totals (hand): A lan 4 / cloud 4; B lan 7 / cloud 7
    window = _window(sim)
    totals = {}
    for r in window:
        totals[(r["mode"], r["fy"])] = totals.get((r["mode"], r["fy"]), 0) + 1
    assert totals == WINDOW_TOTALS, totals
    assert all(r["status"] in OK for r in window), [(r["run_id"], r["status"]) for r in window if r["status"] not in OK]

    # after the 06:00 rollover only FY27-28 is backed up: no new FY26-27 row ever again
    # id-based: the 90-day run-log retention (db_retention_days, purge_old_runs) deletes old rows during the
    # calendar, so a plain count can fall; a row written after the rollover always has a higher id.
    new_a = [r for r in sim.runs() if r["mode"] in ("lan", "cloud") and r["fy"] == A and r["id"] > max_a_id]
    assert new_a == [], [(r["id"], r["mode"], r["started_at"]) for r in new_a]
    late = [r for r in sim.runs() if r["mode"] in ("lan", "cloud") and r["started_at"] > "2027-04-01T06:30"]
    assert len(late) == 8, [(r["mode"], r["started_at"]) for r in late]   # Apr 1 21:00 .. Apr 5 02:00
    assert {r["fy"] for r in late} == {B}

    # the 4 Apr audit: FY27-28 only
    audits = sim.sql("SELECT fy FROM integrity_audits WHERE started_at LIKE '2027-04-04T08:%'")
    assert audits, "the Sunday 4 Apr audit wrote no integrity_audits row"
    assert {fy for (fy,) in audits} == {B}, audits

    # the folders of the closed year did not change
    assert sim.tree(sim.src_root / A) == src_before
    assert sim.tree(sim.nas_root / A) == nas_before

    # mails: no failure or action subject for the whole window
    bad = [s for s in sim.subjects() if "FAILURE" in s or "ACTION" in s]
    assert bad == [], bad

    # dashboard read at Apr 5 09:00 (hand: after the switch ONE folder card, for FY27-28 only)   # product-view
    status = sim.status(pendulum.datetime(2027, 4, 5, 9, 0, tz=IST))
    folders = status["folders"]                                       # product-view
    assert [f["fy"] for f in folders] == [B], folders                 # product-view: one card, FY27-28
    assert A not in str(status.get("attention", [])), status.get("attention")   # product-view: no FY26-27 item


def test_s2_rollover_mail_subjects_golden(sim):
    """product-view: the Subjects of the 1 April rollover mails, frozen once (see GOLDEN_ROLLOVER_SUBJECTS)."""
    sim.seed_to_mar28()
    cut = _rollover_apr1_index()
    sim.play(CAL_A[:cut])                      # everything before the rollover
    assert sim.errors == [], sim.errors
    mark = len(sim.subjects())
    sim.play(CAL_A[cut:cut + 1])               # the 1 April 06:00 rollover only
    assert sim.errors == [], sim.errors
    rollover_subjects = sim.subjects()[mark:]
    print("S2 golden rollover subjects:", rollover_subjects)
    assert rollover_subjects == GOLDEN_ROLLOVER_SUBJECTS
