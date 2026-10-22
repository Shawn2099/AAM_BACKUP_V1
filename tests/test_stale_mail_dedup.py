"""Quieter "backup overdue" mail (plan Part C).

C1  the watchdog's stale:<leg> mail is skipped only when the owner was ALREADY told about the missed
    run: a failure / action mail (SENT, or SUPPRESSED = "already told") for that leg created after the
    scheduled run that should have succeeded. Failed, disabled and the stale mails themselves never
    count; a scheduler that stopped (no new mail) is still reported.
C2  the stale mail repeats at most once per alerts.stale_repeat_hours (default 24)."""
from __future__ import annotations

import pendulum
import pytest
from pydantic import ValidationError

import watchdog as wd
from core import notify as nt
from core.attention import compute_attention
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import AlertsConfig
from tests.test_m5_stale_alert import _cfg, _success

NOW = pendulum.datetime(2026, 10, 22, 15, 0, tz="Asia/Kolkata")


@pytest.fixture(autouse=True)
def _frozen():
    with pendulum.travel_to(NOW, freeze=True):
        yield


def _db(cfg):
    return ManifestDB.from_config(cfg)


def _lan_overdue(cfg):
    """LAN last succeeded 40 h ago: its 01:00 run on 22 Oct never succeeded (overdue since 07:00)."""
    db = _db(cfg)
    try:
        _success(db, "lan", 40)
        _success(db, "cloud", 1) if cfg.cloud.enabled else None
    finally:
        db.close()


def _note(cfg, *, status, kind="FAILURE", key="backup:lan:LAN_FAILED", mode="lan", hours_ago=1,
          audience=None):
    db = _db(cfg)
    try:
        db.record_notification({
            "event_key": key, "kind": kind, "mode": mode, "subject": "s", "status": status,
            "created_at": pendulum.now(IST).subtract(hours=hours_ago).isoformat(), "audience": audience})
    finally:
        db.close()


@pytest.fixture
def sent(monkeypatch):
    events = []
    monkeypatch.setattr(nt, "notify", lambda cfg, ev, db=None: events.append(ev))
    return events


def test_an_overdue_leg_with_no_earlier_mail_is_emailed(tmp_path, sent):
    cfg = _cfg(tmp_path)
    _lan_overdue(cfg)
    assert wd._check_stale_legs(cfg) == ["lan"]
    assert [e.key for e in sent] == ["stale:lan"]


@pytest.mark.parametrize("status", ["SENT", "SUPPRESSED"])
def test_a_failure_mail_after_the_missed_run_means_the_owner_was_told(tmp_path, sent, status):
    cfg = _cfg(tmp_path)
    _lan_overdue(cfg)
    _note(cfg, status=status)                                    # 1 h ago: after the 01:00 run
    assert wd._check_stale_legs(cfg) == ["lan"]                  # still reported as stale ...
    assert sent == []                                            # ... but no second mail


def test_the_banner_stays_red_even_when_the_mail_is_skipped(tmp_path, sent):
    cfg = _cfg(tmp_path)
    _lan_overdue(cfg)
    _note(cfg, status="SENT")
    wd._check_stale_legs(cfg)
    db = _db(cfg)
    try:
        assert any(i["code"] == "stale:lan" and i["level"] == "red" for i in compute_attention(db, cfg))
    finally:
        db.close()


@pytest.mark.parametrize("status", ["FAILED", "DISABLED"])
def test_an_undelivered_or_silenced_mail_does_not_count(tmp_path, sent, status):
    cfg = _cfg(tmp_path)
    _lan_overdue(cfg)
    _note(cfg, status=status)
    assert wd._check_stale_legs(cfg) == ["lan"]
    assert [e.key for e in sent] == ["stale:lan"]


def test_a_mail_from_before_the_missed_run_does_not_count(tmp_path, sent):
    cfg = _cfg(tmp_path)
    _lan_overdue(cfg)
    _note(cfg, status="SENT", hours_ago=30)                      # yesterday's failure, long before 01:00
    assert wd._check_stale_legs(cfg) == ["lan"]
    assert [e.key for e in sent] == ["stale:lan"]


def test_the_stale_mail_itself_never_counts(tmp_path, sent):
    cfg = _cfg(tmp_path)
    _lan_overdue(cfg)
    _note(cfg, status="SENT", kind="ACTION_REQUIRED", key="stale:lan")
    wd._check_stale_legs(cfg)
    assert [e.key for e in sent] == ["stale:lan"]


def test_a_mail_about_the_other_leg_does_not_count(tmp_path, sent):
    cfg = _cfg(tmp_path)
    _lan_overdue(cfg)
    _note(cfg, status="SENT", mode="cloud", key="backup:cloud:CLOUD_FAILED")
    wd._check_stale_legs(cfg)
    assert [e.key for e in sent] == ["stale:lan"]


def test_a_dead_scheduler_is_reported_after_the_next_missed_run(tmp_path, sent):
    """One 'could not start' mail on 20 Oct, then nothing: the later missed runs write no rows."""
    cfg = _cfg(tmp_path)
    _lan_overdue(cfg)
    _note(cfg, status="SENT", hours_ago=44)
    assert wd._check_stale_legs(cfg) == ["lan"]
    assert [e.key for e in sent] == ["stale:lan"]


# ── C2: how often it repeats ─────────────────────────────────────────────────

