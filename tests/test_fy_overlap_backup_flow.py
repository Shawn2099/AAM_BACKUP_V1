"""B3 (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, sections 2.1, 2.2 and 9): backup() runs the FY passes of each leg.

Per leg the configured FY's pass runs first, then the next FY's (owner order). Both passes share the leg's budget
origin, the first pass alone is late, a LAN pass after the first shuts the NAS down only when every earlier pass
completed (D-8). A failing pass does not stop the other one, but the flow still raises. `fy=` runs one pass, an
inactive FY raises, the race guard stops the remaining passes when the configured FY was switched meanwhile.
The pipelines are mocks that record their config, run id and keyword arguments; the owner layout is used
(`E:\\FY26-27` + `\\\\nas\\backup\\FY26-27`, next year `E:\\FY27-28` + `\\\\nas\\backup\\FY27-28`).
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import flow
from core import fy_overlap, statuses
from flow import backup
from models.config import CONFIG_PATH, load_config

ACTIVATION_RECORD = {"activated_at": "2026-12-31T06:00:00+05:30", "from_fy": "FY26-27",
                     "source_folder": "E:\\FY27-28", "clock": {}}


def owner_config(*, overlap=True, cloud_scheduled=True):
    """A real AppConfig (the sandbox config) moved to the owner's layout."""
    base = load_config(CONFIG_PATH)
    paths = base.paths.model_copy(update={"source_drive": "E:\\FY26-27",
                                          "lan_destination": "\\\\nas\\backup\\FY26-27"})
    rollover = base.rollover.model_copy(update={"overlap_enabled": overlap})
    cloud = base.cloud.model_copy(update={"enabled": True, "scheduled": cloud_scheduled})
    lan = base.lan.model_copy(update={"enabled": True, "scheduled": True})
    return base.model_copy(update={"paths": paths, "rollover": rollover, "cloud": cloud, "lan": lan})


def _run_result(status):
    """A pipeline returns its status when it succeeded and raises otherwise (as the real pipelines do)."""
    if not statuses.is_success(status):
        raise flow.PartialRun(f"run ended {status}")
    return {"status": status, "exit_code": 0}


@contextlib.contextmanager
def backup_env(cfg, *, cloud=None, lan=None, lateness=0.0, reloads=None, record=True, record_error=None):
    """`cloud` / `lan`: {FY or "baseline": outcome}; an outcome is a status string or an exception to raise.
    record: whether the activation record of the next year exists (the fake database answers with it).
    record_error: opening the database for that record raises this."""
    cloud = cloud or {}
    lan = lan or {}
    calls = []
    with contextlib.ExitStack() as st:
        def p(target, **kw):
            return st.enter_context(patch(target, **kw))

        def pipeline(leg, outcomes, default):
            def run(config, run_id, started_at, monotonic_start=None, **kwargs):
                fy_pass = kwargs.get("fy_pass")
                fy = fy_pass.fy if fy_pass is not None else "baseline"
                calls.append(SimpleNamespace(leg=leg, fy=fy, config=config, run_id=run_id,
                                             kwargs=kwargs, source=config.paths.source_drive))
                outcome = outcomes.get(fy, default)
                if isinstance(outcome, BaseException):
                    raise outcome
                return _run_result(outcome)
            return run

        p("flow.configure_logging")
        p("flow.configure_prefect_bridge")
        p("flow._scheduled_lateness_seconds", return_value=lateness)
        p("flow._stable_run_id", side_effect=lambda mode: f"run-{mode}")
        if reloads is None:
            p("flow.load_config", return_value=cfg)
        else:
            p("flow.load_config", side_effect=reloads)
        db = p("flow.ManifestDB")
        db.from_config.return_value = db.return_value
        if record_error is not None:
            db.from_config.side_effect = record_error
        db.return_value.overlap_record.side_effect = (
            lambda fy: ACTIVATION_RECORD if fy == "FY27-28" and record else None)
        cloud_run = p("flow._run_cloud_pipeline", side_effect=pipeline("cloud", cloud, "CLOUD_COMPLETE"))
        lan_run = p("flow._run_lan_pipeline", side_effect=pipeline("lan", lan, "LAN_COMPLETE"))
        yield SimpleNamespace(calls=calls, cloud=cloud_run, lan=lan_run, db=db.return_value)


