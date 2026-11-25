"""Reports — failure alerts, weekly/monthly summaries via email and HTML.

Reads from ManifestDB run_history. Zero knowledge of backup internals.
generate_report_html() is shared between email delivery and UI download.
"""

import csv
import html
import io
import json
import re

import humanize
import pendulum
from loguru import logger

from core import faults, statuses
from core.email_compose import LEG_LABEL
from core.manifest import ManifestDB
from core.notify import AUDIENCE_ALERTS, AUDIENCE_REPORTS, SENT, send_email
from core.report_period import ReportPeriod
from core.time_utils import IST, format_ist, now_formatted
from models.config import NotificationConfig




def _send_email_with_attachments(
    config: NotificationConfig,
    subject: str,
    body_html: str,
    attachments: list[dict] = None,
    *,
    audience: str,
) -> bool:
    """Send one email through the SINGLE SMTP transport (core.notify.send_email,
    plan WP8): retries via tenacity, 5xx permanent / 4xx retried (the SMTP
    library raises SenderRefused for transient 4xx replies too), optional auth,
    configurable security and timeout. True only when delivered."""
    result = send_email(config, subject, body_html, attachments or [], audience=audience)
    return result.status == SENT


def send_failure_alert(
    config: NotificationConfig,
    firm_name: str,
    error_message: str,
    run_data: dict,
    timestamp: str = "",
) -> bool:
    """Send immediate email on backup failure.
    
    Attaches the full error log as a text file if the error is extremely long.
    """
    if not config.send_on_failure:
        logger.info("send_on_failure disabled - skipping alert")
        return False

    mode = (run_data.get("mode") or "unknown").upper()
    subject = f"Backup Failure Alert - {firm_name} ({mode})"

    ts_display = f"<p><strong>Time:</strong> {html.escape(format_ist(timestamp))}</p>" if timestamp else ""
    status_code = html.escape(str(run_data.get("status") or ""))
    exit_code = run_data.get("exit_code")
    exit_code_display = html.escape(str(exit_code)) if exit_code is not None else "-"

    # Truncate the inline error for a clean email UI, attach the rest
    attachments = []
    body_error_limit = 1000
    if len(error_message) > body_error_limit:
        error_display = html.escape(error_message[:body_error_limit]) + "\n\n... [TRUNCATED - SEE ATTACHMENT FOR FULL ERROR]"
        attachments.append({
            "filename": "failure_details.txt",
            "content": error_message.encode("utf-8")
        })
    else:
        error_display = html.escape(error_message)

    body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;color:#1f2937;margin:0;padding:0">
