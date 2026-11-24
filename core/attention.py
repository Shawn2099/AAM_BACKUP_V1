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

from core import fy_overlap, fy_rollover, overdue, statuses
from core.time_utils import IST, format_ist, get_fy_prefix

RED = "red"
AMBER = "amber"

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
    try:
        stored = db.get_schedule_in_effect() if hasattr(db, "get_schedule_in_effect") else None
    except Exception as e:  # noqa: BLE001 - a database hiccup must not stop the overdue check
        logger.warning(f"The schedule in effect could not be read ({e}); using the configured one")
        stored = None
    if isinstance(stored, dict):
        return stored
    sched = getattr(config, "schedule", None)
    try:
        return {"cloud_cron": str(sched.cloud_cron), "lan_cron": str(sched.lan_cron),
                "timezone": str(sched.timezone)}
    except AttributeError:
        return None


def leg_scheduled(db, config, leg: str) -> bool:
    """Does the scheduler in effect run this leg on its cron (B9 Q6, shared with C2)? The flag the scheduler
    recorded wins (absent = scheduled); without a record the configured `<leg>.scheduled` (default true)."""
    sched = _schedule_in_effect(db, config)
    if isinstance(sched, dict) and f"{leg}_scheduled" in sched:
        return sched[f"{leg}_scheduled"] is not False
    return getattr(getattr(config, leg, None), "scheduled", True) is not False


def _later(a: str | None, b: str | None) -> str | None:
    both = [t for t in (a, b) if t]
    return max(both, key=lambda t: pendulum.parse(t).timestamp()) if both else None


def _fy_kw(fy: str | None) -> dict:
    """The `fy=` keyword of a database query: none for the configured FY, so its calls stay the baseline calls."""
    return {} if fy is None else {"fy": fy}


def _code(kind: str, leg: str, fy: str | None) -> str:
    """`<kind>:<leg>` for the configured FY, `<kind>:<leg>:<FY>` for a next-FY pass (B7)."""
    return f"{kind}:{leg}:{fy}" if fy else f"{kind}:{leg}"


def _phrase(leg: str, name: str | None) -> str:
    base = _PHRASE.get(leg, leg)
    return f"{base} of {name}" if name else base


def _configured(config) -> str | None:
    """The configured FY (the source folder's FY), or None when the config has no folder paths."""
    paths = getattr(config, "paths", None)
    source = getattr(paths, "source_drive", None)
    if not isinstance(source, str):
        return None
    return fy_rollover.configured_fy(source, getattr(paths, "lan_destination", None))


def _before_switch(db, configured: str, row: dict | None) -> bool:
    """An unsuffixed row that started before the switch of the overlap belongs to the previous FY (B7 Q10)."""
    if not row or fy_overlap.fy_of_id(row.get("run_id") or row.get("audit_id")):
        return False
    rec = db.overlap_record(configured)
    ended = rec.get("ended_at") if isinstance(rec, dict) else None
    started = row.get("started_at") or row.get("ended_at")
    if not ended or not started:
        return False
    return pendulum.parse(started) < pendulum.parse(ended)


_READ = object()      # "look the active next FY up yourself"


def _text_fy(db, config, row: dict | None, fy: str | None, active=_READ) -> str | None:
    """The FY name a text carries (B7 Q4, Q10). A next-FY pass names its own FY. A configured-FY text names the
    configured FY while the overlap is active, and the real previous FY when its row started before the switch.
    None keeps the baseline text. A database error here only leaves the text baseline (the overlap:unknown
    item reports the unreadable state)."""
    if fy is not None:
        return fy
    configured = _configured(config)
    if configured is None:
        return None
    try:
        if _before_switch(db, configured, row):
            return fy_overlap.prev_fy(configured)
        if (fy_overlap.active_next_fy(config, db) if active is _READ else active) is not None:
            return configured
    except Exception as e:  # noqa: BLE001 - a name in a text is never worth a crash
        logger.warning(f"The FY name of a banner text could not be read ({e})")
    return None


