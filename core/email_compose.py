"""Plain-language emails (docs/EMAIL_CONTENT_SPEC.md §3, §7; audit 2026-10-18).

`compose(config, event, db)` turns one AlertEvent into (subject, html, text):
severity word, what happened (from the fault code's catalog text), "Is my data
safe?" (each enabled leg's last good copy), numbered steps, what happens next
(the next scheduled run, IST), the dashboard link, and a "For support" block
with the technical facts. Technical words appear only in that support block.
"""

from __future__ import annotations

import html
import socket
import tomllib
from pathlib import Path

import pendulum
from cronsim import CronSim

from core import faults
from core.cloud_sync import RCLONE_EXIT_MEANING
from core.time_utils import IST, format_ist

ROOT = Path(__file__).resolve().parent.parent
TIME_FMT = "D MMM YYYY, h:mm A"

LEG_LABEL = {
    "lan": "Office NAS backup",
    "cloud": "Online backup (Google Cloud)",
    "audit": "Weekly safety check",
    "watchdog": "Backup program on the server",
    "report": "Backup reports",
    "rollover": "Financial-year switch",
}
# what {leg_name} reads as inside a sentence ("... from the office backup (NAS)")
LEG_NAME = {"lan": "office backup (NAS)", "cloud": "online backup (Google Cloud)"}
# the leg inside "the next ... runs" / "each ... stops"
LEG_RUN = {"lan": "office NAS backup", "cloud": "online backup", "audit": "weekly safety check"}

SEVERITY = {  # event kind -> severity word when the event has no fault code
    "ACTION_REQUIRED": faults.ACTION, "FAILURE": faults.PROBLEM,
    "WARNING": faults.WARNING, "RECOVERED": "RESOLVED",
}
COLOUR = {faults.ACTION: "#b91c1c", faults.PROBLEM: "#b91c1c", faults.WARNING: "#b45309",
          "RESOLVED": "#047857", "INFO": "#1d4ed8"}
WHO = {faults.YOU: "you", faults.IT: "your IT person", faults.SUPPORT: "support"}

_ROBOCOPY_BITS = ((1, "files copied"), (2, "extra files in the backup"),
                  (4, "mismatched files"), (8, "some files could not be copied"),
                  (16, "fatal error"))


def exit_meaning(leg: str, code) -> str:
    if type(code) is not int or code < 0:
        return "the tool did not run or did not finish"
    if leg == "cloud":
        return RCLONE_EXIT_MEANING.get(code, "unknown exit code")
    if code == 0:
        return "no change"
    return "; ".join(text for bit, text in _ROBOCOPY_BITS if code & bit) or "unknown exit code"


def software_version() -> str:
    """'1.1.3 (mvp-hardening d78c41d)' - pyproject version + the checked-out commit."""
    try:
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"]["version"]
    except Exception:  # noqa: BLE001 - display only
        version = "?"
    try:
        git = ROOT / ".git"
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            ref_file = git / ref
            if ref_file.exists():
                sha = ref_file.read_text(encoding="utf-8").strip()
            else:
                packed = (git / "packed-refs").read_text(encoding="utf-8")
                sha = next(ln.split()[0] for ln in packed.splitlines() if ln.endswith(" " + ref))
            return f"{version} ({ref.rsplit('/', 1)[-1]} {sha[:7]})"
        return f"{version} ({head[:7]})"
    except Exception:  # noqa: BLE001
        return version


def _when(iso_ts) -> str:
    return format_ist(iso_ts, TIME_FMT)


def _age(iso_ts, now) -> str:
    try:
        hours = (now - pendulum.parse(str(iso_ts), tz=IST)).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return ""
    if hours < 1:
        return "less than an hour old"
    if hours < 48:
        return f"{hours:.0f} hour{'s' if round(hours) != 1 else ''} old"
    return f"{hours / 24:.0f} days old"


