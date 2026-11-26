"""F2 + F7b: approval reminders use db.open_approvals(), real guards key, and per-leg/guard metadata."""
from __future__ import annotations

from unittest.mock import patch

import flow
from core import approvals
from core.manifest import ManifestDB
from models.config import load_config


def test_f7b_waiting_summary_reads_real_guards_key():
    req = {
        "approval_id": "a1",
        "created_at": "2026-11-01T21:01:00+05:30",
        "guards": [{"guard": "G3_MASS_DELETE", "verdict": "BLOCK", "metrics": {"planned_deletes": 5000}}],
    }
    s = approvals.waiting_summary(req, now_iso="2026-11-04T09:00:00+05:30")
    assert s["deletions_waiting"] == 5000
    assert s["waiting_days"] == 2
    assert s["waiting_label"] == "5000 deletions waiting, 2 days"


def test_f7b_waiting_summary_non_g3_guard_label():
    req = {
        "approval_id": "a2",
        "created_at": "2026-11-01T06:00:00+05:30",
        "guards": [{"guard": "G5_CANARY", "verdict": "BLOCK", "metrics": {"destination": "\\\\nas\\share"}}],
    }
    s = approvals.waiting_summary(req, now_iso="2026-11-03T06:00:00+05:30")
    assert s["deletions_waiting"] == 0
    assert s["waiting_days"] == 2
    assert "deletions" not in s["waiting_label"].lower()
    assert "G5_CANARY" in s["waiting_label"]


def test_f2_send_pending_approval_reminders_uses_open_approvals_and_guard_info(tmp_path):
    db = ManifestDB(tmp_path / "m.db")
    try:
        aid_lan = db.open_approval_request(
            leg="lan",
            blocked_run_id="r-lan",
            fy="FY26-27",
            guards=[{"guard": "G3_MASS_DELETE", "verdict": "BLOCK", "metrics": {"planned_deletes": 120}}],
            phrase="APPROVE",
            created_at="2026-11-01T21:01:00+05:30",
        )
        aid_rol = db.open_approval_request(
            leg="rollover",
            blocked_run_id="r-rol",
            fy=None,
            guards=[{"guard": "G6_FY_ROLLOVER", "verdict": "BLOCK", "metrics": {"from_fy": "FY25-26"}}],
            phrase="APPROVE",
            created_at="2026-11-01T06:00:00+05:30",
        )
        # Approved (not PENDING) should NOT receive a pending reminder
        aid_app = db.open_approval_request(
            leg="cloud",
            blocked_run_id="r-cld",
            fy="FY26-27",
            guards=[{"guard": "G3_MASS_DELETE", "verdict": "BLOCK", "metrics": {"planned_deletes": 50}}],
            phrase="APPROVE",
            created_at="2026-11-01T02:01:00+05:30",
        )
        db.approve_request(
            aid_app, approved_by="owner", approved_at="2026-11-01T10:00:00+05:30",
            expires_at="2026-11-03T10:00:00+05:30",
        )

        sent_events = []
        cfg = load_config()
        with (
            patch.object(flow, "now_iso", return_value="2026-11-02T22:00:00+05:30"),
            patch.object(flow, "notify", side_effect=lambda c, ev, db=None: sent_events.append(ev)),
        ):
            flow._send_pending_approval_reminders(cfg, db)

        assert len(sent_events) == 2
        by_key = {ev.key: ev for ev in sent_events}
        ev_lan = by_key[f"approval:reminder:{aid_lan}:day1"]
        assert ev_lan.status == "LAN_BLOCKED"
        assert ev_lan.fault_code == "AAM-DEL-01"
        assert "120 deletions waiting" in ev_lan.message

        ev_rol = by_key[f"approval:reminder:{aid_rol}:day1"]
        assert ev_rol.status == "ROLLOVER_BLOCKED"
        assert ev_rol.fault_code == "AAM-ROL-02"
        assert "mass-delete" not in ev_rol.message.lower()
    finally:
        db.close()
