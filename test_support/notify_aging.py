"""Test helper (W39): make the mail audit-log rows older, so a problem 'lasted' long enough for the RESOLVED mail.

A normal recovery of a problem that lasted under `alerts.resolved_mail_min_hours` is only logged (LOGGED_ONLY).
Tests that are about threading, scoping or counting of RESOLVED mails call `age_notifications(db)` between the
problem mail and the recovery."""
from __future__ import annotations

import pendulum


def age_notifications(db, hours: float = 25, mode: str | None = None) -> None:
    with db._lock:
        conn = db._get_conn()
        rows = conn.execute("SELECT id, created_at FROM notifications WHERE ? IS NULL OR mode = ?",
                            (mode, mode)).fetchall()
        for row in rows:
            moved = pendulum.parse(row["created_at"]).subtract(hours=hours).isoformat()
            conn.execute("UPDATE notifications SET created_at = ? WHERE id = ?", (moved, row["id"]))
        conn.commit()