def _next_run(config, cron_key: str, now) -> str | None:
    try:
        tz = config.schedule.timezone
        start = now.in_timezone(tz)
        nxt = next(iter(CronSim(getattr(config.schedule, cron_key), start)))
        return pendulum.instance(nxt).in_timezone(IST).format(TIME_FMT) + " IST"
    except Exception:  # noqa: BLE001 - "what happens next" is informative only
        return None


def _facts(config) -> dict:
    # the host the backup really connects to (the UNC target), not the WoL
    # setting, which may hold a default while Wake-on-LAN is off
    dest = config.paths.lan_destination
    nas = dest.lstrip("\\").split("\\", 1)[0] if dest.startswith("\\\\") else ""
    if not nas and config.wol.enabled:
        nas = config.wol.server_ip
    return {"source": config.paths.source_drive, "dest": dest, "nas": nas or "the NAS",
            "leg_name": "backup"}


def _data_safety(config, db, now) -> list[str]:
    lines = ["Your files on the server: never changed or deleted by the backup."]
    for leg in ("lan", "cloud"):
        if not getattr(config, leg).enabled:
            continue
        try:
            last = db.last_successful_run(leg) if db is not None else None
        except Exception:  # noqa: BLE001
            last = None
        if last:
            when = last.get("ended_at") or last.get("started_at")
            lines.append(f"{LEG_LABEL[leg]}: last good copy {_when(when)} ({_age(when, now)}).")
        else:
            lines.append(f"{LEG_LABEL[leg]}: no good copy recorded yet.")
    return lines


def _next_text(config, event, fault, now) -> str:
    if event.kind == "RECOVERED":
        return "Nothing to do."
    if fault and fault.severity == faults.ACTION and event.status and \
            str(event.status).endswith("_BLOCKED"):
        return (f"Until you decide, each {LEG_RUN.get(event.mode, 'backup')} stops "
                "at this point - nothing is deleted. The request stays open on the dashboard "
                "and we remind you while it waits.")
    if event.mode == "watchdog":
        return "We email you again when the backup program is running again."
    cron = {"lan": "lan_cron", "cloud": "cloud_cron", "audit": "audit_cron"}.get(event.mode)
    nxt = _next_run(config, cron, now) if cron else None
    if nxt:
        return (f"The next {LEG_RUN[event.mode]} runs automatically on {nxt}. "
                "You get one email when it works again.")
    return "You get one email when this is resolved."


def effective_code(event) -> str | None:
    """The event's fault code; a backup event without one gets it from its
    status + exit code, so subject, headers and attachments always agree."""
    if event.fault_code or event.kind == "RECOVERED":
        return event.fault_code
    if event.mode in ("lan", "cloud") and event.status:
        return faults.classify_run(event.mode, str(event.status),
                                   sync_result={"exit_code": event.exit_code})
    return None


