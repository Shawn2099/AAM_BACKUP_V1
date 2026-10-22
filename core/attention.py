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
from loguru import logger

from core import overdue, statuses
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


def _schedule_in_effect(db, config) -> dict | None:
    """The cron strings + timezone the scheduler is actually serving: what serve.py recorded when it
    registered them (a schedule saved in Settings but not yet applied is NOT in effect), else the
    configured ones. None when neither can be read."""
    stored = db.get_schedule_in_effect() if hasattr(db, "get_schedule_in_effect") else None
    if isinstance(stored, dict):
        return stored
    sched = getattr(config, "schedule", None)
    try:
        return {"cloud_cron": str(sched.cloud_cron), "lan_cron": str(sched.lan_cron),
                "timezone": str(sched.timezone)}
    except AttributeError:
        return None


def _later(a: str | None, b: str | None) -> str | None:
    both = [t for t in (a, b) if t]
    return max(both, key=lambda t: pendulum.parse(t).timestamp()) if both else None


def _progressing_partial(run: dict | None) -> bool:
    """The latest run is a budget-stopped PARTIAL that copied something: a large upload spread over
    several nights (first full upload, big change) - slow, not stuck."""
    if not run or run.get("status") not in statuses.PARTIAL_STATUSES:
        return False
    if not (run.get("bytes_copied") or 0) > 0:
        return False
    try:
        metrics = json.loads(run.get("extended_metrics") or "{}")
    except ValueError:
        return False
    budget = metrics.get("budget") if isinstance(metrics, dict) else None
    return isinstance(budget, dict) and bool(budget.get("stopped"))


def _overdue_item(db, config, leg: str, sched: dict, now) -> dict | None:
    """The banner / watchdog item for one enabled leg, or None while it is on time.

    Overdue = now is past  (next scheduled run after the last success started) + (the leg's run
    budget) + dashboard.stale_grace_hours  - see core.overdue. Codes: `stale:<leg>` (red, the only
    one the watchdog emails), `first_run:<leg>` (amber, nothing has run yet but nothing is late),
    `catching_up:<leg>` (amber, a big upload is progressing over several nights)."""
    phrase = _PHRASE.get(leg, leg)
    grace = config.dashboard.stale_grace_hours
    budget_h = getattr(config.budget, f"{leg}_max_hours")
    cron = sched[f"{leg}_cron"]
    tz = sched["timezone"]
    last = db.last_successful_run(leg)
    if last is not None:
        base = last.get("started_at") or last.get("ended_at")
    else:       # never succeeded: count from when the system (or this leg) came into being
        base = _later(db.first_known_at(), db.last_config_change_at(f"{leg}.enabled"))
    # a schedule that changed after that moment starts its own clock: the old plan's run history says
    # nothing about when the NEW plan's first run is due (daily -> weekly must not look a week late)
    base = _later(base, sched.get("since"))
    try:
        due = overdue.next_firing(cron, tz, pendulum.parse(base)) if base else None
        limit = overdue.deadline(cron, tz, pendulum.parse(base), budget_h, grace) if base else None
    except Exception as e:  # noqa: BLE001 - an unreadable time must never hide an overdue leg
        logger.warning(f"Overdue check for the {leg} leg could not read its schedule/time ({e})")
        due = limit = None
        if last is not None or base:
            return _item(RED, f"stale:{leg}",
                         act(f"Check why the {phrase} has not succeeded",
                             "The last success could not be placed on the schedule"))
    if limit is None or now <= limit:
        if last is None:
            when = f" (due {format_ist(due.isoformat())})" if due else ""
            return _item(AMBER, f"first_run:{leg}",
                         act("Nothing to do yet",
                             f"No {phrase} has finished yet; the first scheduled run is still ahead{when}"))
        return None
    if _progressing_partial(db.last_run(leg)):
        return _item(AMBER, f"catching_up:{leg}",
                     act("Nothing to do - leave it running",
                         f"The {phrase} is copying a large amount over several nights and is "
                         "making progress"))
    if last is None:
        return _item(RED, f"stale:{leg}",
                     act(f"Run the {phrase} now from its card, or check why it has not run",
                         "It has never completed successfully"))
    age = _hours_since(last.get("ended_at") or last.get("started_at"), now)
    return _item(RED, f"stale:{leg}",
                 act(f"Check why the {phrase} has not succeeded",
                     f"The last success was {age:.0f} h ago; the next scheduled run was due "
                     f"{format_ist(due.isoformat())} and it was not finished by "
                     f"{format_ist(limit.isoformat())} (next scheduled run + {budget_h:g} h run "
                     f"budget + {grace:g} h grace)"))


def compute_attention(db, config, now=None) -> list[dict]:
    """Ordered list of {level, code, message}; empty when all is well."""
    now = now or pendulum.now(IST)
    items: list[dict] = []
    sched = _schedule_in_effect(db, config)

    for leg in ("cloud", "lan"):
        if not getattr(config, leg).enabled:
            continue
        name = _LEG_NAME[leg]
        item = _overdue_item(db, config, leg, sched, now) if sched else None
        if item:
            items.append(item)

        run = db.last_run(leg)
        if run and statuses.category(run.get("status")) == statuses.CATEGORY_FAILED:
            # D2: a failed night is not "healthy" until 26 h have passed since the last success.
            # A missed night (server off or busy) is a warning; PARTIAL / RUNNING are not alarms.
            level = AMBER if run.get("status") in statuses.MISSED_STATUSES else RED
            when = format_ist(run.get("ended_at") or run.get("started_at"))
            items.append(_item(level, f"last_run:{leg}",
                               act(f"Open the {_PHRASE.get(leg, leg)} card on the dashboard and fix the cause",
                                   f"The last {_PHRASE.get(leg, leg)} did not succeed "
                                   f"({statuses.label(run.get('status'))}, {when})")))
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

    notif = config.notifications
    alerts_to, reports_to = getattr(notif, "recipients", None), getattr(notif, "report_recipients", None)
    if alerts_to is None or reports_to is None:      # a config without the lists: nothing to judge
        pass
    elif not alerts_to and not reports_to:
        items.append(_item(RED, "recipients_none",
                           act("Add an email address in Settings (\"Who gets problem alerts\")",
                               "No email address is set, so nobody receives any alert or report")))
    elif not alerts_to:
        items.append(_item(AMBER, "alerts_list_empty",
                           act("Add an address under \"Who gets problem alerts\" in Settings",
                               "The alerts list is empty, so problem alerts go to the reports list")))

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