<div style="max-width:600px;margin:0 auto;padding:24px">
<h2 style="color:#dc2626;margin:0 0 16px 0">Backup Failure Alert \u2014 {html.escape(firm_name)}</h2>
<table style="width:100%;border-collapse:collapse;margin:16px 0">
<tr><td style="padding:8px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600;width:120px">Firm</td>
<td style="padding:8px 12px;border:1px solid #e5e7eb">{html.escape(firm_name)}</td></tr>
<tr><td style="padding:8px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600">Pipeline</td>
<td style="padding:8px 12px;border:1px solid #e5e7eb">{html.escape(mode)}</td></tr>
<tr><td style="padding:8px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600">Status</td>
<td style="padding:8px 12px;border:1px solid #e5e7eb">{status_code or '-'}</td></tr>
<tr><td style="padding:8px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600">Exit Code</td>
<td style="padding:8px 12px;border:1px solid #e5e7eb">{exit_code_display}</td></tr>
<tr><td style="padding:8px 12px;border:1px solid #e5e7eb;background:#f9fafb;font-weight:600">Error</td>
<td style="padding:8px 12px;border:1px solid #e5e7eb;color:#dc2626;white-space:pre-wrap;font-family:monospace;font-size:13px">{error_display}</td></tr>
</table>
{ts_display}
<p style="color:#6b7280;font-size:13px;margin-top:24px">Review the server logs or attached details for more info.</p>
</div>
</body>
</html>"""

    return _send_email_with_attachments(config, subject, body, attachments,
                                        audience=AUDIENCE_ALERTS)


# G5: cells that begin with one of these are interpreted as formulas/commands
# by spreadsheet apps when the CSV is opened in Excel (CSV injection / DDE).
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """Neutralize CSV formula injection for text cells.

    Error messages and metric JSON are free text that ends up in a CSV opened
    in Excel by accountants. A cell starting with =, +, -, @ (or tab/CR) is
    evaluated as a formula/DDE by Excel; prepending a single quote forces
    literal-text treatment in Excel and LibreOffice without changing what the
    user reads (the quote renders as the cell's text format marker, not the
    value). Numeric columns are never passed through this helper.
    """
    text = "" if value is None else str(value)
    if text.startswith(_CSV_INJECTION_PREFIXES):
        return "'" + text
    return text


def _generate_csv_data(runs: list[dict], run_fy: dict | None = None) -> bytes:
    """Generate a CSV payload containing the full history and un-truncated errors.
    `run_fy` ({run_id: FY}, only in an FY view) adds a last column "Financial Year"."""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write Headers
    writer.writerow([
        "Started At", "Ended At", "Mode", "Status", 
        "Files Copied", "Files Failed", "Bytes Copied", 
        "Duration (sec)", "Exit Code", "Error Message", "Extended Metrics"
    ] + (["Financial Year"] if run_fy is not None else []))
    
    # Write Rows
    for r in runs:
        writer.writerow([
            r.get("started_at", ""),
            r.get("ended_at", ""),
            r.get("mode", ""),
            r.get("status", ""),
            r.get("files_copied", 0),
            r.get("files_failed", 0),
            r.get("bytes_copied", 0),
            r.get("duration_seconds", 0),
            r.get("exit_code", ""),
            # G5: free-text columns neutralized against CSV injection
            _csv_safe(r.get("error_message", "")),
            _csv_safe(r.get("extended_metrics", ""))
        ] + ([run_fy.get(r.get("run_id"), "")] if run_fy is not None else []))
        
    return output.getvalue().encode("utf-8")


BACKUP_LEGS = ("cloud", "lan")


def resolve_period(days: int, period: str, now=None, report_period=None) -> ReportPeriod:
    """The period a report covers. Callers that know it (the scheduled flows: the previous
    calendar week/month of the run's scheduled time) pass `report_period`; the legacy
    (days, name) form means the previous calendar month for "Monthly", else the last `days`."""
    if report_period is not None:
        return report_period
    now = now or pendulum.now(IST)
    if period == "Monthly":
        return ReportPeriod.previous_month(now)
    return ReportPeriod.last_days(days, now)


def period_runs(db: ManifestDB, days: int, period: str, now=None, report_period=None):
    """(runs, label) of a report period (audit 2026-10-18 item 5; plan C)."""
    rp = resolve_period(days, period, now, report_period)
    return db.get_runs_between(rp.start, rp.end), rp.label


def _when(run: dict):
    try:
        return pendulum.parse(str(run.get("started_at")), tz=IST)
    except Exception:  # noqa: BLE001 - an unreadable time sorts first, never crashes
        return pendulum.datetime(1970, 1, 1, tz=IST)


_OK = (statuses.CATEGORY_SUCCESS, statuses.CATEGORY_NO_CHANGES)


def _fault(run: dict) -> tuple[str | None, str]:
    try:
        code = (json.loads(run.get("extended_metrics") or "{}") or {}).get("fault_code")
    except (ValueError, AttributeError):
        code = None
    if code in faults.CATALOG:
        return code, faults.CATALOG[code].title
    return None, statuses.label(run.get("status"))


FY_ERROR_TEXT = ("The financial-year state could not be read; the next financial year may not be "
                 "backed up. Open the dashboard.")
MANUAL_DETAIL = ("No backup was scheduled in this period - this backup runs only when started "
                 "from the dashboard.")


def _is_manual(config, leg: str) -> bool:
    """A leg that runs only when started by hand (`<leg>.scheduled: false`, B8 D8-5)."""
    return getattr(getattr(config, leg, None), "scheduled", True) is False


def _rec_time(rec, key: str):
    """(present, instant or None) of a stored time of an overlap record; present with None = unreadable."""
    from core import fy_overlap

    value = rec.get(key) if isinstance(rec, dict) else None
    if not value:
        return False, None
    try:
        return True, fy_overlap._instant(value)
    except Exception:  # noqa: BLE001 - an unreadable time is reported, never guessed
        return True, None


def report_fy_view(config, db, rp: ReportPeriod) -> dict | None:
    """What a report needs to split by financial year (B8 D8-1..D8-4, D8-11), or None for the baseline
    (no config, no readable configured FY). Never raises. `split` = the activation record of the next /
    configured FY intersects the period (or the records could not be read)."""
    from core import fy_overlap, fy_rollover

    paths, rollover = getattr(config, "paths", None), getattr(config, "rollover", None)
    source = getattr(paths, "source_drive", None)
    if not isinstance(source, str) or rollover is None:
        return None
    try:
        configured = fy_rollover.configured_fy(source, getattr(paths, "lan_destination", None))
        if configured is None:
            return None
        nxt, prev = fy_overlap.next_fy(configured), fy_overlap.prev_fy(configured)
    except Exception:  # noqa: BLE001
        return None
    view = {"configured": configured, "next": None,
            "next_off": getattr(rollover, "overlap_enabled", None) is not True,
            "closed": None, "closed_at": None, "windows": {}, "error": None, "split": False}
    try:
        recs = {fy: db.overlap_record(fy) for fy in (prev, configured, nxt)}
    except Exception:  # noqa: BLE001 - fail loud: the report says the state is unknown
        view.update(error=FY_ERROR_TEXT, split=True)
        return view
    recs = {fy: (rec if isinstance(rec, dict) else None) for fy, rec in recs.items()}

    def intersects(rec) -> bool:
        if rec is None:
            return False
        _, act = _rec_time(rec, "activated_at")
        _, end = _rec_time(rec, "ended_at")
        return (act is None or act < rp.end) and (end is None or end > rp.start)

    view["split"] = intersects(recs[configured]) or intersects(recs[nxt])
    for fy in (prev, configured, nxt):
        start, stop, known = rp.start, rp.end, True
        has_act, act = _rec_time(recs.get(fy), "activated_at")
        if has_act:
            known = act is not None
            start = max(start, act) if act is not None else start
        has_end, end = _rec_time(recs.get(fy_overlap.next_fy(fy)), "ended_at")
        if has_end:
            known = known and end is not None
            stop = min(stop, end) if end is not None else stop
        view["windows"][fy] = (start, stop) if known else None
    has_end, end = _rec_time(recs[configured], "ended_at")
    if has_end and end is not None:
        view["closed_at"] = recs[configured]["ended_at"]
        if end > rp.start:
            view["closed"] = prev
    rec_next = recs[nxt]
    if rec_next is not None and not rec_next.get("ended_at"):
        _, act = _rec_time(rec_next, "activated_at")
        if act is None or act < rp.end:
            view["next"] = nxt
    return view


def _row_fy(view: dict, row: dict) -> str:
    from core import fy_overlap

    return fy_overlap.row_fy_or_default(row, view["configured"], view["closed_at"])


def _section_fys(view: dict, leg_runs: list[dict]) -> list[str]:
    """The FY sections of one leg, oldest first (B8 D8-4)."""
    from core import fy_overlap

    row_fys = {_row_fy(view, r) for r in leg_runs}
    fys = {view["configured"]} | row_fys
    if view["next"] and (not view["next_off"] or view["next"] in row_fys):
        fys.add(view["next"])
    if view["closed"]:
        fys.add(view["closed"])

    def key(fy):
        try:
            return (fy_overlap._start_year(fy), fy)
        except ValueError:
            return (9999, fy)
    return sorted(fys, key=key)


def _latest_audit(db, leg: str, fy: str | None, view: dict | None):
    """The audit row that counts for `leg` (and FY in an FY view, D8-7); None when there is none."""
    from core import fy_overlap

    if fy is None:
        return db.latest_audit(leg)
    configured = view["configured"]
    if fy != configured:
        return db.latest_audit(leg, fy=fy)
    found = []
    plain = db.latest_audit(leg)
    if plain and fy_overlap.row_fy_or_default(plain, configured, view["closed_at"]) == configured:
        found.append(plain)
    suffixed = db.latest_audit(leg, fy=configured)
    if suffixed:
        found.append(suffixed)
    return fy_overlap._newest(found)


def _summary(db, leg: str, leg_runs: list[dict], label: str, last_of) -> dict:
    finished = [r for r in leg_runs
                if statuses.category(r.get("status")) != statuses.CATEGORY_RUNNING]
    ok_times = [_when(r) for r in finished if statuses.category(r.get("status")) in _OK]
    problems = []
    for r in finished:
        if statuses.category(r.get("status")) in _OK:
            continue
        code, title = _fault(r)
        problems.append({"when": format_ist(r.get("started_at"), "D MMM, h:mm A"),
                         "code": code, "title": title,
                         "resolved": any(t > _when(r) for t in ok_times),
                         "_at": _when(r)})
    try:
        last = last_of()
    except Exception:  # noqa: BLE001 - a partial DB (tests) has no history query
        last = None
    # P3: a failure is resolved by ANY later good run, also one after the period ends
    last_good_start = _when(last) if isinstance(last, dict) else None
    if last_good_start is not None:
        for p in problems:
            p["resolved"] = p["resolved"] or last_good_start > p["_at"]
    for p in problems:
        del p["_at"]
    last_when = (last.get("ended_at") or last.get("started_at")) if last else None
    return {"leg": leg, "label": label,
            "total": len(finished), "ok": len(ok_times), "problems": problems,
            "last_good": format_ist(last_when, "D MMM YYYY, h:mm A") if last_when
            else "none yet"}


def leg_summaries(db, runs: list[dict], legs, view: dict | None = None, config=None) -> list[dict]:
    """Per leg: runs OK, last good copy, problems (fault code, resolved?). With an FY `view` one entry
    per (leg, FY) built from that FY's rows only (B8); `config` only marks manual-only legs."""
    from core import fy_overlap

    out = []
    for leg in legs:
        leg_runs = sorted((r for r in runs if r.get("mode") == leg), key=_when)
        base = LEG_LABEL.get(leg, leg.upper())
        manual = _is_manual(config, leg)
        if view is None:
            s = _summary(db, leg, leg_runs, base + (" (run by hand)" if manual else ""),
                         lambda leg=leg: db.last_successful_run(leg))
            if manual:
                s["manual"] = True
            out.append(s)
            continue
        configured, nxt = view["configured"], fy_overlap.next_fy(view["configured"])
        for fy in _section_fys(view, leg_runs):
            mine = [r for r in leg_runs if _row_fy(view, r) == fy]
            label = f"{base} - {fy}"
            if fy == nxt and view["next_off"]:
                label += " (not backed up - switched off in Settings)"
            elif manual:
                label += " (run by hand)"
            s = _summary(db, leg, mine, label,
                         lambda leg=leg, fy=fy: fy_overlap.last_good_of_fy(
                             db, leg, fy, configured, view["closed_at"]))
            s["fy"] = fy
            s["audit_status"] = None
            if fy == configured or (fy == view["next"] and not view["next_off"]):
                try:
                    audit = _latest_audit(db, leg, fy, view)
                    s["audit_status"] = audit.get("status") if isinstance(audit, dict) else None
                except Exception:  # noqa: BLE001
                    pass
            if manual:
                s["manual"] = True
            if fy == view["closed"]:
                when = fy_overlap._instant(view["closed_at"]).in_timezone(IST).format("D MMM YYYY, HH:mm")
                s["note"] = (f"{fy} was closed on {when} (switch to {configured}); "
                             "it is not backed up any more.")
            out.append(s)
    return out


def _open_decisions(db) -> int:
    try:
        return len(db.open_approvals())
    except Exception:  # noqa: BLE001
        return 0


def report_headline(db, summaries: list[dict]) -> tuple[str, bool]:
    """(one line for the subject and the top, whether a person must act). A leg with no run is a
    missing backup only if a run was SCHEDULED in the period (`scheduled` > 0, or unknown)."""
    for s in summaries:
        if s["total"] == 0 and s.get("scheduled", 1) > 0:
            return f"action needed: {s['label']} - no backup ran", True
    for s in summaries:
        open_problems = [p for p in s["problems"] if not p["resolved"]]
        if open_problems:
            return f"action needed: {s['label']} - {open_problems[-1]['title']}", True
    for s in summaries:
        if "audit_status" in s:
            failed = s["audit_status"] == "VERIFICATION_FAILED"
        else:
            try:
                audit = db.latest_audit(s["leg"])
            except Exception:  # noqa: BLE001
                audit = None
            failed = isinstance(audit, dict) and audit.get("status") == "VERIFICATION_FAILED"
        if failed:
            return f"action needed: {s['label']} - safety check found differences", True
    if _open_decisions(db):
        return "action needed: a backup is waiting for your decision", True
    fixed = sum(len(s["problems"]) for s in summaries)
    if fixed:
        return f"backups OK - {fixed} problem(s) this period, all resolved", False
    if summaries and all(s["total"] == 0 for s in summaries):
        return "no backup was scheduled in this period", False
    return "all backups OK", False


def _audit_row(db, leg: str, fy: str | None = None, view: dict | None = None) -> dict:
    row = _audit_row_of(db, leg, fy, view)
    if fy is not None:
        row["fy"] = fy
    return row


def _audit_row_of(db, leg: str, fy: str | None, view: dict | None) -> dict:
    try:
        audit = _latest_audit(db, leg, fy, view)
    except Exception:  # noqa: BLE001 - a partial DB (tests) has no audit query
        audit = None
    if not isinstance(audit, dict) or not audit:
        return {"leg": leg, "status": "NOT VERIFIED", "checked": "-",
                "detail": "No independent audit has run yet."}
    status_text = audit.get("status", "UNKNOWN")
    if status_text == "AUDIT_INCOMPLETE":
        # WP7: "could not run" (network/TLS/DNS/process) is never divergence
        status_text = "AUDIT COULD NOT RUN"
    if status_text == "PENDING_BACKUP":
        status_text = "NEWER FILES WAIT FOR THE NEXT BACKUP"
    detail = str(audit.get("detail") or "-")
    if audit.get("newer_incomplete_at"):        # A1: the failure stays until a check reaches a verdict
        detail += f" The newer check on {format_ist(audit['newer_incomplete_at'])} could not run."
    return {"leg": leg, "status": status_text,
            "checked": format_ist(audit.get("ended_at") or audit.get("started_at")),
            "detail": detail}


def _scheduled_counts(schedule, summaries: list[dict], rp: ReportPeriod, view: dict | None = None,
                      config=None) -> None:
    """Set `scheduled` on each leg summary: how many runs the schedule planned in the period (so an
    empty period of a weekly plan is not reported as a missing backup). No schedule = unknown, which
    keeps the old behaviour (an empty period is action needed). In an FY view the count is taken inside
    the FY's own window (B8 D8-3); a manual-only leg (D8-5) and a next FY that is switched off (D8-4)
    plan nothing."""
    from core import fy_overlap, overdue

    for s in summaries:
        if s.get("manual") or _is_manual(config, s["leg"]):
            s["scheduled"] = 0
            continue
        window = (rp.start, rp.end)
        if view is not None and s.get("fy"):
            fy = s["fy"]
            if view["next_off"] and fy == fy_overlap.next_fy(view["configured"]):
                s["scheduled"] = 0
                continue
            if view["error"]:
                window = window if fy == view["configured"] else None
            else:
                window = view["windows"].get(fy, window)
            if window is None:
                s.pop("scheduled", None)
                continue
        try:
            first, last = window
            s["scheduled"] = (overdue.firings_between(
                getattr(schedule, f"{s['leg']}_cron"), schedule.timezone, first, last)
                if first < last else 0)
        except Exception:  # noqa: BLE001 - unreadable schedule: leave it unknown, never hide a gap
            s.pop("scheduled", None)


def _effective_view(config, db, rp: ReportPeriod, runs: list[dict], legs) -> dict | None:
    """The FY view when this report really splits by FY (B8 D8-1, D8-4), else None (baseline)."""
    from core import fy_overlap

    view = report_fy_view(config, db, rp)
    if view is None:
        return None
    mine = [r for r in runs if r.get("mode") in legs]
    if not view["split"] and not any(fy_overlap.fy_of_id(r.get("run_id")) for r in mine):
        return None
    if not view["error"] and all(
            _section_fys(view, [r for r in mine if r.get("mode") == leg]) == [view["configured"]]
            for leg in legs):
        return None
    return view


def build_report_model(db: ManifestDB, report_period: ReportPeriod, legs=None, schedule=None,
                       alerts_off=None, config=None) -> dict:
    """Everything a report says, computed ONCE (plan C3). Every renderer (email, download, CSV,
    subject) reads this and nothing else, so they cannot disagree.

    runs      backup runs (cloud / lan) that STARTED in the period; rollover and audit rows are
              not backups and are left out (P4)
    per_leg   files / bytes copied per leg, never added together across legs (P4)
    summaries per leg: runs OK, last good copy, problems; "resolved" looks at the whole history (P3)
    audits    latest independent audit of each ENABLED leg only (P5)

    With `config`, a period that touches the financial-year overlap is split per leg and FY (B8):
    summaries / per_leg / audits per (leg, FY) plus the keys fy_view, run_fy, labels. Otherwise the
    model is exactly the baseline one."""
    runs = [r for r in db.get_runs_between(report_period.start, report_period.end)
            if r.get("mode") in BACKUP_LEGS]
    legs = tuple(legs) if legs is not None else BACKUP_LEGS
    view = _effective_view(config, db, report_period, runs, legs) if config is not None else None
    if view is not None:
        runs = [r for r in runs if r.get("mode") in legs]
    summaries = leg_summaries(db, runs, legs, view, config)
    if schedule is not None or config is not None:
        _scheduled_counts(schedule, summaries, report_period, view, config)
    headline, act = report_headline(db, summaries)
    if view is not None and view["error"] and not act:
        headline, act = "action needed: the financial-year state could not be read", True

    cats = [statuses.category(r.get("status")) for r in runs]
    total = len(runs)
    successes = cats.count(statuses.CATEGORY_SUCCESS)
    no_changes = cats.count(statuses.CATEGORY_NO_CHANGES)
    partials = cats.count(statuses.CATEGORY_PARTIAL)
    skipped = cats.count(statuses.CATEGORY_SKIPPED)
    blocked = cats.count(statuses.CATEGORY_BLOCKED)
    running = cats.count(statuses.CATEGORY_RUNNING)
    totals = {"total": total, "successes": successes, "no_changes": no_changes,
              "partials": partials, "skipped": skipped, "blocked": blocked, "running": running,
              # everything not positively classified counts as a failure (fail loud)
              "failures": total - successes - no_changes - partials - skipped - blocked - running,
              "success_rate": ((successes + no_changes) / total * 100) if total else 0}
    if view is None:
        per_leg = {leg: {"files": sum(r.get("files_copied") or 0 for r in runs if r.get("mode") == leg),
                         "bytes": sum(r.get("bytes_copied") or 0 for r in runs if r.get("mode") == leg)}
                   for leg in legs}
        audits = [_audit_row(db, leg) for leg in legs]
        extra = {}
    else:
        run_fy = {r.get("run_id"): _row_fy(view, r) for r in runs}
        per_leg = {}
        for s in summaries:
            mine = [r for r in runs if r.get("mode") == s["leg"] and run_fy[r.get("run_id")] == s["fy"]]
            per_leg[f"{s['leg']}:{s['fy']}"] = {"files": sum(r.get("files_copied") or 0 for r in mine),
                                                "bytes": sum(r.get("bytes_copied") or 0 for r in mine)}
        active = [view["configured"]] + ([view["next"]] if view["next"] and not view["next_off"] else [])
        audits = [_audit_row(db, leg, fy, view) for leg in legs for fy in active]
        extra = {"fy_view": view, "run_fy": run_fy,
                 "labels": {f"{s['leg']}:{s['fy']}": s["label"] for s in summaries}}
    return {"alerts_off": list(alerts_off or []),
            "period": report_period, "legs": legs, "runs": runs, "totals": totals,
            "per_leg": per_leg, "summaries": summaries, "headline": headline, "act": act,
            "audits": audits,
            "open_decisions": _open_decisions(db), **extra}


_TD = "padding:6px 12px;border:1px solid #e5e7eb"
_TH = ("text-align:left;padding:6px 12px;border:1px solid #e5e7eb;background:#f3f4f6;"
       "font-size:0.85rem")


def _summary_html(model: dict) -> str:
    cell = "padding:6px 12px;border:1px solid #e5e7eb;vertical-align:top"
    rows = ""
    for s in model["summaries"]:
        if s["total"] == 0 and s.get("scheduled", 1) == 0:
            detail = (MANUAL_DETAIL if s.get("manual") else
                      "No backup was scheduled in this period.")
        elif s["total"] == 0:
            detail = "No backup ran in this period."
        else:
            items = [f"{html.escape(p['when'])}: {html.escape(p['title'])}"
                     + (f" ({p['code']})" if p["code"] else "")
                     + (" - resolved" if p["resolved"] else " - <b>not resolved</b>")
                     for p in s["problems"]]
            detail = "<br>".join(items) or "No problems."
        if s.get("note"):                                   # a closed financial year (B8)
            detail += "<br>" + html.escape(s["note"])
        rows += (f"<tr><th style='{cell};text-align:left;background:#f9fafb'>"
                 f"{html.escape(s['label'])}</th><td style='{cell}'>{s['ok']} of {s['total']} "
                 f"runs OK<br><span style='color:#6b7280'>Last good copy: "
                 f"{html.escape(s['last_good'])}</span></td><td style='{cell}'>{detail}</td></tr>")
    decisions = model["open_decisions"]
    if decisions == 1:
        open_items = "1 backup waiting for your decision on the dashboard"
    elif decisions:
        open_items = f"{decisions} backups waiting for your decision on the dashboard"
    else:
        open_items = "none"
    act = model["act"]
    todo = ("Open the dashboard and look at the items marked 'not resolved'; forward this "
            "report to support if you are unsure." if act else "Nothing.")
    colour = "#b91c1c" if act else "#047857"
    headline = model["headline"]
    fy_error = (model.get("fy_view") or {}).get("error")
    fy_error_html = (f"<p style=\"{_TD};background:#fef2f2\">{html.escape(fy_error)}</p>"
                     if fy_error else "")
    return (f"<div style='background:{colour};color:#ffffff;padding:10px 14px;border-radius:6px;"
            f"font-weight:600;margin:0 0 16px 0'>"
            f"{html.escape(headline[0].upper() + headline[1:])}</div>"
            f"<table style='width:100%;border-collapse:collapse;margin:0 0 12px 0'>{rows}</table>"
            f"{fy_error_html}"
            f"<p><b>Open items:</b> {html.escape(open_items)}<br><b>What to do:</b> "
            f"{html.escape(todo)}</p>")


def _stat_row(label: str, value, bold: bool = False) -> str:
    weight = ";font-weight:600" if bold else ""
    return (f"<tr><td style=\"{_TD};background:#f9fafb;width:180px{weight}\">{html.escape(label)}"
            f"</td><td style=\"{_TD}{weight}\">{value}</td></tr>\n")


def render_report_html(model: dict, firm_name: str, is_email: bool = False, name: str = "") -> str:
    """HTML of a report (the email body and the dashboard download), from the model only."""
    rp, t = model["period"], model["totals"]
    summary_html = _summary_html(model)
    if model.get("alerts_off"):          # a switched-off kind of email must be visible to the reader
        from core import alert_class

        names = ", ".join(alert_class.LABELS[c] for c in model["alerts_off"])
        summary_html += (f"<p style=\"{_TD};background:#fffbeb\">These emails are switched off: "
                         f"{html.escape(names)}. The dashboard still shows every problem.</p>")
    title = f"{name or rp.kind} Backup Report"

    rows = ""
    run_fy = model.get("run_fy")
    labels = model.get("labels") or {}
    for r in model["runs"]:                       # every run of the period, not a "last 10"
        dur = f"{r.get('duration_seconds', 0):.0f}s" if r.get("duration_seconds") else "-"
        err = r.get("error_message", "")
        # heavy truncation for the inline email (the CSV has the full error)
        err_display = (html.escape(err[:97]) + "..." if err and len(err) > 100
                       else html.escape(err) if err else "-")
        rows += (f"<tr><td>{html.escape(format_ist(r.get('started_at')))}</td>"
                 f"<td>{html.escape((r.get('mode') or 'unknown').upper())}"
                 f"{html.escape(' ' + run_fy[r.get('run_id')]) if run_fy and r.get('run_id') in run_fy else ''}</td>"
                 f"<td>{html.escape(statuses.label(r.get('status')))}</td>"
                 f"<td>{r.get('files_copied') or 0}</td><td>{html.escape(dur)}</td>"
                 f"<td>{err_display}</td></tr>")

    if t["total"] == 0 and model["summaries"] and all(
            s.get("scheduled", 1) == 0 for s in model["summaries"]):
        stats = (f"<p style=\"{_TD}\">No backup was scheduled in this period "
                 f"({html.escape(rp.label)}).</p>")
    elif t["total"] == 0:
        stats = (f"<p style=\"{_TD}\">No backups in {html.escape(rp.label)}.</p>")
    else:
        data = "".join(
            _stat_row(f"Files copied ({labels.get(leg) or LEG_LABEL.get(leg, leg.upper())})",
                      f"{v['files']:,}")
            + _stat_row(f"Data transferred ({labels.get(leg) or LEG_LABEL.get(leg, leg.upper())})",
                        f"{v['bytes']:,} ({humanize.naturalsize(v['bytes'], binary=True)})")
            for leg, v in model["per_leg"].items())
        stats = ("<table style=\"width:100%;border-collapse:collapse;margin:0 0 24px 0\">\n"
                 + _stat_row("Total Backups", t["total"])
                 + _stat_row("Successful Backups", t["successes"])
                 + _stat_row("No Changes (Up-to-date)", t["no_changes"])
                 + _stat_row("Partial Backups", t["partials"])
                 + _stat_row("Skipped Runs", t["skipped"])
                 + _stat_row("Blocked (approval needed)", t["blocked"])
                 + _stat_row("In Progress", t["running"])
                 + _stat_row("Failed Backups", t["failures"])
                 + _stat_row("Success Rate", f"{t['success_rate']:.1f}%", bold=True)
                 + data + "</table>")

    integrity_rows = "".join(
        f"<tr><td style=\"{_TD};background:#f9fafb;width:180px\">{a['leg'].upper()}{' ' + a['fy'] if a.get('fy') else ''} Integrity"
        f"</td><td style=\"{_TD}\">{html.escape(a['status'])} (checked: "
        f"{html.escape(str(a['checked']))})<br><span style=\"color:#6b7280;font-size:0.85rem\">"
        f"{html.escape(a['detail'])}</span></td></tr>" for a in model["audits"])

    csv_notice = ""
    if is_email:
        csv_notice = ('<p style="color:#059669;font-weight:600;font-size:0.9rem;margin:16px 0;">'
                      'A complete CSV with full error logs is attached to this email.</p>')
    generated = now_formatted("YYYY-MM-DD HH:mm z")
    head = "".join(f"<th style=\"{_TH}\">{h}</th>"
                   for h in ("Started", "Pipeline", "Status", "Files", "Duration", "Error"))
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:system-ui,sans-serif;color:#1f2937;margin:0;padding:0">
<div style="max-width:800px;margin:0 auto;padding:24px">
<h2 style="color:#1e3a5f;margin:0 0 8px 0">{html.escape(title)} — {html.escape(firm_name)}</h2>
<p style="color:#6b7280;margin:0 0 16px 0">Period: {html.escape(rp.label)} (generated {generated})</p>

{summary_html}

{csv_notice}

<h3 style="color:#374151;margin:0 0 8px 0">Summary</h3>
{stats}

<h3 style="color:#374151;margin:0 0 8px 0">Integrity Verification (independent weekly audit)</h3>
<p style="color:#6b7280;font-size:0.85rem;margin:0 0 8px 0">A COMPLETE backup means the transfer finished normally — it does not imply independent content verification. Integrity below reflects the latest read-only audit only.</p>
<table style="width:100%;border-collapse:collapse;margin:0 0 24px 0">
{integrity_rows}
</table>

<h3 style="color:#374151;margin:0 0 8px 0">Recent Backups (all of the period)</h3>
<table style="width:100%;border-collapse:collapse;margin:0 0 8px 0">
<tr>{head}</tr>
{rows}
</table>
<p style="color:#6b7280;font-size:0.85rem;margin:0">Showing all {len(model['runs'])} backups of the period</p>
</div>
</body>
</html>"""


def generate_report_html(
    db: ManifestDB,
    firm_name: str,
    days: int,
    period: str,
    is_email: bool = False,
    legs=None,
    now=None,
    report_period: ReportPeriod | None = None,
    schedule=None,
    alerts_off=None,
    config=None,
) -> str:
    """HTML report for a period (plan C). An empty period still gives a report that says so
    (P1). `legs` = the enabled legs; default: the legs that appear in the runs. `schedule` (the
    config's schedule section) lets an empty period say "no backup was scheduled" instead of
    "no backup ran" when none was due."""
    rp = resolve_period(days, period, now, report_period)
    return render_report_html(build_report_model(db, rp, legs, schedule=schedule, alerts_off=alerts_off,
                                                config=config),
                              firm_name, is_email, name=period)


def send_summary_report(
    db: ManifestDB,
    notifications: NotificationConfig,
    firm_name: str,
    days: int,
    period: str,
    body_html: str = None,
    legs=None,
    report_period: ReportPeriod | None = None,
    schedule=None,
    alerts_off=None,
    config=None,
) -> bool:
    """Send aggregated summary report via email with CSV attachment. `notifications` = the mail
    settings, `config` = the app config (financial-year view, B8).

    Returns True if email sent, False if it failed."""
    subject, body, attachments = build_summary_email(db, firm_name, days, period,
                                                     body_html=body_html, legs=legs,
                                                     report_period=report_period,
                                                     schedule=schedule, alerts_off=alerts_off,
                                                     config=config)
    return _send_email_with_attachments(notifications, subject, body, attachments,
                                        audience=AUDIENCE_REPORTS)


def build_summary_email(db: ManifestDB, firm_name: str, days: int, period: str,
                        body_html: str = None, legs=None, now=None,
                        report_period: ReportPeriod | None = None, schedule=None, alerts_off=None,
                        config=None) -> tuple[str, str, list[dict]]:
    """(subject, html body, [CSV attachment]) for a summary report. A period with no runs is
    still reported ("action needed: ... no backup ran", P1). The subject says the outcome."""
    rp = resolve_period(days, period, now, report_period)
    model = build_report_model(db, rp, legs, schedule=schedule, alerts_off=alerts_off, config=config)
    body = body_html or render_report_html(model, firm_name, is_email=True, name=period)
    safe_firm = re.sub(r'[^a-zA-Z0-9_\-]', '_', firm_name)
    attachments = [{"filename": f"{safe_firm}_{period}_Report.csv",
                    "content": _generate_csv_data(model["runs"], model.get("run_fy"))}]
    return (f"[{firm_name} Backup] {period} backup report {rp.label} - {model['headline']}",
            body, attachments)


def _legs_in(runs: list[dict]) -> tuple[str, ...]:
    return tuple(leg for leg in ("cloud", "lan") if any(r.get("mode") == leg for r in runs))


def versions_size_summary(config) -> str | None:
    """Weekly report line for the NAS versions folder size (Phase 1b B7 / V11)."""
    lan = getattr(config, "lan", None)
    if not lan or not getattr(lan, "enabled", False) or not getattr(lan, "versions_enabled", False):
        return None
    try:
        from core import lan_versions
        from models.config import resolve_versions_root

        vroot = resolve_versions_root(config.paths.lan_destination, lan.versions_folder)
        size_b = lan_versions.versions_folder_size_bytes(vroot)
        return f"NAS versions folder: {size_b / (1024 ** 3):.2f} GB ({vroot})"
    except Exception:  # noqa: BLE001
        return None


def send_weekly_report(
    db: ManifestDB,
    notifications: NotificationConfig,
    firm_name: str,
    body_html: str = None,
    legs=None,
    report_period: ReportPeriod | None = None,
    schedule=None,
    alerts_off=None,
    config=None,
) -> bool:
    return send_summary_report(db, notifications, firm_name, 7, "Weekly", body_html=body_html,
                               legs=legs, report_period=report_period, schedule=schedule,
                               alerts_off=alerts_off, config=config)


def send_monthly_report(
    db: ManifestDB,
    notifications: NotificationConfig,
    firm_name: str,
    body_html: str = None,
    legs=None,
    report_period: ReportPeriod | None = None,
    schedule=None,
    alerts_off=None,
    config=None,
) -> bool:
    return send_summary_report(db, notifications, firm_name, 30, "Monthly", body_html=body_html,
                               legs=legs, report_period=report_period, schedule=schedule,
                               alerts_off=alerts_off, config=config)

