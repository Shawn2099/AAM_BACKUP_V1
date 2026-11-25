"""Tests for WS-B B1 notify fixes: P1 (escalation), P7 (duplicate window), P8/R2 (WARNING subject)."""
from __future__ import annotations

import pendulum

from core import email_compose, notify
from core.manifest import ManifestDB
from core.notify import SENT, SUPPRESSED, AlertEvent
from models.config import load_config


def _cfg(tmp_path):
    cfg = load_config("config.example.yaml")
    cfg.paths.database_path = str(tmp_path / "manifest.db")
    cfg.paths.log_directory = str(tmp_path / "logs")
    cfg.paths.runtime_dir = str(tmp_path / "runtime")
    cfg.notifications.recipients = ["ops@example.invalid"]
    cfg.notifications.report_recipients = ["partners@example.invalid"]
    cfg.alerts.repeat_cooldown_hours = 24
    return cfg


def test_p1_escalation_bypasses_24h_cooldown(tmp_path, monkeypatch):
    """P1: a higher-severity alert on the same key bypasses the 24h cooldown of a lower one."""
    cfg = _cfg(tmp_path)
    db = ManifestDB(cfg.paths.database_path)
    sent_subjects = []

    def fake_send(*a, **k):
        sent_subjects.append(a[1] if len(a) > 1 else k.get("subject"))
        return notify.DeliveryResult(SENT, attempts=1, message_id=f"<m{len(sent_subjects)}@test>")

    monkeypatch.setattr(notify, "send_email", fake_send)
    try:
        r1 = notify.notify(cfg, AlertEvent(
            key="backup:lan:warn", kind="WARNING", mode="lan",
            title="Slow copy", message="warn", status="LAN_PARTIAL", fault_code="AAM-LAN-02",
        ), db=db)
        assert r1.status == SENT

        r2 = notify.notify(cfg, AlertEvent(
            key="backup:lan:warn", kind="FAILURE", mode="lan",
            title="NAS offline", message="fail", status="LAN_FAILED", fault_code="AAM-LAN-02",
        ), db=db)
        assert r2.status == SENT, f"escalation should bypass cooldown, got {r2}"
        assert len(sent_subjects) == 2
    finally:
        db.close()


def test_p7_duplicate_window_suppresses_repeat_problem_within_300s(tmp_path, monkeypatch):
    """P7: one PROBLEM mail per incident key within alerts.duplicate_window_seconds (default 300)."""
    cfg = _cfg(tmp_path)
    assert cfg.alerts.duplicate_window_seconds == 300
    db = ManifestDB(cfg.paths.database_path)
    sent = []
    monkeypatch.setattr(
        notify, "send_email",
        lambda *a, **k: sent.append(a[1]) or notify.DeliveryResult(SENT, attempts=1, message_id=f"<p{len(sent)}@test>"),
    )
    try:
        r1 = notify.notify(cfg, AlertEvent(
            key="backup:cloud:fail", kind="FAILURE", mode="cloud",
            title="Cloud failed", message="err1", status="CLOUD_FAILED", fault_code="AAM-CLD-01",
        ), db=db)
        assert r1.status == SENT

        # Even with a different fault_code on the same key within 300s -> SUPPRESSED
        r2 = notify.notify(cfg, AlertEvent(
            key="backup:cloud:fail", kind="FAILURE", mode="cloud",
            title="Cloud failed again", message="err2", status="CLOUD_FAILED", fault_code="AAM-CLD-02",
        ), db=db)
        assert r2.status == SUPPRESSED
        assert len(sent) == 1

        # Simulate > 300s elapsed -> sends
        old_ts = pendulum.now("UTC").subtract(seconds=305).to_iso8601_string()
        db._conn.execute("UPDATE notifications SET created_at = ?", (old_ts,))
        db._conn.commit()
        r3 = notify.notify(cfg, AlertEvent(
            key="backup:cloud:fail", kind="FAILURE", mode="cloud",
            title="Cloud failed later", message="err3", status="CLOUD_FAILED", fault_code="AAM-CLD-03",
        ), db=db)
        assert r3.status == SENT
        assert len(sent) == 2
    finally:
        db.close()


def test_p8_warning_event_never_gets_action_needed_subject(tmp_path):
    """P8/R2: a WARNING-kind event never gets 'ACTION NEEDED' in its subject."""
    cfg = _cfg(tmp_path)
    ev = AlertEvent(
        key="rollover:activation_waiting", kind="WARNING", mode="rollover",
        title="Activation waiting", message="waiting", status="ROLLOVER_BLOCKED",
        fault_code="AAM-AUD-01",
    )
    subject, _, _ = email_compose.compose(cfg, ev)
    assert "ACTION NEEDED" not in subject
    assert "WARNING:" in subject
