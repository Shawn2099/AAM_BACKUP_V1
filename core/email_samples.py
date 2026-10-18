"""One realistic sample of every kind of email, for the owner to review (plan G1).

`samples(config)` renders through the real composer (`core.email_compose.compose`), so what the
owner sees is what a real alert looks like. One sample per fault code (every code in
`core.faults` must have one - a unit test checks it), plus a reminder, a RESOLVED and the INFO
notices. The support-bundle attachments are not part of a preview.
"""
from __future__ import annotations

import pendulum

from core import faults
from core.email_compose import compose
from core.notify import AlertEvent
from core.time_utils import IST

# codes that exist in the catalogue but are no longer produced (the owner is never emailed them)
RETIRED = {"AAM-RPT-01"}      # an empty report period is now the weekly report itself (plan C, P1)

_MODE_BY_FAMILY = {"CLD": "cloud", "AUD": "audit", "ROL": "rollover", "SVC": "watchdog",
                   "SYS": "watchdog", "CLK": "rollover", "RPT": "report"}
_KIND_BY_SEVERITY = {faults.ACTION: "ACTION_REQUIRED", faults.PROBLEM: "FAILURE",
                     faults.WARNING: "WARNING"}


def _mode(code: str) -> str:
    return _MODE_BY_FAMILY.get(code.split("-")[1], "lan")


def _fault_event(code: str, now) -> AlertEvent:
    fault = faults.CATALOG[code]
    return AlertEvent(key=f"preview:{code}", kind=_KIND_BY_SEVERITY.get(fault.severity, "FAILURE"),
                      mode=_mode(code), title=fault.title, message="Sample email for review.",
                      status="SAMPLE", fault_code=code, occurred_at=now.isoformat(),
                      run_id="20261018-210000-sample")


def _report_samples(config, now) -> list[dict]:
    """Weekly, monthly and last-30-days reports built from a throwaway database with a typical
    week: LAN failed once and was fixed, the cloud leg was fine."""
    import tempfile
    from pathlib import Path

    from core import report
    from core.manifest import ManifestDB
    from core.report_period import ReportPeriod

    with tempfile.TemporaryDirectory() as tmp:
        db = ManifestDB(Path(tmp) / "preview.db")
        try:
            for d in range(1, 41):
                day = now.subtract(days=d)
                for leg, ok, bad in (("cloud", "CLOUD_COMPLETE", "CLOUD_FAILED"),
                                     ("lan", "LAN_COMPLETE", "LAN_FAILED")):
                    failed = leg == "lan" and d == 9
                    started = day.replace(hour=21 if leg == "lan" else 2, minute=0, second=0)
                    db.insert_run({"run_id": f"preview-{leg}-{d}", "mode": leg,
                                   "started_at": started.isoformat(),
                                   "ended_at": started.add(minutes=40).isoformat(),
                                   "status": bad if failed else ok, "files_copied": 120 + d,
                                   "bytes_copied": (500 + d) * 1024 * 1024,
                                   "duration_seconds": 2400.0})
            legs = tuple(leg for leg in ("cloud", "lan") if getattr(config, leg).enabled) or ("cloud", "lan")
            out = []
            for name, rp, label in (
                    ("weekly report", ReportPeriod.previous_week(now), "Weekly"),
                    ("monthly report", ReportPeriod.previous_month(now), "Monthly"),
                    ("last 30 days (dashboard button)", ReportPeriod.last_days(30, now), "Monthly")):
                subject, body, _att = report.build_summary_email(
                    db, config.firm_name, 7 if label == "Weekly" else 30, label, legs=legs,
                    report_period=rp)
                out.append({"name": name, "subject": subject, "html": body,
                            "text": "(HTML report)"})
            return out
        finally:
            db.close()


def samples(config, db=None, now=None) -> list[dict]:
    """[{"name", "subject", "html", "text"}] for every email kind."""
    now = now or pendulum.now(IST)
    out = []

    def add(name: str, event: AlertEvent) -> None:
        subject, html, text = compose(config, event, db, now)
        out.append({"name": name, "subject": subject, "html": html, "text": text})

    for code in sorted(faults.CATALOG):
        if code not in RETIRED:
            add(code, _fault_event(code, now))
    first = _fault_event("AAM-NAS-01", now)
    first.first_reported_at = now.subtract(hours=26).isoformat()
    add("reminder (NAS still unreachable)", first)
    add("RESOLVED", AlertEvent(key="preview:resolved", kind="RECOVERED", mode="lan",
                               title="NAS back", message="", resolved_code="AAM-NAS-01",
                               occurred_at=now.isoformat()))
    for who in ("Dashboard", "Advanced"):
        add(f"{who} password changed", AlertEvent(
            key=f"preview:password:{who}", kind="INFO", mode="dashboard",
            title=f"{who} password changed",
            message=f"The {who} password of the backup dashboard was changed by 10.10.151.20 "
                    f"(session) at {now.format('D MMM YYYY, h:mm A')}. If this was not you, "
                    "contact support.", occurred_at=now.isoformat()))
    return out + _report_samples(config, now)