def order(env):
    return [(c.leg, c.fy) for c in env.calls]


# ── order, configs and the shared budget ─────────────────────────────────────────────────────────

class TestOrderAndConfigs:
    def test_each_leg_runs_the_configured_fy_then_the_next(self):
        with backup_env(owner_config()) as env:
            backup.fn(CONFIG_PATH, "all")
        assert order(env) == [("cloud", "FY26-27"), ("cloud", "FY27-28"),
                              ("lan", "FY26-27"), ("lan", "FY27-28")]

    def test_next_pass_uses_the_next_folders_and_run_ids_come_from_the_leg(self):
        with backup_env(owner_config()) as env:
            backup.fn(CONFIG_PATH, "all")
        nxt_cloud, nxt_lan = env.calls[1], env.calls[3]
        assert nxt_cloud.source == "E:\\FY27-28"
        assert nxt_lan.config.paths.lan_destination == "\\\\nas\\backup\\FY27-28"
        assert nxt_lan.config.paths.source_drive == "E:\\FY27-28"
        assert env.calls[0].config.paths.source_drive == "E:\\FY26-27"   # the configured pass is unchanged
        assert [c.run_id for c in env.calls] == ["run-cloud", "run-cloud", "run-lan", "run-lan"]

    def test_both_passes_of_a_leg_share_the_budget_origin_and_only_the_first_is_late(self):
        with backup_env(owner_config(), lateness=600.0) as env:
            backup.fn(CONFIG_PATH, "all")
        cloud_first, cloud_next, lan_first, lan_next = env.calls
        def origin(call):
            """Where the pipeline's RunBudget starts: budget_start - late_seconds (see _run_*_pipeline)."""
            return call.kwargs["budget_start"] - call.kwargs["late_seconds"]

        # a run that started 10 minutes late: the second pass counts from the same (late) origin
        assert origin(cloud_first) == origin(cloud_next)
        assert origin(lan_first) == origin(lan_next)
        assert cloud_first.kwargs["late_seconds"] == 600.0
        assert cloud_next.kwargs["late_seconds"] == 0.0
        assert lan_first.kwargs["late_seconds"] == 0.0     # a second leg starts when it starts (as today)


# ── selection ────────────────────────────────────────────────────────────────────────────────────

class TestSelectionByFy:
    def test_fy_of_the_next_year_runs_only_that_pass(self):
        with backup_env(owner_config()) as env:
            backup.fn(CONFIG_PATH, "all", fy="FY27-28")
        assert order(env) == [("cloud", "FY27-28"), ("lan", "FY27-28")]
        assert all(c.kwargs["fy_pass"].extra for c in env.calls)

    def test_fy_of_the_configured_year_runs_only_that_pass(self):
        with backup_env(owner_config()) as env:
            backup.fn(CONFIG_PATH, "cloud", fy="FY26-27")
        assert order(env) == [("cloud", "FY26-27")]
        assert env.calls[0].kwargs["fy_pass"].extra is False

    def test_inactive_fy_raises_and_runs_nothing(self):
        with backup_env(owner_config()) as env:
            with pytest.raises(ValueError, match="not an active"):
                backup.fn(CONFIG_PATH, "all", fy="FY28-29")
        assert env.calls == []

    def test_next_year_without_an_activation_record_is_inactive(self):
        with backup_env(owner_config(), record=False) as env:
            with pytest.raises(ValueError, match="not an active"):
                backup.fn(CONFIG_PATH, "all", fy="FY27-28")
            assert env.calls == []
            backup.fn(CONFIG_PATH, "all")
        assert order(env) == [("cloud", "baseline"), ("lan", "baseline")]

    def test_manual_run_of_an_unscheduled_leg_runs_both_years(self):
        with backup_env(owner_config(cloud_scheduled=False)) as env:
            backup.fn(CONFIG_PATH, "cloud")
        assert order(env) == [("cloud", "FY26-27"), ("cloud", "FY27-28")]


# ── failures ─────────────────────────────────────────────────────────────────────────────────────