def _progressing_partial(db, config, leg: str, last: dict | None, now, fy: str | None = None) -> bool:
    """The latest run is a budget-stopped PARTIAL that copied something: a large upload spread over
    several nights (first full upload, big change) - slow, not stuck. Only for
    `dashboard.catching_up_max_days` after the first partial run following the last success: an
    upload that never finishes is a problem, not "catching up" forever. `fy`: one next-FY pass (B7)."""
    run = db.last_run(leg, **_fy_kw(fy))
    if not run or run.get("status") not in statuses.PARTIAL_STATUSES:
        return False
    if not (run.get("bytes_copied") or 0) > 0:
        return False
    try:
        metrics = json.loads(run.get("extended_metrics") or "{}")
    except ValueError:
        return False
    budget = metrics.get("budget") if isinstance(metrics, dict) else None
    if not (isinstance(budget, dict) and budget.get("stopped")):
        return False
    from models.config import DashboardConfig, model_default

    max_days = getattr(config.dashboard, "catching_up_max_days",
                       model_default(DashboardConfig, "catching_up_max_days"))
    since = (last.get("started_at") or last.get("ended_at")) if last else None
    first = db.first_partial_after(leg, since, **_fy_kw(fy))
    if not first:
        return True
    return (now.timestamp() - pendulum.parse(first).timestamp()) <= max_days * 86400


def _activation_moment(activated_at: str | None) -> str:
    """The activation time of a next FY, checked: a record that cannot be placed on the calendar raises."""
    try:
        pendulum.parse(activated_at)
    except Exception as e:  # noqa: BLE001 - any unreadable value is the same fail-closed case
        raise ValueError(f"the activation time {activated_at!r} cannot be read") from e
    return activated_at


def _leg_clock(db, config, leg: str, sched: dict, fy: str | None = None, activated_at: str | None = None):
    """(last success or None, next scheduled run, overdue-at) for one leg - the one place that applies
    the rule  overdue-at = next scheduled run after the last success started + run budget + grace.
    The two times are None when no starting point is known or the time cannot be read.
    `fy` = one next-FY pass (B7): its own successes, and without one its clock starts at `activated_at`
    (ValueError when that is unreadable - the caller fails closed)."""
    grace = config.dashboard.stale_grace_hours
    budget_h = getattr(config.budget, f"{leg}_max_hours")
    cron, tz = sched[f"{leg}_cron"], sched["timezone"]
    last = db.last_successful_run(leg, **_fy_kw(fy))
    if last is not None:
        base = last.get("started_at") or last.get("ended_at")
    elif fy is None:       # never succeeded: count from when the system (or this leg) came into being
        base = _later(db.first_known_at(), db.last_config_change_at(f"{leg}.enabled"))
    else:                  # the next FY never succeeded: count from its activation (plan 2.5, B7 Q5)
        base = _later(_activation_moment(activated_at), db.last_config_change_at(f"{leg}.enabled"))
    # a schedule that changed after that moment starts its own clock: the old plan's run history says
    # nothing about when the NEW plan's first run is due (daily -> weekly must not look a week late)
    base = _later(base, sched.get(f"{leg}_since"))
    if not base:
        return last, None, None
    parsed = pendulum.parse(base)
    return (last, overdue.next_firing(cron, tz, parsed),
            overdue.deadline(cron, tz, parsed, budget_h, grace))


