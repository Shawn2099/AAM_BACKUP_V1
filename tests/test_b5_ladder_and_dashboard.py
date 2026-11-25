"""Tests for WS-B B5: approval reminder ladder (N1-N6) and dashboard waiting label."""
from __future__ import annotations

import pendulum

from core import approvals


def test_b5_reminder_ladder_rungs_quiet_days_weekly_and_restart_safety():
    created = "2026-11-01T21:01:00+05:30"
    rungs = [1, 2, 4, 7]
    sent: set[int] = set()

    def at_day(d: int) -> str:
        return pendulum.parse(created).add(days=d).to_iso8601_string()

    # N1: day 0 is initial alert, not a reminder rung
    assert approvals.due_reminder_rung(created, at_day(0), rungs, sent) is None

    # N2 & N3: rungs 1, 2, 4, 7 fire; 3, 5, 6 are quiet
    assert approvals.due_reminder_rung(created, at_day(1), rungs, sent) == 1
    sent.add(1)
    # N6: restart-safe (already sent rung 1 does not re-fire)
    assert approvals.due_reminder_rung(created, at_day(1), rungs, sent) is None

    assert approvals.due_reminder_rung(created, at_day(2), rungs, sent) == 2
    sent.add(2)
    assert approvals.due_reminder_rung(created, at_day(3), rungs, sent) is None
    assert approvals.due_reminder_rung(created, at_day(4), rungs, sent) == 4
    sent.add(4)
    assert approvals.due_reminder_rung(created, at_day(5), rungs, sent) is None
    assert approvals.due_reminder_rung(created, at_day(6), rungs, sent) is None
    assert approvals.due_reminder_rung(created, at_day(7), rungs, sent) == 7
    sent.add(7)

    # N4: weekly after day 7 (14, 21, 28); quiet in between
    for d in range(8, 14):
        assert approvals.due_reminder_rung(created, at_day(d), rungs, sent) is None
    assert approvals.due_reminder_rung(created, at_day(14), rungs, sent) == 14
    sent.add(14)
    assert approvals.due_reminder_rung(created, at_day(20), rungs, sent) is None
    assert approvals.due_reminder_rung(created, at_day(21), rungs, sent) == 21


def test_b5_waiting_summary_formats_deletions_and_days():
    req = {
        "approval_id": "abc",
        "created_at": "2026-11-01T21:01:00+05:30",
        "results": [{"guard": "G3_MASS_DELETE", "passed": False, "metrics": {"planned_deletes": 42}}],
    }
    now = "2026-11-05T09:00:00+05:30"
    s = approvals.waiting_summary(req, now_iso=now)
    assert s["deletions_waiting"] == 42
    assert s["waiting_days"] == 3
    assert s["waiting_label"] == "42 deletions waiting, 3 days"
