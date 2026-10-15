"""What needs a human's attention right now (plan WP9).

One server-side evaluation used by the dashboard banner (/status) and the
unauthenticated /health endpoint, so both always agree. Email is the only
push channel, so the dashboard must say plainly when something is wrong -
including when the emails themselves are not getting through.

RED   = a human must act (no recent successful backup, a blocked run waiting
        for approval, an audit that found differences, alert emails failing).
AMBER = degraded but not failing (audit could not run, run logs incomplete).
"""

from __future__ import annotations

import json

import pendulum

from core.time_utils import IST

RED = "red"
AMBER = "amber"

_LEG_NAME = {"cloud": "Cloud", "lan": "LAN", "rollover": "FY rollover"}


def _item(level: str, code: str, message: str) -> dict:
    return {"level": level, "code": code, "message": message}


def _hours_since(iso_ts: str, now) -> float:
    return (now - pendulum.parse(iso_ts)).total_seconds() / 3600


def compute_attention(db, config, now=None) -> list[dict]:
    """Ordered list of {level, code, message}; empty when all is well."""
    now = now or pendulum.now(IST)
    items: list[dict] = []
    stale_h = config.dashboard.stale_after_hours

    for leg in ("cloud", "lan"):
        if not getattr(config, leg).enabled:
            continue
        name = _LEG_NAME[leg]
        last = db.last_successful_run(leg)
        if last is None:
            items.append(_item(RED, f"stale:{leg}",
                               f"{name}: no successful backup recorded yet."))
        else:
            when = last.get("ended_at") or last.get("started_at")
            age = _hours_since(when, now)
            if age > stale_h:
                items.append(_item(
                    RED, f"stale:{leg}",
                    f"{name}: last successful backup was {age:.0f} h ago "
                    f"(limit {stale_h} h)."))

        run = db.last_run(leg)
        if run and run.get("extended_metrics"):
            try:
                metrics = json.loads(run["extended_metrics"])
                metrics = metrics if isinstance(metrics, dict) else {}
            except ValueError:
                metrics = {}
            if metrics.get("artefacts_degraded"):
                items.append(_item(AMBER, f"run_logs:{leg}",
                                   f"{name}: the full tool logs of the last run could not be "
                                   "stored completely (check free disk space)."))
            shutdown = metrics.get("nas_shutdown")
            if isinstance(shutdown, dict) and shutdown.get("outcome") != "VERIFIED":
                items.append(_item(AMBER, "nas_shutdown",
                                   "NAS was not confirmed powered off after the last LAN backup - "
                                   f"{shutdown.get('detail') or shutdown.get('outcome')}."))

        audit = db.latest_audit(leg)
        if audit and audit.get("status") == "VERIFICATION_FAILED":
            items.append(_item(RED, f"audit:{leg}",
                               f"{name}: the integrity audit found differences - "
                               f"{audit.get('detail') or 'see the audit detail'}."))
        elif audit and audit.get("status") == "AUDIT_INCOMPLETE":
            items.append(_item(AMBER, f"audit:{leg}",
                               f"{name}: the integrity audit could not run - "
                               f"{audit.get('detail') or 'see the logs'}."))

    for req in db.open_approvals():
        name = _LEG_NAME.get(req["leg"], req["leg"])
        what = "waits for your decision" if req["status"] == "PENDING" else "is approved but not run yet"
        items.append(_item(RED, f"approval:{req['leg']}",
                           f"{name} blocked - {what} (see below)."))

    window = config.dashboard.alert_failure_window_days
    failed = db.notification_failures_since(window)
    if failed:
        latest = failed[0]
        items.append(_item(
            RED, "alerts_undelivered",
            f"{len(failed)} alert/report email(s) could not be delivered in the last {window} "
            f"days (latest: {latest.get('subject') or latest.get('event_key')} - "
            f"{latest.get('error') or 'unknown error'}). Check the email settings."))

    # red first, then amber; stable within a level
    return sorted(items, key=lambda i: 0 if i["level"] == RED else 1)