def leg_timing(db, config, leg: str, now=None, *, fy: str | None = None) -> dict:
    """Where one enabled leg stands against its schedule, for the dashboard card:
    state  on_time | late (its run should have finished, grace left) | overdue | waiting (nothing has
           run yet, nothing is late) | catching_up (a large upload is progressing over several nights)
    due_at / overdue_at  ISO times (None when unknown). Same rule as the banner (`_overdue_item`).
    `fy` = one next-FY pass of an active overlap (B7): its own runs, its clock from its activation."""
    now = now or pendulum.now(IST)
    sched = _schedule_in_effect(db, config)
    if not sched:
        return {"state": "on_time", "due_at": None, "overdue_at": None}
    try:
        activated_at = None
        if fy is not None:
            active = fy_overlap.active_next_fy(config, db)
            activated_at = active[1].get("activated_at") if active and active[0] == fy else None
        last, due, limit = _leg_clock(db, config, leg, sched, fy=fy, activated_at=activated_at)
    except Exception as e:  # noqa: BLE001 - the card must still render
        logger.warning(f"Timing of the {leg} leg could not be read ({e})")
        # a next-FY pass whose clock cannot be read is RED on the banner (stale:<leg>:<FY>): the card says overdue too
        return {"state": "on_time" if fy is None else "overdue", "due_at": None, "overdue_at": None}
    out = {"due_at": due.isoformat() if due else None,
           "overdue_at": limit.isoformat() if limit else None}
    if limit is None or now <= limit:
        if last is None:
            return {"state": "waiting", **out}
        budget_h = getattr(config.budget, f"{leg}_max_hours")
        finished_by = due.timestamp() + float(budget_h) * 3600       # real elapsed time, DST-proof
        return {"state": "late" if now.timestamp() > finished_by else "on_time", **out}
    if _progressing_partial(db, config, leg, last, now, fy=fy):
        return {"state": "catching_up", **out}
    return {"state": "overdue", **out}


def _overdue_item(db, config, leg: str, sched: dict, now, fy: str | None = None,
                  activated_at: str | None = None, active=_READ) -> dict | None:
    """The banner / watchdog item for one enabled leg, or None while it is on time.

    Overdue = now is past  (next scheduled run after the last success started) + (the leg's run
    budget) + dashboard.stale_grace_hours  - see core.overdue. Codes: `stale:<leg>` (red, the only
    one the watchdog emails), `first_run:<leg>` (amber, nothing has run yet but nothing is late),
    `catching_up:<leg>` (amber, a big upload is progressing over several nights). `fy` = one next-FY
    pass (B7): the codes gain `:<FY>`, the clock starts at `activated_at`."""
    grace = config.dashboard.stale_grace_hours
    budget_h = getattr(config.budget, f"{leg}_max_hours")
    try:
        last, due, limit = _leg_clock(db, config, leg, sched, fy=fy, activated_at=activated_at)
    except Exception as e:  # noqa: BLE001 - an unreadable time must never hide an overdue leg
        logger.warning(f"Overdue check for the {leg} leg could not read its schedule/time ({e})")
        phrase = _phrase(leg, _text_fy(db, config, None, fy, active))
        return _item(RED, _code("stale", leg, fy),
                     act(f"Check why the {phrase} has not succeeded",
                         "The last success could not be placed on the schedule"))
    phrase = _phrase(leg, _text_fy(db, config, last, fy, active))
    if limit is None or now <= limit:
        if last is None:
            when = f" (due {format_ist(due.isoformat())})" if due else ""
            return _item(AMBER, _code("first_run", leg, fy),
                         act("Nothing to do yet",
                             f"No {phrase} has finished yet; the first scheduled run is still ahead{when}"))
        return None
    if _progressing_partial(db, config, leg, last, now, fy=fy):
        return _item(AMBER, _code("catching_up", leg, fy),
                     act("Nothing to do - leave it running",
                         f"The {phrase} is copying a large amount over several nights and is "
                         "making progress"))
    # the most recent run that was owed: a failure mail created after it already told the owner about
    # the CURRENT state (an older mail says nothing about a scheduler that has stopped since)
    owed = overdue.latest_owed(sched[f"{leg}_cron"], sched["timezone"], now, budget_h).isoformat()
    if last is None:
        return {**_item(RED, _code("stale", leg, fy),
                        act(f"Run the {phrase} now from its card, or check why it has not run",
                            "It has never completed successfully")),
                "due_at": owed}
    age = _hours_since(last.get("ended_at") or last.get("started_at"), now)
    return {**_item(RED, _code("stale", leg, fy),
                 act(f"Check why the {phrase} has not succeeded",
                     f"The last success was {age:.0f} h ago; the next scheduled run was due "
                     f"{format_ist(due.isoformat())} and it was not finished by "
                     f"{format_ist(limit.isoformat())} (next scheduled run + {budget_h:g} h run "
                     f"budget + {grace:g} h grace)")),
            "due_at": owed}