def test_the_repeat_window_is_a_bounded_config_key_defaulting_to_a_day():
    f = AlertsConfig.model_fields["stale_repeat_hours"]
    assert f.default == 24
    with pytest.raises(ValidationError):
        AlertsConfig(stale_repeat_hours=0)
    with pytest.raises(ValidationError):
        AlertsConfig(stale_repeat_hours=721)


def test_the_stale_event_carries_the_stale_repeat_window(tmp_path, sent):
    cfg = _cfg(tmp_path)
    _lan_overdue(cfg)
    wd._check_stale_legs(cfg)
    assert sent[0].cooldown_hours == cfg.alerts.stale_repeat_hours == 24


def _event(cooldown=None):
    return nt.AlertEvent(key="stale:lan", kind="ACTION_REQUIRED", mode="lan", title="t", message="m",
                         fault_code="AAM-SYS-03", cooldown_hours=cooldown)


def _record_sent(db, hours_ago):
    db.record_notification({"event_key": "stale:lan", "kind": "ACTION_REQUIRED", "mode": "lan",
                            "subject": "s", "status": "SENT", "fault_code": "AAM-SYS-03",
                            "created_at": pendulum.now(IST).subtract(hours=hours_ago).isoformat()})


def test_notify_honours_a_per_event_cooldown_longer_than_the_global_one(tmp_path):
    from test_support.smtp_server import smtp_server

    with smtp_server() as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1])         # repeat_cooldown_hours = 12
        db = _db(cfg)
        try:
            _record_sent(db, 13)
            plain = nt.notify(cfg, _event(None), db=db)           # 13 h > 12 h: sent again
            _record_sent(db, 13)
            longer = nt.notify(cfg, _event(24), db=db)            # 13 h < 24 h: suppressed
        finally:
            db.close()
    assert plain.status == nt.SENT and longer.status == nt.SUPPRESSED
    assert "cooldown 24" in longer.error


def test_the_short_fallback_notice_honours_it_too(tmp_path, monkeypatch):
    from test_support.smtp_server import smtp_server

    monkeypatch.setattr(nt, "_FALLBACK_SENT", {})
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1])
        first = nt._fallback_alert(cfg, _event(24), RuntimeError("x"))
        nt._FALLBACK_SENT["stale:lan"] -= 13 * 3600              # 13 h later
        second = nt._fallback_alert(cfg, _event(24), RuntimeError("x"))
    assert first.status == nt.SENT and second.status == nt.SUPPRESSED


def test_a_mail_about_an_older_missed_run_does_not_silence_a_scheduler_that_stopped_since(tmp_path, sent):
    """LAN last succeeded 40 h ago; a failure mail went out 30 h ago for the 21 Oct 01:00 run. Since then
    the 22 Oct 01:00 run produced no mail at all (nothing started): the stale mail must go out now."""
    cfg = _cfg(tmp_path)
    _lan_overdue(cfg)
    _note(cfg, status="SENT", hours_ago=30)            # 09:00 on the 21st, after that night's 01:00 run
    assert wd._check_stale_legs(cfg) == ["lan"]
    assert [e.key for e in sent] == ["stale:lan"]


def test_latest_firing_is_the_most_recent_owed_run():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from core import overdue

    tz = ZoneInfo("Asia/Kolkata")
    assert overdue.latest_firing("0 1 * * *", "Asia/Kolkata", datetime(2026, 10, 22, 15, 0, tzinfo=tz)) == \
        datetime(2026, 10, 22, 1, 0, tzinfo=tz)
    assert overdue.latest_firing("0 1 * * *", "Asia/Kolkata", datetime(2026, 10, 22, 1, 0, 30, tzinfo=tz)) == \
        datetime(2026, 10, 22, 1, 0, tzinfo=tz)                       # at the firing minute: that firing
    assert overdue.latest_firing("0 21 * * MON-FRI", "Asia/Kolkata", datetime(2026, 10, 26, 12, 0, tzinfo=tz)) == \
        datetime(2026, 10, 23, 21, 0, tzinfo=tz)                      # Monday noon: Friday night's run


def test_a_run_still_in_progress_does_not_count_as_the_owed_run(tmp_path, sent):
    """At 21:10 the 21:00 run is working; last night's failure mail already told the owner about the
    state. The stale mail must not go out in the minutes before tonight's own failure mail."""
    cfg = _cfg(tmp_path)
    cfg.schedule.lan_cron = "0 21 * * *"
    cfg.schedule.cloud_cron = "0 18 * * *"
    db = _db(cfg)
    try:
        t = NOW.subtract(days=3).replace(hour=1).isoformat()          # last LAN success: 3 days ago
        db.insert_run({"run_id": "old", "mode": "lan", "started_at": t, "ended_at": t, "status": "LAN_COMPLETE"})
        db.record_notification({                                     # last night's failure mail, 21:30 on the 21st
            "event_key": "backup:lan:LAN_FAILED", "kind": "FAILURE", "mode": "lan", "subject": "s",
            "status": "SENT", "created_at": NOW.subtract(days=1).set(hour=21, minute=30).isoformat()})
    finally:
        db.close()
    with pendulum.travel_to(NOW.set(hour=21, minute=10), freeze=True):
        assert wd._check_stale_legs(cfg) == ["lan"]
    assert sent == []
