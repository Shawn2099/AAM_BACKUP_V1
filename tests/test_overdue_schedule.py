"""Overdue that follows the schedule (plan docs/PLAN_RECIPIENTS_AND_SCHEDULE_ALERTS_2026-10-22.md, Part B).

A leg is overdue when  now > (next scheduled firing after the last success STARTED)
                              + that leg's run budget + dashboard.stale_grace_hours.
It replaces the fixed 26 h, which raised false alarms for any schedule that is not daily."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core import overdue
from core.attention import AMBER, RED, compute_attention
from core.manifest import ManifestDB
from models.config import AppConfig
from test_support import ui_login

IST = ZoneInfo("Asia/Kolkata")


def at(y, mo, d, h=0, mi=0, tz=IST) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=tz)


# ── the pure rule ────────────────────────────────────────────────────────────

def test_deadline_is_next_firing_plus_budget_plus_grace():
    # daily 21:00, 4 h budget, 2 h grace: success started Thu 21:00:05 -> due Fri 21:00 + 6 h = Sat 03:00
    d = overdue.deadline("0 21 * * *", "Asia/Kolkata", at(2026, 10, 22, 21, 0).replace(second=5), 4, 2)
    assert d == at(2026, 10, 24, 3, 0)


def test_a_run_started_exactly_on_a_firing_minute_belongs_to_that_firing():
    d = overdue.deadline("0 21 * * *", "Asia/Kolkata", at(2026, 10, 22, 21, 0), 4, 2)
    assert d == at(2026, 10, 24, 3, 0)


def test_weekdays_only_waits_over_the_weekend():
    # success Friday 21:00 -> next firing Monday 21:00
    d = overdue.deadline("0 21 * * MON-FRI", "Asia/Kolkata", at(2026, 10, 23, 21, 5), 4, 2)
    assert d == at(2026, 10, 27, 3, 0)            # Tue 03:00


def test_weekly_and_monthly():
    assert overdue.deadline("0 2 * * SUN", "Asia/Kolkata", at(2026, 10, 25, 2, 10), 6, 2) == \
        at(2026, 11, 1, 10, 0)
    assert overdue.deadline("30 3 1 * *", "Asia/Kolkata", at(2026, 10, 1, 3, 40), 6, 2) == \
        at(2026, 11, 1, 11, 30)


def test_every_two_days_of_month():
    d = overdue.deadline("0 1 */2 * *", "Asia/Kolkata", at(2026, 10, 3, 1, 5), 4, 2)
    assert d == at(2026, 10, 5, 7, 0)


def test_a_late_start_counts_from_the_next_firing_not_the_missed_one():
    # the 21:00 run only started at 09:30 next morning: due tonight's firing + budget + grace
    d = overdue.deadline("0 21 * * *", "Asia/Kolkata", at(2026, 10, 23, 9, 30), 4, 2)
    assert d == at(2026, 10, 24, 3, 0)


def test_naive_and_other_timezone_inputs_are_read_in_the_schedule_timezone():
    utc = ZoneInfo("UTC")
    # 15:35 UTC == 21:05 IST
    d = overdue.deadline("0 21 * * *", "Asia/Kolkata", datetime(2026, 10, 22, 15, 35, tzinfo=utc), 4, 2)
    assert d == at(2026, 10, 24, 3, 0)


def test_the_deadline_is_real_elapsed_time_across_a_dst_change():
    ny = ZoneInfo("America/New_York")
    # firing Nov 1 00:30 EDT (04:30 UTC) + 2 h budget + 2 h grace = 08:30 UTC = 03:30 EST (the clocks
    # went back at 02:00 EDT); wall-clock arithmetic would say 04:30
    d = overdue.deadline("30 0 * * *", "America/New_York", datetime(2026, 10, 31, 0, 35, tzinfo=ny), 2, 2)
    assert d == datetime(2026, 11, 1, 3, 30, tzinfo=ny)


# ── attention ────────────────────────────────────────────────────────────────

def _cfg(tmp_path, lan_cron="0 21 * * *", cloud_enabled=False, grace=2.0, **sched):
    return AppConfig(
        firm_name="T",
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": "\\\\nas\\share\\FY26-27",
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": cloud_enabled},
        schedule={"lan_cron": lan_cron, **sched},
        budget={"lan_max_hours": 4},
        dashboard={"public_url": "http://backup-server:8080", "stale_grace_hours": grace,
                   **ui_login.DASHBOARD},
        notifications={"recipients": ["a@x.com"]},
    )


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _success(db, started, status="LAN_COMPLETE", run_id=None, **extra):
    db.insert_run({"run_id": run_id or f"r{started.isoformat()}", "mode": "lan",
                   "started_at": started.isoformat(), "ended_at": (started + timedelta(minutes=30)).isoformat(),
                   "status": status, **extra})


def _stale(db, cfg, now):
    return [i for i in compute_attention(db, cfg, now=now) if i["code"] == "stale:lan"]


def test_daily_schedule_matches_the_old_26_hours(tmp_path, db):
    cfg = _cfg(tmp_path)
    _success(db, at(2026, 10, 22, 21, 0, ).replace(second=3))
    assert not _stale(db, cfg, at(2026, 10, 24, 2, 59))          # still inside: 21:00 + 4 h + 2 h = 03:00
    [item] = _stale(db, cfg, at(2026, 10, 24, 3, 1))
    assert item["level"] == RED


def test_weekdays_only_is_quiet_all_weekend_and_red_on_tuesday(tmp_path, db):
    cfg = _cfg(tmp_path, lan_cron="0 21 * * MON-FRI")
    _success(db, at(2026, 10, 23, 21, 0).replace(second=3))      # Friday night
    for quiet in (at(2026, 10, 24, 12), at(2026, 10, 25, 23), at(2026, 10, 26, 20), at(2026, 10, 27, 2, 59)):
        assert not _stale(db, cfg, quiet), quiet
    assert _stale(db, cfg, at(2026, 10, 27, 3, 1))               # Monday's run never succeeded


def test_weekly_schedule_has_no_false_alarm_midweek(tmp_path, db):
    cfg = _cfg(tmp_path, lan_cron="0 2 * * SUN")
    _success(db, at(2026, 10, 25, 2, 0).replace(second=9))
    assert not _stale(db, cfg, at(2026, 10, 29, 12))
    assert not _stale(db, cfg, at(2026, 11, 1, 6, 1))            # Sunday 02:00 fired, its run may take 4 h
    assert _stale(db, cfg, at(2026, 11, 1, 8, 1))                # ... + 2 h grace passed, still no success


def test_a_healthy_run_that_ends_after_the_next_firing_is_not_overdue(tmp_path, db):
    cfg = _cfg(tmp_path)
    # last success started Thu 21:00; tonight's (Fri) run is still going at Sat 01:30 -> inside the budget
    _success(db, at(2026, 10, 22, 21, 0).replace(second=3))
    assert not _stale(db, cfg, at(2026, 10, 24, 1, 30))


def test_a_manual_run_between_firings_moves_the_deadline(tmp_path, db):
    cfg = _cfg(tmp_path)
    _success(db, at(2026, 10, 23, 12, 0))                        # manual, midday
    assert not _stale(db, cfg, at(2026, 10, 23, 23, 0))          # the 21:00 run of that day is still due
    assert not _stale(db, cfg, at(2026, 10, 24, 2, 59))          # 21:00 + 4 h budget + 2 h grace = 03:00
    assert _stale(db, cfg, at(2026, 10, 24, 3, 1))               # ... and that run never succeeded


def test_a_manual_run_just_before_a_firing_that_then_fails_goes_red_at_that_firings_deadline(tmp_path, db):
    cfg = _cfg(tmp_path)
    _success(db, at(2026, 10, 23, 20, 55))                       # manual run at 20:55, firing at 21:00 failed
    assert not _stale(db, cfg, at(2026, 10, 24, 2, 59))
    assert _stale(db, cfg, at(2026, 10, 24, 3, 1))               # that run has its own alert; accepted


def test_schedule_changed_daily_to_weekly_does_not_alarm(tmp_path, db):
    _success(db, at(2026, 10, 22, 21, 0).replace(second=3))      # last success under the daily plan
    db.set_schedule_in_effect({"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *",
                               "timezone": "Asia/Kolkata"})       # the plan that was served before
    db.set_schedule_in_effect({"cloud_cron": "0 2 * * *", "lan_cron": "0 2 * * SUN",
                               "timezone": "Asia/Kolkata"}, since=at(2026, 10, 26, 8, 0).isoformat())
    cfg = _cfg(tmp_path, lan_cron="0 2 * * SUN")
    assert not _stale(db, cfg, at(2026, 10, 26, 12))             # Monday after the change: not a week late
    assert not _stale(db, cfg, at(2026, 11, 1, 7, 59))           # the new plan's first run is due Sunday
    assert _stale(db, cfg, at(2026, 11, 1, 8, 1))                # ... and only then does it count


def test_schedule_changed_weekly_to_daily_starts_its_own_clock(tmp_path, db):
    _success(db, at(2026, 10, 20, 2, 0))                         # last success under the weekly plan
    db.set_schedule_in_effect({"cloud_cron": "0 2 * * *", "lan_cron": "0 2 * * SUN",
                               "timezone": "Asia/Kolkata"})       # the plan that was served before
    db.set_schedule_in_effect({"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *",
                               "timezone": "Asia/Kolkata"}, since=at(2026, 10, 24, 12, 0).isoformat())
    cfg = _cfg(tmp_path)
    assert not _stale(db, cfg, at(2026, 10, 24, 20, 0))          # first daily run is still ahead
    assert _stale(db, cfg, at(2026, 10, 25, 3, 1))               # it did not succeed: now red


def test_the_schedule_in_effect_wins_over_the_saved_config(tmp_path, db):
    cfg = _cfg(tmp_path, lan_cron="0 2 * * SUN")                 # saved: weekly
    db.set_schedule_in_effect({"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *",
                               "timezone": "Asia/Kolkata"})       # but the service still serves daily
    _success(db, at(2026, 10, 22, 21, 0).replace(second=3))
    assert _stale(db, cfg, at(2026, 10, 24, 3, 1))


def test_garbage_schedule_in_effect_falls_back_to_the_config(tmp_path, db):
    cfg = _cfg(tmp_path)
    db._get_conn().execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES ('schedule_in_effect', 'nonsense')")
    db._get_conn().commit()
    assert db.get_schedule_in_effect() is None
    _success(db, at(2026, 10, 22, 21, 0).replace(second=3))
    assert _stale(db, cfg, at(2026, 10, 24, 3, 1))


def test_the_message_explains_the_limit_in_the_legs_own_terms(tmp_path, db):
    cfg = _cfg(tmp_path)
    _success(db, at(2026, 10, 22, 21, 0).replace(second=3))
    [item] = _stale(db, cfg, at(2026, 10, 24, 5))
    assert "next scheduled run" in item["message"] and "26 Oct" not in item["message"]
    assert "2 h" in item["message"]


# ── never succeeded / first install ──────────────────────────────────────────

def test_a_fresh_install_is_amber_until_the_first_scheduled_run_is_due(tmp_path, db):
    cfg = _cfg(tmp_path)
    db.set_meta_created_at(at(2026, 10, 22, 10, 0).isoformat())
    items = compute_attention(db, cfg, now=at(2026, 10, 22, 12))
    assert not [i for i in items if i["code"] == "stale:lan"]
    waiting = [i for i in items if i["code"] == "first_run:lan"]
    assert waiting and waiting[0]["level"] == AMBER
    items = compute_attention(db, cfg, now=at(2026, 10, 23, 3, 1))     # 21:00 + 4 + 2 h passed
    assert [i for i in items if i["code"] == "stale:lan" and i["level"] == RED]


def test_a_database_without_the_marker_uses_the_oldest_run(tmp_path, db):
    cfg = _cfg(tmp_path)
    db._get_conn().execute("DELETE FROM db_meta WHERE key = 'created_at'")        # a pre-marker database
    db._get_conn().commit()
    db.insert_run({"run_id": "f1", "mode": "lan", "started_at": at(2026, 10, 22, 21, 0).isoformat(),
                   "ended_at": at(2026, 10, 22, 21, 1).isoformat(), "status": "LAN_FAILED"})
    assert db.first_known_at() == at(2026, 10, 22, 21, 0).isoformat()
    assert [i for i in compute_attention(db, cfg, now=at(2026, 10, 24, 3, 1)) if i["code"] == "stale:lan"]


def test_a_database_with_nothing_at_all_is_amber_not_red(tmp_path, db):
    cfg = _cfg(tmp_path)
    items = compute_attention(db, cfg, now=at(2026, 10, 22, 12))
    assert [i for i in items if i["code"] == "first_run:lan"]
    assert not [i for i in items if i["code"] == "stale:lan"]


def test_a_leg_switched_on_later_waits_for_its_own_first_run(tmp_path, db):
    cfg = _cfg(tmp_path)
    db.set_meta_created_at(at(2026, 1, 1).isoformat())
    db.record_config_change({"changed_at": at(2026, 10, 22, 22, 0).isoformat(), "changed_by": "x",
                             "key": "lan.enabled", "old_value": "False", "new_value": "True",
                             "risky": 0, "backup_file": ""})
    items = compute_attention(db, cfg, now=at(2026, 10, 23, 12))
    assert [i for i in items if i["code"] == "first_run:lan"] and not [i for i in items if i["code"] == "stale:lan"]


def test_the_marker_is_written_only_for_a_database_that_did_not_exist(tmp_path):
    path = str(tmp_path / "fresh.db")
    first = ManifestDB(path)
    created = first.meta_created_at()
    first.close()
    assert created
    again = ManifestDB(path)
    try:
        assert again.meta_created_at() == created                 # never rewritten by a later open
    finally:
        again.close()


# ── partial uploads that are making progress ─────────────────────────────────

def _partial(db, started, bytes_copied=1000, stopped=True):
    db.insert_run({"run_id": f"p{started.isoformat()}", "mode": "lan", "started_at": started.isoformat(),
                   "ended_at": started.isoformat(), "status": "LAN_PARTIAL", "bytes_copied": bytes_copied,
                   "extended_metrics": json.dumps({"budget": {"stopped": stopped}})})


def test_overdue_but_progressing_partial_uploads_are_amber_and_not_stale(tmp_path, db):
    cfg = _cfg(tmp_path)
    _success(db, at(2026, 10, 15, 21, 0))
    _partial(db, at(2026, 10, 23, 21, 0))
    items = compute_attention(db, cfg, now=at(2026, 10, 24, 12))
    assert not [i for i in items if i["code"] == "stale:lan"]
    catching = [i for i in items if i["code"] == "catching_up:lan"]
    assert catching and catching[0]["level"] == AMBER


@pytest.mark.parametrize("bytes_copied,stopped", [(0, True), (1000, False)])
def test_a_partial_without_progress_stays_red(tmp_path, db, bytes_copied, stopped):
    cfg = _cfg(tmp_path)
    _success(db, at(2026, 10, 15, 21, 0))
    _partial(db, at(2026, 10, 23, 21, 0), bytes_copied, stopped)
    assert [i for i in compute_attention(db, cfg, now=at(2026, 10, 24, 12)) if i["code"] == "stale:lan"]


# ── nothing else is affected ─────────────────────────────────────────────────

def test_a_disabled_leg_never_alerts(tmp_path, db):
    cfg = _cfg(tmp_path, cloud_enabled=False)
    assert not [i for i in compute_attention(db, cfg, now=at(2026, 10, 22, 12)) if i["code"].endswith(":cloud")]


def test_the_old_fixed_window_key_is_dropped_with_a_warning(tmp_path):
    import yaml

    from models.config import load_config
    from tests.test_m4_settings_ux import _write_config

    path = _write_config(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.setdefault("dashboard", {})["stale_after_hours"] = 26
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = load_config(str(path))                                   # still loads
    assert not hasattr(cfg.dashboard, "stale_after_hours")
    assert cfg.dashboard.stale_grace_hours == 2


# ── the service records the schedule it serves ───────────────────────────────

def test_serve_records_the_schedule_it_registers(tmp_path):
    import serve

    cfg = _cfg(tmp_path, lan_cron="0 20 * * MON-FRI")
    serve._record_schedule_in_effect(cfg)
    db = ManifestDB.from_config(cfg)
    try:
        stored = db.get_schedule_in_effect()
    finally:
        db.close()
    assert stored["lan_cron"] == "0 20 * * MON-FRI" and stored["timezone"] == "Asia/Kolkata"
    assert stored["lan_since"] is None and stored["cloud_since"] is None       # first record: no change


def test_a_failure_to_record_never_stops_the_scheduler(tmp_path, monkeypatch):
    import serve

    monkeypatch.setattr("core.manifest.ManifestDB.from_config",
                        classmethod(lambda cls, c: (_ for _ in ()).throw(OSError("db locked"))))
    serve._record_schedule_in_effect(_cfg(tmp_path))            # must not raise


# ── per-leg timing for the dashboard card ────────────────────────────────────

def _timing(db, cfg, now, leg="lan"):
    from core.attention import leg_timing
    return leg_timing(db, cfg, leg, now=now)


def test_timing_states_on_time_late_overdue(tmp_path, db):
    cfg = _cfg(tmp_path)                                          # daily 21:00, 4 h budget, 2 h grace
    _success(db, at(2026, 10, 22, 21, 0).replace(second=3))
    assert _timing(db, cfg, at(2026, 10, 23, 12))["state"] == "on_time"
    assert _timing(db, cfg, at(2026, 10, 23, 22, 0))["state"] == "on_time"       # tonight's run is working
    assert _timing(db, cfg, at(2026, 10, 24, 0, 30))["state"] == "on_time"       # inside its 4 h budget
    assert _timing(db, cfg, at(2026, 10, 24, 1, 30))["state"] == "late"          # budget used up, grace left
    assert _timing(db, cfg, at(2026, 10, 24, 3, 1))["state"] == "overdue"


def test_timing_for_a_weekly_plan_is_on_time_all_week(tmp_path, db):
    cfg = _cfg(tmp_path, lan_cron="0 2 * * SUN")
    _success(db, at(2026, 10, 25, 2, 0).replace(second=9))
    for day in (26, 27, 28, 29, 30, 31):
        assert _timing(db, cfg, at(2026, 10, day, 12))["state"] == "on_time", day


def test_timing_carries_the_due_and_overdue_moments(tmp_path, db):
    cfg = _cfg(tmp_path)
    _success(db, at(2026, 10, 22, 21, 0).replace(second=3))
    t = _timing(db, cfg, at(2026, 10, 23, 12))
    assert t["due_at"].startswith("2026-10-23T21:00") and t["overdue_at"].startswith("2026-10-24T03:00")


def test_timing_waiting_and_catching_up(tmp_path, db):
    cfg = _cfg(tmp_path)
    assert _timing(db, cfg, at(2026, 10, 22, 12))["state"] == "waiting"
    _success(db, at(2026, 10, 15, 21, 0))
    _partial(db, at(2026, 10, 23, 21, 0))
    assert _timing(db, cfg, at(2026, 10, 24, 12))["state"] == "catching_up"


def test_the_status_endpoint_and_the_page_use_the_servers_timing(tmp_path, db, monkeypatch):
    from pathlib import Path
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    import ui

    cfg = _cfg(tmp_path)
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    _success(db, at(2026, 10, 22, 21, 0).replace(second=3))
    data = TestClient(ui.app, raise_server_exceptions=False).get("/status").json()
    assert data["lan"]["timing"]["state"] in ("on_time", "late", "overdue", "waiting", "catching_up")
    js = (Path(__file__).resolve().parent.parent / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
    assert ".timing" in js and "diffDays < 2" not in js          # the fixed 1-day / 2-day labels are gone


# ── code review fixes (Opus, 2026-10-22) ─────────────────────────────────────

def test_since_is_kept_per_leg_so_changing_cloud_does_not_move_the_lan_clock(tmp_path, db):
    base = {"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *", "timezone": "Asia/Kolkata"}
    db.set_schedule_in_effect(base)
    assert db.get_schedule_in_effect()["cloud_since"] is None and db.get_schedule_in_effect()["lan_since"] is None
    db.set_schedule_in_effect({**base, "cloud_cron": "0 3 * * *"}, since="2026-10-24T12:00:00+05:30")
    got = db.get_schedule_in_effect()
    assert got["cloud_since"] == "2026-10-24T12:00:00+05:30" and got["lan_since"] is None
    db.set_schedule_in_effect({**base, "cloud_cron": "0 3 * * *"})            # restart, nothing changed
    assert db.get_schedule_in_effect()["cloud_since"] == "2026-10-24T12:00:00+05:30"
    db.set_schedule_in_effect({**base, "cloud_cron": "0 3 * * *", "timezone": "UTC"},
                              since="2026-10-25T00:00:00+05:30")             # timezone moves both legs
    got = db.get_schedule_in_effect()
    assert got["cloud_since"] == got["lan_since"] == "2026-10-25T00:00:00+05:30"


def test_a_cloud_schedule_change_leaves_a_failing_lan_red(tmp_path, db):
    cfg = _cfg(tmp_path)
    base = {"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *", "timezone": "Asia/Kolkata"}
    db.set_schedule_in_effect(base)
    _success(db, at(2026, 10, 20, 21, 0).replace(second=3))                  # LAN failing since the 21st
    db.set_schedule_in_effect({**base, "cloud_cron": "0 3 * * *"}, since=at(2026, 10, 23, 12).isoformat())
    assert _stale(db, cfg, at(2026, 10, 23, 13))


def test_the_first_recorded_schedule_does_not_hide_a_leg_that_was_already_failing(tmp_path, db):
    cfg = _cfg(tmp_path)
    _success(db, at(2026, 10, 20, 21, 0).replace(second=3))
    db.set_schedule_in_effect({"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *",
                               "timezone": "Asia/Kolkata"})                    # first record: no 'since'
    assert _stale(db, cfg, at(2026, 10, 23, 13))


def test_the_latest_owed_run_is_one_whose_run_should_already_have_finished():
    # daily 21:00, 4 h budget: at 21:10 the 21:00 run is still working, so the owed run is yesterday's
    assert overdue.latest_owed("0 21 * * *", "Asia/Kolkata", at(2026, 10, 22, 21, 10), 4) == at(2026, 10, 21, 21, 0)
    assert overdue.latest_owed("0 21 * * *", "Asia/Kolkata", at(2026, 10, 23, 1, 0), 4) == at(2026, 10, 22, 21, 0)


def test_a_schedule_the_database_cannot_give_falls_back_to_the_config(tmp_path, db, monkeypatch):
    from core import attention
    cfg = _cfg(tmp_path)

    def locked():
        raise OSError("database is locked")

    monkeypatch.setattr(db, "get_schedule_in_effect", locked)
    assert attention._schedule_in_effect(db, cfg)["lan_cron"] == "0 21 * * *"
    _success(db, at(2026, 10, 22, 21, 0).replace(second=3))
    assert _stale(db, cfg, at(2026, 10, 24, 3, 1))                    # the overdue check still works


def test_an_unreadable_schedule_is_red_not_silence(tmp_path, db, monkeypatch):
    from core import attention
    monkeypatch.setattr(attention, "_schedule_in_effect", lambda d, c: None)
    items = compute_attention(db, _cfg(tmp_path), now=at(2026, 10, 22, 12))
    red = [i for i in items if i["code"] == "stale:lan" and i["level"] == RED]
    assert red and "schedule cannot be read" in red[0]["message"]


def test_catching_up_ends_after_the_configured_number_of_days(tmp_path, db):
    cfg = _cfg(tmp_path)
    _success(db, at(2026, 10, 1, 21, 0))
    _partial(db, at(2026, 10, 2, 21, 0))                                       # first partial after the success
    _partial(db, at(2026, 10, 20, 21, 0))
    days = cfg.dashboard.catching_up_max_days
    assert days == 14
    # 3 days after the first partial: still catching up; 20 days after: red
    assert [i for i in compute_attention(db, cfg, now=at(2026, 10, 5, 12)) if i["code"] == "catching_up:lan"]
    assert [i for i in compute_attention(db, cfg, now=at(2026, 10, 22, 12)) if i["code"] == "stale:lan"]