def _leg_items(db, config, leg: str, sched: dict | None, now, fy: str | None = None,
               activated_at: str | None = None, active=_READ) -> list[dict]:
    """Every banner item of one leg for one FY pass (B7 K5). `fy` None is the configured FY: the baseline items,
    unchanged. `fy` set: that next FY's own items with `<kind>:<leg>:<FY>` codes. `clock` and `nas_shutdown` are
    never suffixed; the caller de-duplicates them across the passes."""
    out: list[dict] = []
    if sched:
        item = _overdue_item(db, config, leg, sched, now, fy=fy, activated_at=activated_at, active=active)
    else:       # cannot even tell when it should run: say so (red + the watchdog mails it)
        item = _item(RED, _code("stale", leg, fy),
                     act("Ask support to check the schedule settings",
                         f"The {_PHRASE.get(leg, leg)} schedule cannot be read, so it cannot be "
                         "checked whether the backup is on time"))
    if item:
        out.append(item)

    run = db.last_run(leg, **_fy_kw(fy))
    if run and statuses.category(run.get("status")) == statuses.CATEGORY_FAILED:
        # D2: a failed night is not "healthy" until the leg is overdue (next scheduled run + run budget + grace).
        # A missed night (server off or busy) is a warning; PARTIAL / RUNNING are not alarms.
        level = AMBER if run.get("status") in statuses.MISSED_STATUSES else RED
        phrase = _phrase(leg, _text_fy(db, config, run, fy, active))
        when = format_ist(run.get("ended_at") or run.get("started_at"))
        out.append(_item(level, _code("last_run", leg, fy),
                         act(f"Open the {phrase} card on the dashboard and fix the cause",
                             f"The last {phrase} did not succeed "
                             f"({statuses.label(run.get('status'))}, {when})")))
    if run and run.get("extended_metrics"):
        try:
            metrics = json.loads(run["extended_metrics"])
            metrics = metrics if isinstance(metrics, dict) else {}
        except ValueError:
            metrics = {}
        if metrics.get("artefacts_degraded"):
            phrase = _phrase(leg, _text_fy(db, config, run, fy, active))
            out.append(_item(AMBER, _code("run_logs", leg, fy),
                             act("Free up disk space on the server",
                                 f"The {phrase} could not store all of its detailed logs")))
        clock = metrics.get("clock")
        if isinstance(clock, dict) and clock.get("state") == "UNVERIFIED":
            out.append(_item(AMBER, "clock",
                             act("Check the server's date, time and internet access",
                                 "The clock could not be verified on the last cloud backup "
                                 f"({clock.get('reason') or 'no time source answered'})")))
        shutdown = metrics.get("nas_shutdown")
        if isinstance(shutdown, dict) and shutdown.get("outcome") != "VERIFIED":
            out.append(_item(AMBER, "nas_shutdown",
                             act("Check that the NAS has switched off",
                                 "It was not confirmed off after the last LAN backup "
                                 f"({shutdown.get('detail') or shutdown.get('outcome')})")))

    audit = db.latest_audit(leg, **_fy_kw(fy))
    phrase = _phrase(leg, _text_fy(db, config, audit, fy, active)) if audit else _PHRASE.get(leg, leg)
    if audit and audit.get("status") == "VERIFICATION_FAILED":
        newer = audit.get("newer_incomplete_at")   # A1: a later check that could not run
        newer_note = f" The newer check on {format_ist(newer)} could not run." if newer else ""
        out.append(_item(RED, _code("audit", leg, fy),
                         act(f"Open the {phrase} details, fix the cause, then press Check now",
                             "The safety check found files that do not match "
                             f"({audit.get('detail') or 'see the audit detail'})." + newer_note)))
    elif audit and audit.get("status") == "PENDING_BACKUP":
        out.append(_item(AMBER, _code("audit", leg, fy),
                         act("Nothing to do",
                             f"Newer files will be copied by the next {phrase}",
                             audit.get("detail") or "")))
    elif audit and audit.get("status") == "AUDIT_INCOMPLETE":
        out.append(_item(AMBER, _code("audit", leg, fy),
                         act("Run the safety check again later with Check now",
                             f"The safety check of the {phrase} could not run "
                             f"({audit.get('detail') or 'see the logs'})")))
    return out