def compose(config, event, db=None, now=None) -> tuple[str, str, str]:
    """(subject, html body, plain-text body) for one alert event."""
    from core.notify import dashboard_url, html_to_text

    now = now or pendulum.now(IST)
    firm = config.firm_name
    facts = _facts(config)
    facts["leg_name"] = LEG_NAME.get(event.mode, "backup")
    label = LEG_LABEL.get(event.mode, event.mode.upper())

    if event.kind == "RECOVERED":
        fault = None
        severity = "RESOLVED"
        was = faults.get(event.resolved_code) if event.resolved_code else None
        subject = f"[{firm} Backup] RESOLVED: {label} is working again" + (
            f" (was {was.code})" if was else "")
        headline = f"{label} is working again."
        what = (f"The problem reported earlier ({was.title}) is fixed." if was
                else (event.message or "The earlier problem is fixed."))
        steps: tuple[str, ...] = ()
    elif event.fault_code is not None or event.status:
        fault = faults.get(effective_code(event))
        severity = fault.severity
        subject = f"[{firm} Backup] {severity}: {label} - {fault.title} ({fault.code})"
        headline = f"{label}: {fault.title}"
        what = fault.what.format(**facts)
        steps = tuple(s.format(**facts) for s in fault.steps)
    else:
        fault = None
        severity = SEVERITY.get(event.kind, "INFO")
        subject = f"[{firm} Backup] {severity}: {event.title}"
        headline = event.title
        what = event.message or ""
        steps = ()

    # a blocked run's own numbers are plain words ("would delete 1,240 of 5,800 files");
    # every other message is technical and stays in the support block
    details = event.message if (fault and str(event.status).endswith("_BLOCKED")
                                and event.message) else None
    who_list = fault.who if fault else ()
    who = ("you" if faults.YOU in who_list or not who_list
           else "fix by " + " or ".join(WHO[w] for w in who_list if w in WHO))
    url = dashboard_url(config)
    safety = _data_safety(config, db, now)
    next_text = _next_text(config, event, fault, now)
    support = _support_lines(config, event, fault)

    e = html.escape
    colour = COLOUR.get(severity, COLOUR["INFO"])
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
        "<body style='margin:0;padding:0;background:#ffffff;color:#1f2937;"
        "font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5'>"
        "<div style='max-width:600px;margin:0 auto;padding:16px'>",
        f"<div style='background:{colour};color:#ffffff;padding:10px 14px;border-radius:6px;"
        f"font-weight:600'>{e(severity)} - {e(headline)}</div>",
        f"<p>{e(_when(event.occurred_at))}</p>",
        (f"<p><b>Reminder:</b> this was first reported on {e(_when(event.first_reported_at))} "
         "and is still not fixed.</p>" if event.first_reported_at else ""),
        f"<p>{e(what)}</p>",
    ]
    if details:
        parts.append(f"<p><b>Details:</b> {e(details)}</p>")
    parts.append("<h3 style='margin:18px 0 6px 0'>Is my data safe?</h3><ul>"
                 + "".join(f"<li>{e(s)}</li>" for s in safety) + "</ul>")
    if steps:
        parts.append(f"<h3 style='margin:18px 0 6px 0'>What to do ({e(who)})</h3><p>"
                     + "<br>".join(f"{i}. {e(s)}" for i, s in enumerate(steps, 1)) + "</p>")
    parts.append(f"<h3 style='margin:18px 0 6px 0'>What happens next</h3><p>{e(next_text)}</p>")
    parts.append(
        f"<p style='margin:20px 0'><a href='{e(url)}' style='background:#1d4ed8;color:#ffffff;"
        f"padding:10px 16px;border-radius:6px;text-decoration:none;font-weight:600'>"
        f"Open the backup dashboard</a><br><span style='font-size:13px;color:#6b7280'>"
        f"{e(url)} (works on the office network)</span></p>")
    parts.append(
        "<hr style='border:none;border-top:1px solid #e5e7eb;margin:24px 0 8px 0'>"
        "<div style='font-size:12px;color:#6b7280'><b>For support</b><br>"
        + "<br>".join(e(s) for s in support) + "</div></div></body></html>")
    body_html = "".join(parts)
    return subject, body_html, html_to_text(body_html)


def _support_lines(config, event, fault) -> list[str]:
    leg = event.mode if event.mode in ("lan", "cloud") else ""
    lines = []
    if fault:
        lines.append(f"Fault code: {fault.code} ({fault.title})")
    if event.resolved_code:
        lines.append(f"Resolved fault code: {event.resolved_code}")
    lines.append(f"Reference: {event.run_id or event.key}")
    if event.status:
        lines.append(f"Status: {event.status}")
    if leg or event.exit_code is not None:
        lines.append(f"Exit code: {'-' if event.exit_code is None else event.exit_code} "
                     f"({exit_meaning(leg, event.exit_code)})")
    lines.append(f"Server: {socket.gethostname()}  |  Software: {software_version()}")
    lines.append(f"Time: {_when(event.occurred_at)}")
    for k, v in (event.details or {}).items():
        lines.append(f"{k}: {v}")
    message = event.message or ""
    limit = config.alerts.body_max_chars
    if message:
        lines.append("Message: " + (message if len(message) <= limit
                                    else message[:limit] + " ... [truncated - full text attached]"))
    return lines
