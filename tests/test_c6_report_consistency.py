"""Plan C6: the email body, the download, the CSV and the subject all come from one ReportModel,
so for the same period they report the same runs and counts."""
import csv
import io

import pendulum
import pytest

from core import report
from core.manifest import ManifestDB
from core.report_period import ReportPeriod
from core.time_utils import IST

WEEK = ReportPeriod.previous_week(pendulum.datetime(2026, 10, 19, 8, 0, tz=IST))


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    for i, (mode, status) in enumerate([("cloud", "CLOUD_COMPLETE"), ("lan", "LAN_COMPLETE"),
                                        ("cloud", "CLOUD_NO_CHANGES_COMPLETE"), ("lan", "LAN_FAILED"),
                                        ("rollover", "ROLLOVER_COMPLETE")]):
        t = f"2026-10-{13 + i}T23:00:00+05:30"
        d.insert_run({"run_id": f"r{i}", "mode": mode, "started_at": t, "ended_at": t,
                      "status": status, "files_copied": 10 + i, "bytes_copied": 100 * (i + 1)})
    yield d
    d.close()


def test_email_download_csv_and_model_agree(db):
    legs = ("cloud", "lan")
    model = report.build_report_model(db, WEEK, legs)
    subject, body, att = report.build_summary_email(db, "Firm", 7, "Weekly", legs=legs,
                                                    report_period=WEEK)
    download = report.generate_report_html(db, "Firm", 7, "Weekly", legs=legs, report_period=WEEK)
    rows = list(csv.reader(io.StringIO(att[0]["content"].decode())))[1:]

    assert len(rows) == model["totals"]["total"] == 4              # rollover is not a backup
    assert f"Showing all {len(rows)} backups of the period" in body
    assert f"Showing all {len(rows)} backups of the period" in download
    assert sorted(r[2] for r in rows) == sorted(r["mode"] for r in model["runs"])
    assert model["headline"] in subject and WEEK.label in subject
    assert body.split("A complete CSV")[0].count("runs OK") == len(legs)
    assert model["per_leg"]["cloud"]["files"] == 10 + 12            # per leg, never summed together
    assert f"{model['per_leg']['lan']['files']:,}" in download


def test_late_scheduled_report_covers_the_period_of_the_scheduled_time(tmp_path, monkeypatch):
    """G-9: the weekly flow was scheduled Mon 19 Oct 08:00 but ran a week late (agent down); the
    report must still be for 12-18 Oct, not the week before 'now'."""
    from types import SimpleNamespace
    from unittest.mock import patch

    import flow
    from tests.test_m1_notify import _cfg
    from test_support.smtp_server import smtp_server

    scheduled = pendulum.datetime(2026, 10, 19, 8, 0, tz=IST)
    run = SimpleNamespace(auto_scheduled=True, expected_start_time=scheduled)
    with smtp_server() as srv, \
            patch("prefect.context.FlowRunContext.get", return_value=SimpleNamespace(flow_run=run)), \
            patch("flow.pendulum.now", return_value=pendulum.datetime(2026, 10, 26, 9, 0, tz=IST)):
        flow._send_summary_report(_cfg(tmp_path, port=srv.server_address[1]), days=7, period="Weekly")
    assert "12-18 Oct 2026" in srv.messages[0]["data"].replace("\r\n ", " ") or \
        "12-18 Oct 2026" in srv.messages[0]["data"]