def _started_ts(row: dict | None) -> float:
    stamp = (row.get("started_at") or row.get("ended_at")) if row else None
    return pendulum.parse(stamp).timestamp() if stamp else float("-inf")


def _settle_repeats(db, leg: str, nxt: str, own: list[dict], ahead: list[dict]) -> tuple[list[dict], list[dict]]:
    """`clock` / `nas_shutdown` are one fact of a leg, read from the last run of each pass (B7 Q6): when both
    passes show it, the item of the newer run is kept and the other one is dropped."""
    shared = {i["code"] for i in own} & {i["code"] for i in ahead} & {"clock", "nas_shutdown"}
    if not shared:
        return own, ahead
    if _started_ts(db.last_run(leg, fy=nxt)) > _started_ts(db.last_run(leg)):
        return [i for i in own if i["code"] not in shared], ahead
    return own, [i for i in ahead if i["code"] not in shared]


def _next_of(config) -> str | None:
    configured = _configured(config)
    return fy_overlap.next_fy(configured) if configured else None


def _activation_item(db, config, now) -> dict | None:
    """The next FY is not added yet (B7 I10, Q2). AMBER on its activation day once the daily check has mailed
    `rollover:activation:<nxt>`; RED from the next day, mailed or not (the check did not run = a fact). A fact,
    never a blocker: no approval, no backup waits for it (plan E1). The mail body is not stored, so the reason is
    the mail's time (the mail itself gives the reason)."""
    rollover = getattr(config, "rollover", None)
    configured = _configured(config)
    activate_on_text = getattr(rollover, "overlap_activate_on", None)
    if getattr(rollover, "overlap_enabled", None) is not True or configured is None \
            or not isinstance(activate_on_text, str):
        return None
    today = pendulum.instance(now).in_timezone(IST).date()
    pending = fy_overlap.activation_pending(config, db, today)
    if pending is None:
        return None                       # not due, already added, or the clock is behind: the rollover check alerts
    nxt, activate_on = pending
    mail = db.last_notification(f"rollover:activation:{nxt}")
    mailed = mail is not None and pendulum.parse(mail["created_at"]).in_timezone(IST).date() >= activate_on
    if today == activate_on and not mailed:
        return None                       # the daily check has not tried yet today
    if mailed:
        reason = f"the daily check mailed at {format_ist(mail['created_at'])}; the mail gives the reason"
    else:
        reason = f"the daily check has not run since {activate_on.strftime('%d %b %Y')}"
    return _item(AMBER if today == activate_on else RED, f"rollover:activation:{nxt}",
                 act("Press 'Run FY rollover now' on the FY rollover card to try at once",
                     f"{nxt} is not added to the backups yet ({reason}). Backups of {configured} go on as usual; "
                     "nothing waits for a backup or an approval. The check retries every morning"))


def _overlap_off_item(db, config) -> dict | None:
    """The overlap was switched off while files wait in the next FY's folder (B7 I9, Q3): those files are not
    backed up. Only while the record is open; a folder with the placeholder alone is no item."""
    rollover = getattr(config, "rollover", None)
    configured = _configured(config)
    if getattr(rollover, "overlap_enabled", None) is not False or configured is None:
        return None
    nxt = fy_overlap.next_fy(configured)
    record = db.overlap_record(nxt)
    if not (isinstance(record, dict) and not record.get("ended_at")):
        return None
    if not fy_rollover.new_fy_has_files(config.paths.source_drive, nxt,
                                        getattr(rollover, "source_placeholder_name", None)):
        return None
    return _item(RED, f"overlap_off:{nxt}",
                 act("Switch 'back up the next financial year' on again in Settings",
                     f"Files saved in {nxt} are no longer backed up"))