class TestFailures:
    def test_a_failing_first_pass_does_not_stop_the_second_and_the_flow_fails(self):
        cloud = {"FY26-27": flow.PartialRun("cloud FY26-27 failed")}
        with backup_env(owner_config(), cloud=cloud) as env:
            with pytest.raises(ExceptionGroup) as info:
                backup.fn(CONFIG_PATH, "all")
        assert order(env) == [("cloud", "FY26-27"), ("cloud", "FY27-28"),
                              ("lan", "FY26-27"), ("lan", "FY27-28")]
        assert len(info.value.exceptions) == 1

    def test_a_failing_next_lan_pass_leaves_the_other_passes_alone(self):
        lan = {"FY27-28": flow.PartialRun("lan FY27-28 failed")}
        with backup_env(owner_config(), lan=lan) as env:
            with pytest.raises(ExceptionGroup) as info:
                backup.fn(CONFIG_PATH, "all")
        assert order(env) == [("cloud", "FY26-27"), ("cloud", "FY27-28"),
                              ("lan", "FY26-27"), ("lan", "FY27-28")]
        [err] = info.value.exceptions
        assert "FY27-28" in str(err)

    def test_both_failing_passes_are_both_reported(self):
        cloud = {"FY27-28": RuntimeError("cloud next")}
        lan = {"FY26-27": RuntimeError("lan current")}
        with backup_env(owner_config(), cloud=cloud, lan=lan):
            with pytest.raises(ExceptionGroup) as info:
                backup.fn(CONFIG_PATH, "all")
        assert len(info.value.exceptions) == 2


# ── LAN shutdown (D-8) ───────────────────────────────────────────────────────────────────────────

class TestLanShutdownRule:
    def test_earlier_pass_never_shuts_down_the_nas(self):
        with backup_env(owner_config()) as env:
            backup.fn(CONFIG_PATH, "lan")
        assert env.calls[0].kwargs["allow_shutdown"] is False

    def test_last_pass_may_shut_down_when_the_earlier_pass_completed(self):
        with backup_env(owner_config()) as env:
            backup.fn(CONFIG_PATH, "lan")
        assert env.calls[1].kwargs["allow_shutdown"] is True

    def test_last_pass_does_not_shut_down_after_an_earlier_failure(self):
        lan = {"FY26-27": flow.PartialRun("LAN_PARTIAL")}
        with backup_env(owner_config(), lan=lan) as env:
            with pytest.raises(ExceptionGroup):
                backup.fn(CONFIG_PATH, "lan")
        assert env.calls[1].kwargs["allow_shutdown"] is False

    def test_last_pass_does_not_shut_down_after_an_earlier_partial_completion(self):
        with backup_env(owner_config(), lan={"FY26-27": "LAN_PARTIAL"}) as env:
            with pytest.raises(ExceptionGroup):
                backup.fn(CONFIG_PATH, "lan")
        assert env.calls[1].kwargs["allow_shutdown"] is False


# ── race guard ───────────────────────────────────────────────────────────────────────────────────

class TestRaceGuard:
    def test_a_switch_during_the_run_stops_the_remaining_passes(self):
        cfg = owner_config()
        paths = cfg.paths.model_copy(update={"source_drive": "E:\\FY27-28",
                                             "lan_destination": "\\\\nas\\backup\\FY27-28"})
        switched = cfg.model_copy(update={"paths": paths})
        reads = []

        def reload(path):
            # read 1 = initial load_config, read 2 = pre-first-pass race check; switch happens mid-run
            reads.append(path)
            return cfg if len(reads) <= 2 else switched

        with backup_env(cfg, reloads=reload) as env:
            with pytest.raises(ExceptionGroup) as info:   # a non-success end must raise (review F3)
                backup.fn(CONFIG_PATH, "all")
        # the in-memory config is stale for BOTH legs: nothing after the switch runs
        assert order(env) == [("cloud", "FY26-27")]
        [err] = info.value.exceptions
        assert isinstance(err, flow.PartialRun)
        assert "FY26-27->FY27-28" in str(err)
        for skipped in ("cloud FY27-28", "lan FY26-27", "lan FY27-28"):
            assert skipped in str(err)

    def test_an_unreadable_config_between_passes_also_fails_the_flow(self):
        cfg = owner_config()
        reads = []

        def reload(path):
            reads.append(path)
            if len(reads) <= 2:
                return cfg
            raise OSError("config.yaml locked")

        with backup_env(cfg, reloads=reload) as env:
            with pytest.raises(ExceptionGroup):
                backup.fn(CONFIG_PATH, "cloud")
        assert order(env) == [("cloud", "FY26-27")]


