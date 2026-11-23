"""The read-only 'Alerts history' view of the mail audit log (owner decision 2026-11-23, W39).

Rows come from the `notifications` table (never purged). Nothing here writes. No address is ever shown (only
the NUMBER of recipients), and nothing but the title (first line of the subject) and, for rows the program
wrote itself without mailing, a short note is returned - never a message body or a mail-server reply.
"""
from __future__ import annotations

import json
import re

import pendulum

from core import alert_class
from core.manifest import ALL_FYS
from core.notify import format_duration
from core.time_utils import format_ist

_EMAIL = re.compile(r"[^\s<>()\[\],;]+@[^\s<>()\[\],;]+")
_TITLE_MAX = 200
_NOTE_STATUSES = ("LOGGED_ONLY", "DISABLED", "SUPPRESSED", "CLOSED")   # our own wording, no mail-server text


def _clean(text: str | None, limit: int = _TITLE_MAX) -> str:
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    first = _EMAIL.sub("<address>", first)
    return first if len(first) <= limit else first[: limit - 1] + "…"


def _recipient_count(raw) -> int | None:
    if raw in (None, ""):
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return len(value) if isinstance(value, list) else None
    except (TypeError, ValueError):
        return None


def _incident_keys(mode: str | None, key: str) -> tuple[str, ...] | None:
    if mode != "rollover_files":
        return None
    problem = key.removeprefix("recovered:")
    return problem, f"recovered:{problem}"


def _recovered_after(db, row: dict) -> str | None:
    """'2h 5m': from the first problem row of the incident this recovery closed to the recovery itself."""
    if row.get("kind") != "RECOVERED" or row.get("status") not in ("SENT", "LOGGED_ONLY"):
        return None
    try:
        start = db.incident_start(row.get("mode") or "", fy=row.get("fy") or ALL_FYS,
                                  keys=_incident_keys(row.get("mode"), row["event_key"]),
                                  before_id=row["id"])
        if start is None:
            return None
        seconds = (pendulum.parse(row["created_at"]) - pendulum.parse(start["created_at"])).total_seconds()
        return format_duration(seconds)
    except Exception:  # noqa: BLE001 - the history page must still render
        return None


def shape_row(db, row: dict) -> dict:
    kind, key = row.get("kind") or "", row.get("event_key") or ""
    cls = alert_class.classify(kind, key)
    status = row.get("status") or ""
    return {
        "id": row["id"],
        "time": format_ist(row.get("created_at")),
        "time_iso": row.get("created_at"),
        "kind": kind,
        "class": cls,
        "class_label": alert_class.LABELS.get(cls, cls),
        "fault_code": row.get("fault_code") or "",
        "title": _clean(row.get("subject") or key),
        "fy": row.get("fy") or "",
        "leg": row.get("mode") or "",
        "status": status,
        "recipient_count": _recipient_count(row.get("recipients")),
        "recovered_after": _recovered_after(db, row),
        "note": _clean(row.get("error")) if status in _NOTE_STATUSES else "",
    }


def build_page(db, page: int, page_size: int) -> dict:
    """One page of the alerts history, newest first. `page` starts at 1; a page past the end is empty."""
    page = max(1, int(page))
    rows, total = db.notifications_page(page_size, (page - 1) * page_size)
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, -(-total // page_size)),
        "rows": [shape_row(db, r) for r in rows],
    }
