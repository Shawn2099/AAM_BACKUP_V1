"""M4 4.4 - per-job run budgets + schedule check (plan WP14, owner decision 2026-10-15).

Finding: the configured timeouts x retries add up to ~46 h (cloud) and ~9 h
(LAN), so nothing bounded how long a job held the backup slot. A big first
upload occupied the night for ~15 h (3 x 5 h attempts) and ended FAILED
while the LAN backup could not start; the production schedule (LAN 21:00,
cloud 22:00) lets any LAN run over ~2 h knock out that night's cloud backup.

Now: each job has a budget counted from its start (slot wait included); the
long steps get what is left; a job that reaches it stops cleanly as
*_PARTIAL and the next run continues; the config refuses a schedule where a
job's budget could run into the next slot job.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

import flow
from core.budget import BudgetExceeded, RunBudget, schedule_problems
from core.time_utils import now_iso
from models.config import AppConfig, BudgetConfig, CloudConfig, DashboardConfig, LanConfig, \
    PathsConfig, ScheduleConfig, WolConfig

H = 3600


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class TestRunBudget:
    def test_cap_gives_the_smaller_of_timeout_and_what_is_left(self):
        clock = Clock(0)
        b = RunBudget("cloud", 6 * H, 0.0)
        assert b.cap(5 * H, clock=clock) == 5 * H            # own timeout smaller
        clock.t = 4 * H
        assert b.cap(5 * H, reserve_s=H, clock=clock) == 1 * H  # budget binds
        assert b.binding(5 * H, H, clock=clock) is True
        assert b.binding(30 * 60, H, clock=clock) is False

    def test_used_up_budget_raises(self):
        b = RunBudget("lan", 4 * H, 0.0)
        with pytest.raises(BudgetExceeded, match="4 h used up"):
            b.cap(100, reserve_s=30 * 60, clock=Clock(3.6 * H))


class TestSchedule:
    def _jobs(self, cloud="0 18 * * *", lan="0 1 * * *", audit="0 10 * * SUN"):
        return {"cloud": (cloud, 6 * H), "lan": (lan, 4 * H), "audit": (audit, 8 * H)}

    def test_new_defaults_are_consistent(self):
        assert schedule_problems(self._jobs(), "Asia/Kolkata", H) == []

    def test_production_schedule_is_rejected_with_the_arithmetic(self):
        [p] = schedule_problems(self._jobs(cloud="0 22 * * *", lan="0 21 * * *"),
                                "Asia/Kolkata", H)
        assert "schedule.lan_cron" in p and "schedule.cloud_cron" in p
        assert "4 h > 1 h + 1 h" in p

    def test_proposed_production_schedule_fits(self):
        assert schedule_problems(self._jobs(cloud="0 2 * * *", lan="0 21 * * *"),
                                 "Asia/Kolkata", H) == []

    def test_old_default_audit_time_collides_with_the_lan_run(self):
        [p] = schedule_problems(self._jobs(audit="0 3 * * SUN"), "Asia/Kolkata", H)
        assert "schedule.audit_cron" in p and "4 h > 2 h + 1 h" in p

    def test_two_jobs_in_the_same_minute_are_rejected(self):
        problems = schedule_problems(self._jobs(cloud="0 1 * * *"), "Asia/Kolkata", H)
        assert any("0 h + 1 h" in p for p in problems)   # same minute: no gap at all

    def test_budget_must_exceed_the_slot_wait(self):
        problems = schedule_problems({"lan": ("0 1 * * *", H)}, "Asia/Kolkata", H)
        assert any(p.startswith("Set budget.lan_max_hours above") for p in problems)


def _app(**kw):
    base = dict(
        paths=PathsConfig(source_drive="E:\\DATA\\FY26-27",
                          lan_destination="\\\\nas\\share\\FY26-27"),
        lan=LanConfig(enabled=True), wol=WolConfig(enabled=False),
        cloud=CloudConfig(enabled=False), dashboard=DashboardConfig(),
    )
    base.update(kw)
    return AppConfig(**base)


class TestConfig:
    def test_defaults(self):
        b = BudgetConfig()
        assert (b.cloud_max_hours, b.lan_max_hours, b.audit_max_hours) == (6, 4, 8)
        assert ScheduleConfig().audit_cron == "0 10 * * SUN"

    def test_default_config_loads(self):
        _app()

    def test_conflicting_schedule_fails_at_load(self):
        with pytest.raises(ValidationError, match="schedule does not fit the run budgets"):
            _app(schedule=ScheduleConfig(lan_cron="0 21 * * *", audit_cron="0 22 * * SUN"))

    def test_a_disabled_leg_is_not_checked(self):
        # cloud disabled: its 22:00 slot right after LAN 21:00 is irrelevant
        _app(schedule=ScheduleConfig(lan_cron="0 21 * * *", cloud_cron="0 22 * * *"))


def _cfg(**budget):
    cfg = MagicMock()
    cfg.cloud.subprocess_timeout_seconds = 5 * H
    cfg.cloud.max_duration_seconds = None
    cfg.lan.subprocess_timeout_seconds = 4 * H
    cfg.budget.cloud_verify_reserve_minutes = 60
    cfg.budget.lan_post_reserve_minutes = 30
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    for k, v in budget.items():
        setattr(cfg.budget, k, v)
    return cfg


def _budget(seconds_left, total=6 * H):
    import time
    return RunBudget("cloud", total, time.monotonic() - (total - seconds_left))


class TestCloudSyncTask:
    def test_budget_binds_rclone_max_duration_and_a_stop_is_partial(self):
        with patch("flow.run_cloud_sync", return_value={
                "status": "CLOUD_PARTIAL", "exit_code": 10,
                "stats": {"transfers": 1200, "bytes": 180 * 1024 ** 3}}) as run:
            out = flow.cloud_sync_task.fn(_cfg(), "FY26-27", None, budget=_budget(3 * H))
        kw = run.call_args.kwargs
        assert kw["timeout"] <= 2 * H + 5                      # 3 h left - 1 h reserve
        assert kw["max_duration_seconds"] == kw["timeout"] - 300   # rclone stops itself first
        assert out["status"] == "CLOUD_PARTIAL" and out["budget_stopped"] is True
        assert "1200 file(s), 180.00 GB uploaded" in out["error"]
        assert "next run continues" in out["error"]

    def test_hard_kill_at_the_budget_is_partial_not_failed(self):
        with patch("flow.run_cloud_sync", return_value={
                "status": "CLOUD_FAILED", "exit_code": -1, "timed_out": True, "stats": None}):
            out = flow.cloud_sync_task.fn(_cfg(), "FY26-27", None, budget=_budget(3 * H))
        assert out["status"] == "CLOUD_PARTIAL" and out["budget_stopped"] is True

    def test_no_time_left_does_not_start_rclone(self):
        with patch("flow.run_cloud_sync") as run:
            out = flow.cloud_sync_task.fn(_cfg(), "FY26-27", None, budget=_budget(30 * 60))
        run.assert_not_called()
        assert out["budget_stopped"] is True and "no time was left" in out["error"]

    def test_when_the_budget_does_not_bind_nothing_changes(self):
        with patch("flow.run_cloud_sync", return_value={
                "status": "CLOUD_PARTIAL", "exit_code": 10, "stats": None}) as run:
            out = flow.cloud_sync_task.fn(_cfg(), "FY26-27", None, budget=_budget(6 * H, 7 * H))
        assert run.call_args.kwargs["timeout"] == 5 * H
        assert run.call_args.kwargs["max_duration_seconds"] is None   # config: auto
        assert "budget_stopped" not in out


class TestLanSyncTask:
    def test_robocopy_gets_what_is_left_and_a_kill_there_is_partial(self):
        with patch("flow.run_lan_sync", return_value={
                "status": "LAN_FAILED", "exit_code": -1, "timed_out": True,
                "error": "Timeout"}) as run:
            out = flow.lan_sync_task.fn(_cfg(), None, budget=_budget(2 * H, 4 * H))
        assert run.call_args.kwargs["timeout_s"] <= 1.5 * H + 5   # 2 h - 30 min reserve
        assert out["status"] == "LAN_PARTIAL" and out["budget_stopped"] is True

    def test_own_timeout_still_fails_as_before(self):
        with patch("flow.run_lan_sync", return_value={
                "status": "LAN_FAILED", "exit_code": -1, "timed_out": True, "error": "Timeout"}):
            with pytest.raises(RuntimeError):
                flow.lan_sync_task.fn(_cfg(), None, budget=_budget(10 * H, 10 * H))


_CLOUD = ["flow._record_running", "flow._open_run_artefacts", "flow.cloud_publish_artifact_task",
          "flow.cloud_record_task", "flow.cloud_verify_and_report_task", "flow.cloud_sync_task",
          "flow.cloud_access_task", "flow.cloud_guards_task", "flow.cloud_preflight_task",
          "flow.health_check_task", "flow.ManifestDB", "flow._record_run",
          "flow._send_recovery_if_needed", "flow.notify"]


def test_cloud_pipeline_budget_stop_is_partial_unverified_and_alerted_once():
    ps = [patch(n) for n in _CLOUD]
    m = {n.split(".", 1)[1]: p.start() for n, p in zip(_CLOUD, ps)}
    try:
        m["_open_run_artefacts"].return_value = None
        m["health_check_task"].return_value = None
        m["cloud_sync_task"].with_options.return_value.return_value = {
            "status": "CLOUD_PARTIAL", "exit_code": 10, "budget_stopped": True,
            "stats": {"transfers": 7, "bytes": 99}, "error": "Stopped at the run budget ..."}
        cfg = _cfg(cloud_max_hours=6)
        cfg.paths.database_path = ":memory:"
        cfg.cloud.max_attempts = 3
        with pytest.raises(flow.PartialRun):
            flow._run_cloud_pipeline(cfg, "run-b", now_iso())
        m["cloud_verify_and_report_task"].with_options.return_value.assert_not_called()
        m["cloud_record_task"].assert_not_called()
        args, kwargs = m["_record_run"].call_args
        assert args[4] == "CLOUD_PARTIAL"
        em = json.loads(kwargs["extended_metrics"])
        assert em["budget"]["stopped"] is True and em["budget"]["hours"] == 6
        assert em["tool_counts"]["transfers"] == 7 and "skipped" in em["verify"]
        alerts = [c.args[1] for c in m["notify"].call_args_list if c.args[1].kind == "FAILURE"]
        assert len(alerts) == 1 and alerts[0].status == "CLOUD_PARTIAL"
    finally:
        for p in ps:
            p.stop()


def test_audit_with_no_budget_left_is_incomplete_and_does_not_wake_the_nas(monkeypatch):
    recorded = []
    db = MagicMock()
    db.record_audit.side_effect = recorded.append
    cfg = MagicMock()
    cfg.cloud.enabled = False
    cfg.lan.enabled = True
    cfg.budget.audit_max_hours = 0.0001        # used up immediately
    cfg.budget.lan_post_reserve_minutes = 30
    cfg.lan.subprocess_timeout_seconds = 4 * H
    monkeypatch.setattr(flow, "load_config", lambda p: cfg)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow.ManifestDB, "from_config", lambda c: db)
    monkeypatch.setattr(flow, "_backup_slot", MagicMock())
    wol = MagicMock()
    monkeypatch.setattr(flow, "wol_check_task", wol)
    monkeypatch.setattr(flow, "notify", MagicMock())
    with pytest.raises(Exception):
        flow.integrity_audit_flow.fn("x.yaml", "lan")
    wol.assert_not_called()
    [row] = recorded
    assert row["status"] == "AUDIT_INCOMPLETE" and "run budget" in row["detail"]


def test_manual_budget_override_is_bounded():
    with pytest.raises(ValueError, match="budget_hours"):
        flow.backup.fn("x.yaml", "cloud", budget_hours=0)


class TestSuggestions:
    """The 'move it to HH:MM' advice is only advice: the verdict never depends on it, and the time
    it offers really makes the whole schedule fit."""

    JOBS = {"cloud": ("0 2 * * *", 6 * 3600), "lan": ("0 11 * * *", 4 * 3600),
            "audit": ("0 10 * * SUN", 8 * 3600)}

    def test_the_offered_time_clears_every_problem(self):
        import re
        [msg] = schedule_problems(self.JOBS, "Asia/Kolkata", H)
        assert msg.startswith("Move the weekly safety check to start at ")
        hh, mm = map(int, re.search(r"start at (\d\d):(\d\d)", msg).groups())
        moved = {**self.JOBS, "audit": (f"{mm} {hh} * * SUN", 8 * 3600)}
        assert schedule_problems(moved, "Asia/Kolkata", H) == []

    def test_the_rarer_job_is_the_one_asked_to_move(self):
        [msg] = schedule_problems(self.JOBS, "Asia/Kolkata", H)
        assert msg.startswith("Move the weekly safety check")    # not the daily LAN backup

    def test_a_failing_suggestion_search_does_not_change_the_verdict(self, monkeypatch):
        import core.budget as b
        monkeypatch.setattr(b, "_nearest_working_time", lambda *a, **k: 1 / 0)
        problems = schedule_problems(self.JOBS, "Asia/Kolkata", H)
        assert len(problems) == 1 and "could be skipped" in problems[0]

    def test_every_message_says_what_to_do_first(self):
        for jobs in (self.JOBS, {"cloud": ("0 2 * * *", 6 * 3600), "lan": ("0 6 * * *", 4 * 3600)}):
            for msg in schedule_problems(jobs, "Asia/Kolkata", H):
                assert msg.index("Why:") > 0 and msg.split()[0] in ("Move", "Start")