def row_fy_label(db, config, row: dict | None) -> str | None:
    """The FY a shown row belongs to, for the dashboard labels (B9 K4, Q5). None while no overlap ever happened
    for the configured FY (baseline: no labels)."""
    configured = _configured(config)
    if configured is None or not row:
        return None
    if fy_overlap.active_next_fy(config, db) is None and not isinstance(db.overlap_record(configured), dict):
        return None
    own = fy_overlap.row_fy(row)
    if own:
        return own
    if _before_switch(db, configured, row):
        return fy_overlap.prev_fy(configured)
    return configured


def _unknown_item() -> dict:
    return _item(RED, "overlap:unknown",
                 act("Check the database on the server, then open the FY rollover card",
                     "The next financial year's backup state could not be read - it may not be backed up"))


def compute_attention(db, config, now=None) -> list[dict]:
    """Ordered list of {level, code, message}; empty when all is well."""
    now = now or pendulum.now(IST)
    items: list[dict] = []
    sched = _schedule_in_effect(db, config)
    try:       # B7: the next FY of an active overlap (its own items below); an unreadable state is said, not hidden
        active = fy_overlap.active_next_fy(config, db)
        state_known = True
    except Exception as e:  # noqa: BLE001 - the banner still renders
        logger.warning(f"The next financial year's overlap state could not be read ({e})")
        active, state_known = None, False
        items.append(_unknown_item())
    nxt = active[0] if active else None
    activated_at = active[1].get("activated_at") if active else None

    for leg in ("cloud", "lan"):
        if not getattr(config, leg).enabled:
            continue
        own = _leg_items(db, config, leg, sched, now, active=active)
        if nxt is None:
            items.extend(own)
            continue
        ahead = _leg_items(db, config, leg, sched, now, fy=nxt, activated_at=activated_at, active=active)
        own, ahead = _settle_repeats(db, leg, nxt, own, ahead)
        items.extend(own + ahead)

    for req in db.open_approvals():
        fy = req.get("fy")
        named = fy if nxt is not None and fy == nxt else None      # B7 Q12: the next FY's request has its own code
        what = "waits for your decision" if req["status"] == "PENDING" else "is approved but not run yet"
        items.append(_item(RED, _code("approval", req["leg"], named),
                           act("Review the decision below",
                               f"The {_phrase(req['leg'], named)} is blocked: {what}")))

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

    from core import fy_rollover

    try:                       # plan FY-6: files saved in the next year's folder before the switch
        found = fy_rollover.unprotected_new_fy(config, db, count=False)
    except Exception as e:  # noqa: BLE001 - the dashboard still renders
        logger.warning(f"Could not check the new financial year's folder: {e}")
        found = None
    if found:
        items.append(_item(RED, f"rollover:unprotected:{found['to_fy']}",
                           act("Open the FY rollover card and finish the switch (or approve it)",
                               fy_rollover.unprotected_message(found))))
    unprotected_next = bool(found) and found["to_fy"] == _next_of(config)     # B7 Q7: the alarm already says it
    if state_known and not unprotected_next:
        for make in (_activation_item, _overlap_off_item):
            try:
                item = make(db, config, now) if make is _activation_item else make(db, config)
            except Exception as e:  # noqa: BLE001 - the banner still renders; the unreadable state is said
                logger.warning(f"The next financial year's overlap state could not be read ({e})")
                if not any(i["code"] == "overlap:unknown" for i in items):
                    items.append(_unknown_item())
                continue
            if item:
                items.append(item)

    from core import alert_class

    for cls in alert_class.switched_off(config):
        code = "alerts_off" if cls == alert_class.FAILURE else f"alerts_off:{cls}"
        label = alert_class.LABELS[cls]
        if cls in (alert_class.DECISION, alert_class.OVERDUE, alert_class.ACTION, alert_class.RESOLVED):
            fact = (f"The {label} are switched off, so nobody is mailed about them - "
                    "check the dashboard regularly")
        else:
            fact = f"The {label} are switched off; the dashboard still shows every problem"
        items.append(_item(AMBER, code, act("Switch them back on in Settings > Advanced", fact)))

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
