"""Card U20 (agent_runs/TEST_DESIGN_RELEASE.md): a monthly report counts every unfinished night as a problem.

October 2026 on the LAN leg: 20 COMPLETE nights, then 3 LAN_ABANDONED, 2 LAN_MISSED and 1
LAN_SLOT_TIMEOUT. The problems come after the last good night, so none of them is resolved.

Hand oracle (written from the status definitions, not from core/statuses):
  successes         = 20 (COMPLETE nights)
  problem count     = 6  (3 + 2 + 1; ABANDONED, MISSED and SLOT_TIMEOUT are failures, MISSED
                          being the "warning" kind of failure)
  labels in the run table: "Abandoned" x3, "Missed" x2, "Could not start" x1
  overall verdict   = action needed, never "all backups OK"
"""

from __future__ import annotations

import pendulum

from core import report as rpt
from core.manifest import ManifestDB
from core.report_period import ReportPeriod
from core.time_utils import IST


def _iso(day: int, hour: int = 21) -> str:
    return pendulum.datetime(2026, 10, day, hour, 0, tz=IST).isoformat()


def _insert(db: ManifestDB, n: int, status: str, day: int, files: int = 0) -> None:
    started = _iso(day)
    db.insert_run({"run_id": f"lan-{status.lower()}-{n}", "mode": "lan", "fy": "FY26-27",
                   "started_at": started, "ended_at": _iso(day, 22), "status": status,
                   "files_copied": files, "exit_code": 0 if status == "LAN_COMPLETE" else 1})


def _month(tmp_path) -> ManifestDB:
    db = ManifestDB(str(tmp_path / "m.db"))
    for i in range(20):                                    # October 1..20
        _insert(db, i, "LAN_COMPLETE", day=i + 1, files=7)
    for i, day in enumerate((21, 22, 23)):
        _insert(db, i, "LAN_ABANDONED", day=day)
    for i, day in enumerate((24, 25)):
        _insert(db, i, "LAN_MISSED", day=day)
    _insert(db, 0, "LAN_SLOT_TIMEOUT", day=26)
    return db


def _october() -> ReportPeriod:
    return ReportPeriod.previous_month(pendulum.datetime(2026, 11, 2, 9, 0, tz=IST))


def test_monthly_report_counts_abandoned_missed_and_slot_timeout_as_problems(tmp_path):
    db = _month(tmp_path)
    try:
        rp = _october()
        model = rpt.build_report_model(db, rp, legs=("lan",))
    finally:
        db.close()

    totals = model["totals"]
    assert totals["total"] == 26
    assert totals["successes"] == 20
    assert totals["failures"] == 6
    assert sum(len(s["problems"]) for s in model["summaries"]) == 6
    (lan,) = model["summaries"]
    assert lan["ok"] == 20 and lan["total"] == 26
    assert {p["title"] for p in lan["problems"]} == {"Abandoned", "Missed", "Could not start"}
    assert all(p["resolved"] is False for p in lan["problems"])
    assert model["act"] is True
    assert "all backups OK" not in model["headline"]


def test_monthly_report_lists_each_status_label_and_says_action_needed(tmp_path):
    db = _month(tmp_path)
    try:
        rp = _october()
        model = rpt.build_report_model(db, rp, legs=("lan",))
        html = rpt.render_report_html(model, "AAM", name="Monthly")
        subject, _body, _att = rpt.build_summary_email(db, "AAM", 30, "Monthly",
                                                       report_period=rp, legs=("lan",))
    finally:
        db.close()

    # one table cell per run with its label: 3 Abandoned, 2 Missed, 1 Could not start
    assert html.count("<td>Abandoned</td>") == 3
    assert html.count("<td>Missed</td>") == 2
    assert html.count("<td>Could not start</td>") == 1
    assert html.count("<td>Completed</td>") == 20
    assert "action needed" in subject.lower()
    assert "all backups OK" not in subject