# ── the activation record cannot be read (review F2) ─────────────────────────────────────────────

class TestOverlapCheckFailure:
    def test_a_database_error_still_backs_up_the_configured_fy_and_the_flow_fails(self):
        with backup_env(owner_config(), record_error=OSError("database is locked")) as env:
            with pytest.raises(ExceptionGroup) as info:
                backup.fn(CONFIG_PATH, "all")
        assert order(env) == [("cloud", "baseline"), ("lan", "baseline")]   # exactly the baseline calls
        assert set(env.calls[0].kwargs) == {"approval_id", "budget_hours", "late_seconds"}
        [err] = info.value.exceptions
        assert "FY27-28" in str(err) and "could not be checked" in str(err)

    def test_an_fy_run_is_refused_when_the_record_cannot_be_read(self):
        with backup_env(owner_config(), record_error=OSError("database is locked")) as env:
            with pytest.raises(ValueError, match="could not be checked"):
                backup.fn(CONFIG_PATH, "all", fy="FY27-28")
        assert env.calls == []


# ── overlap off: exactly today's single call ─────────────────────────────────────────────────────

class TestBaselineCall:
    def test_overlap_switched_off_is_one_baseline_call_per_leg(self):
        with backup_env(owner_config(overlap=False)) as env:
            backup.fn(CONFIG_PATH, "all")
        assert order(env) == [("cloud", "baseline"), ("lan", "baseline")]
        cloud_kwargs = env.calls[0].kwargs
        assert set(cloud_kwargs) == {"approval_id", "budget_hours", "late_seconds"}
        assert "allow_shutdown" not in env.calls[1].kwargs
        assert set(env.calls[1].kwargs) == {"approval_id", "budget_hours", "late_seconds"}
        assert env.calls[0].run_id == "run-cloud" and env.calls[1].run_id == "run-lan"

    def test_no_activation_record_is_one_baseline_call_per_leg(self):
        with backup_env(owner_config(), record=False) as env:
            backup.fn(CONFIG_PATH, "all")
        assert order(env) == [("cloud", "baseline"), ("lan", "baseline")]

    def test_no_fy_folder_is_one_baseline_call_per_leg(self):
        cfg = owner_config()
        paths = cfg.paths.model_copy(update={"source_drive": "D:\\Data",
                                             "lan_destination": "\\\\nas\\backup\\Data"})
        cfg = cfg.model_copy(update={"paths": paths})
        with backup_env(cfg) as env:
            backup.fn(CONFIG_PATH, "all")
        assert order(env) == [("cloud", "baseline"), ("lan", "baseline")]


# ── active_passes (plan 2.1) ─────────────────────────────────────────────────────────────────────

class TestActivePasses:
    def test_configured_then_next_when_the_record_is_open(self):
        cfg = owner_config()
        db = MagicMock()
        db.overlap_record.side_effect = lambda fy: ACTIVATION_RECORD if fy == "FY27-28" else None
        passes = fy_overlap.active_passes(cfg, db.overlap_record)
        assert [(p.fy, p.extra) for p in passes] == [("FY26-27", False), ("FY27-28", True)]
        assert passes[1].config.paths.source_drive == "E:\\FY27-28"

    def test_an_ended_record_is_not_active(self):
        cfg = owner_config()
        db = MagicMock()
        db.overlap_record.return_value = dict(ACTIVATION_RECORD, ended_at="2027-04-01T06:00:00+05:30")
        assert [p.fy for p in fy_overlap.active_passes(cfg, db.overlap_record)] == ["FY26-27"]

    def test_overlap_switched_off_keeps_only_the_configured_year(self):
        cfg = owner_config(overlap=False)
        db = MagicMock()
        db.overlap_record.return_value = ACTIVATION_RECORD
        assert [p.fy for p in fy_overlap.active_passes(cfg, db.overlap_record)] == ["FY26-27"]

    def test_a_mock_config_has_no_passes(self):
        assert fy_overlap.active_passes(MagicMock(), MagicMock()) == []
