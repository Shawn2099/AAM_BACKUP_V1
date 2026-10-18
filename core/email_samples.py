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
    return out
