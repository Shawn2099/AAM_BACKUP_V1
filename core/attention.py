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

from core.time_utils import IST, format_ist

RED = "red"
AMBER = "amber"

_LEG_NAME = {"cloud": "Cloud", "lan": "LAN", "rollover": "FY rollover"}
_PHRASE = {"cloud": "cloud backup", "lan": "LAN backup", "rollover": "FY rollover"}


from core.messages import act  # noqa: E402


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
                               act(f"Run the {_PHRASE.get(leg, leg)} now from its card, or check why it has not run",
                                   "It has never completed successfully")))
        else:
            when = last.get("ended_at") or last.get("started_at")
            age = _hours_since(when, now)
            if age > stale_h:
                items.append(_item(
                    RED, f"stale:{leg}",
                    act(f"Check why the {_PHRASE.get(leg, leg)} has not succeeded",
                        f"The last success was {age:.0f} h ago (limit {stale_h} h)")))

        run = db.last_run(leg)
        if run and run.get("extended_metrics"):
            try:
                metrics = json.loads(run["extended_metrics"])
                metrics = metrics if isinstance(metrics, dict) else {}
            except ValueError:
                metrics = {}
            if metrics.get("artefacts_degraded"):
                items.append(_item(AMBER, f"run_logs:{leg}",
                                   act("Free up disk space on the server",
                                       f"The {_PHRASE.get(leg, leg)} could not store all of its detailed logs")))
            clock = metrics.get("clock")
            if isinstance(clock, dict) and clock.get("state") == "UNVERIFIED":
                items.append(_item(AMBER, "clock",
                                   act("Check the server's date, time and internet access",
                                       "The clock could not be verified on the last cloud backup "
                                       f"({clock.get('reason') or 'no time source answered'})")))
            shutdown = metrics.get("nas_shutdown")
            if isinstance(shutdown, dict) and shutdown.get("outcome") != "VERIFIED":
                items.append(_item(AMBER, "nas_shutdown",
                                   act("Check that the NAS has switched off",
                                       "It was not confirmed off after the last LAN backup "
                                       f"({shutdown.get('detail') or shutdown.get('outcome')})")))

        audit = db.latest_audit(leg)
        if audit and audit.get("status") == "VERIFICATION_FAILED":
            newer = audit.get("newer_incomplete_at")   # A1: a later check that could not run
            newer_note = f" The newer check on {format_ist(newer)} could not run." if newer else ""
            items.append(_item(RED, f"audit:{leg}",
                               act(f"Open the {_PHRASE.get(leg, leg)} details, fix the cause, then press Check now",
                                   "The safety check found files that do not match "
                                   f"({audit.get('detail') or 'see the audit detail'})." + newer_note)))
        elif audit and audit.get("status") == "PENDING_BACKUP":
            items.append(_item(AMBER, f"audit:{leg}",
                               act("Nothing to do",
                                   f"Newer files will be copied by the next {_PHRASE.get(leg, leg)}",
                                   audit.get("detail") or "")))
        elif audit and audit.get("status") == "AUDIT_INCOMPLETE":
            items.append(_item(AMBER, f"audit:{leg}",
                               act("Run the safety check again later with Check now",
                                   f"The safety check of the {_PHRASE.get(leg, leg)} could not run "
                                   f"({audit.get('detail') or 'see the logs'})")))

    for req in db.open_approvals():
        name = _LEG_NAME.get(req["leg"], req["leg"])
        what = "waits for your decision" if req["status"] == "PENDING" else "is approved but not run yet"
        items.append(_item(RED, f"approval:{req['leg']}",
                           act("Review the decision below", f"The {_PHRASE.get(req['leg'], req['leg'])} is blocked: {what}")))

    window = config.dashboard.alert_failure_window_days
    # WP13: a watchdog CRITICAL stays red until its RECOVERED notice follows
    watchdog = db.latest_notification_for_mode("watchdog", window)
    if watchdog and watchdog.get("kind") == "ACTION_REQUIRED":
        items.append(_item(RED, "watchdog",
                           act("Ask support to check the server",
                               "The service watchdog reported: "
                               f"{watchdog.get('subject') or watchdog.get('event_key')} "
                               f"({format_ist(watchdog.get('created_at'))})")))

    from core.notify import dashboard_link_problem   # local: notify imports time_utils only

    link_problem = dashboard_link_problem(config)
    if link_problem:
        items.append(_item(AMBER, "email_link",
                           act("Ask support to set the dashboard address in Settings",
                               f"Emails cannot link to this dashboard: {link_problem}")))

    smtp_pw = getattr(getattr(config, "notifications", None), "smtp_password", "")
    if getattr(smtp_pw, "undecryptable", False):
        items.append(_item(RED, "smtp_password_undecryptable",
                           act("Enter the mail password again in Settings",
                               "The saved one cannot be decrypted on this server (copied from "
                               "another machine?), so alert emails cannot be sent")))
    if smtp_pw and not getattr(smtp_pw, "protected", True):
        from core import secrets as secrets_store
        if secrets_store.available():
            items.append(_item(AMBER, "smtp_password_plain",
                               act("Save the mail password again in Settings",
                                   "It is stored as plain text in config.yaml; saving stores it "
                                   "encrypted")))

    changed = db.last_config_change_at("notifications.")
    if changed:
        tested = db.last_sent_at("test")
        if not tested or pendulum.parse(tested) < pendulum.parse(changed):
            items.append(_item(AMBER, "email_untested",
                               act('Press "Send test email" in Settings',
                                   f"The email settings were changed on {format_ist(changed)} "
                                   "and not tested yet")))

    if not config.notifications.send_on_failure:
        items.append(_item(AMBER, "alerts_off",
                           act("Switch failure emails back on in Settings > Advanced",
                               "They are off. Decisions, overdue backups and safety-check "
                               "differences are still emailed")))

    failed = db.notification_failures_since(window)
    if failed:
        latest = failed[0]
        items.append(_item(
            RED, "alerts_undelivered",
            act("Check the email settings",
                f"{len(failed)} alert/report email(s) could not be delivered in the last {window} "
                f"days (latest: {latest.get('subject') or latest.get('event_key')} - "
                f"{latest.get('error') or 'unknown error'})")))

    # red first, then amber; stable within a level
    return sorted(items, key=lambda i: 0 if i["level"] == RED else 1)
