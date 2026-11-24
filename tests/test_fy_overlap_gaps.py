"""Gap tests from docs/TEST_QUALITY_REVIEW_2026-11-24.md (the ranked list of tests to add).

Each test proves one behaviour with the real function; only external tools are faked (as in the files it reuses).
  1  the real `flow._auto_scheduled_run` detector, and the weekly audit that depends on it (mutant M27)
  2  `flow._other_fy` and the `keep_fy=` call sites of both pipelines, down to a real approvals table (M34)
  3  a disabled leg gets no FY pass in `backup()` with the overlap on (M41, M45)
  4  activation when the next FY's folder cannot be made (core/fy_overlap.py, FOLDER_FAILED)
  5  a final backup that RAISES during the April check (flow._run_final_backups)
  6  a failed verify is never a final copy; the cloud hint text
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pendulum
import pytest

import flow
from core import fy_overlap, guards, statuses
from core import fy_rollover as fr
from core.manifest import ManifestDB
from flow import backup
from models.config import CONFIG_PATH
from tests.test_fy_overlap_activation import (  # noqa: F401  (fixtures)
    NOT_ADDED_KEY, TO, _at, _clock, _events, _new_folder, _run, cfg, db, fakes, source)
from tests.test_fy_overlap_audit import _activate_next, _env, _run as _run_audit
from tests.test_fy_overlap_backup_flow import backup_env, order, owner_config
from tests.test_fy_overlap_pipelines import (
    CLOUD_BASE, CUR_CLOUD, LAN_BASE, NEXT_CLOUD, STARTED, cloud_env, cur_pass, lan_env, make_config, next_pass)
from tests.test_fy_overlap_rollover import FROM, _OverlapRig

# the real detector, captured before any test patches `flow._auto_scheduled_run`
REAL_AUTO_SCHEDULED_RUN = flow._auto_scheduled_run


# ── 1. the real `_auto_scheduled_run` ────────────────────────────────────────────────────────────

def _flow_run_context(**run_attrs):
    return SimpleNamespace(flow_run=SimpleNamespace(**run_attrs))


class TestAutoScheduledRunDetector:
    def test_a_run_started_by_its_schedule_is_scheduled(self):
        with patch("prefect.context.FlowRunContext.get", return_value=_flow_run_context(auto_scheduled=True)):
            assert flow._auto_scheduled_run() is True

    def test_a_run_started_by_hand_is_not_scheduled(self):
        with patch("prefect.context.FlowRunContext.get", return_value=_flow_run_context(auto_scheduled=False)):
            assert flow._auto_scheduled_run() is False

    def test_outside_a_flow_run_is_not_scheduled(self):
        with patch("prefect.context.FlowRunContext.get", return_value=None):
            assert flow._auto_scheduled_run() is False

    def test_a_flow_context_without_a_run_is_not_scheduled(self):
        with patch("prefect.context.FlowRunContext.get", return_value=SimpleNamespace(flow_run=None)):
            assert flow._auto_scheduled_run() is False

    def test_a_context_that_cannot_be_read_means_by_hand(self):
        with patch("prefect.context.FlowRunContext.get", side_effect=RuntimeError("no context")):
            assert flow._auto_scheduled_run() is False

    def test_the_weekly_audit_skips_an_unscheduled_leg_only_on_the_scheduled_run(self, tmp_path):
        """The audit test patches the detector; here the REAL detector decides, fed by Prefect's own flag."""
        with _env(tmp_path, lan_scheduled=False, shutdown=True, wol=True) as env:
            env.auto.side_effect = REAL_AUTO_SCHEDULED_RUN
            _activate_next(env.cfg)
            with patch("prefect.context.FlowRunContext.get",
                       return_value=_flow_run_context(auto_scheduled=True)):
                _run_audit(env.cfg)
            assert [c for c in env.log if c[0] == "lan"] == []
            assert env.wol.call_count == 0
            assert env.log == [("cloud", "FY27-28"), ("cloud", "FY26-27")]

            with patch("prefect.context.FlowRunContext.get",
                       return_value=_flow_run_context(auto_scheduled=False)):
                _run_audit(env.cfg)
            assert [c for c in env.log if c[0] == "lan"] == [("lan", "FY27-28"), ("lan", "FY26-27")]


# ── 2. `_other_fy` and the keep_fy call sites ────────────────────────────────────────────────────

class TestOtherFy:
    def test_baseline_has_no_other_fy(self):
        assert flow._other_fy(None) is None

    def test_the_configured_pass_keeps_the_next_fy(self):
        assert flow._other_fy(cur_pass()) == "FY27-28"

    def test_the_next_pass_keeps_the_configured_fy(self):
        assert flow._other_fy(next_pass()) == "FY26-27"


def _block_record(fy):
    return {"fy": fy, "approved_guards": [],
            "results": [{"verdict": guards.BLOCK, "guard": "G2_SOURCE_SHRINK", "reason": "shrunk", "metrics": {}}]}


def _blocked(leg, fy):
    return guards.GuardBlocked(leg, _block_record(fy))


class TestKeepFyAtThePipelineCallSites:
    """A blocked pass opens its request with `keep_fy` = the OTHER active FY, so it supersedes only its own FY."""

    @pytest.mark.parametrize("fy_pass, config, expected", [
        (next_pass, NEXT_CLOUD, "FY26-27"),
        (cur_pass, CUR_CLOUD, "FY27-28"),
    ])
    def test_cloud_blocked_pass_keeps_the_other_fy(self, fy_pass, config, expected):
        with cloud_env() as env, patch("flow._open_block_request") as opened:
            env.guards.side_effect = _blocked("cloud", fy_pass().fy)
            with pytest.raises((flow.PartialRun, guards.GuardBlocked)):
                flow._run_cloud_pipeline(config, CLOUD_BASE, STARTED, time.monotonic(), fy_pass=fy_pass())
        assert env.record_run.call_args.args[4] == "CLOUD_BLOCKED"
        assert opened.call_args.kwargs["keep_fy"] == expected

    def test_cloud_baseline_blocked_run_keeps_nothing(self):
        with cloud_env() as env, patch("flow._open_block_request") as opened:
            env.guards.side_effect = _blocked("cloud", "FY26-27")
            with pytest.raises((flow.PartialRun, guards.GuardBlocked)):
                flow._run_cloud_pipeline(CUR_CLOUD, CLOUD_BASE, STARTED, time.monotonic())
        assert opened.call_args.kwargs["keep_fy"] is None

    @pytest.mark.parametrize("pass_of, fy, expected", [(next_pass, "FY27-28", "FY26-27"),
                                                      (cur_pass, "FY26-27", "FY27-28")])
    def test_lan_blocked_pass_keeps_the_other_fy(self, tmp_path, pass_of, fy, expected):
        cfg = make_config(f"E:\\{fy}", str(tmp_path / fy))
        (tmp_path / fy).mkdir()
        with lan_env() as env, patch("flow._open_block_request") as opened:
            env.guards.side_effect = _blocked("lan", fy)
            with pytest.raises((flow.PartialRun, guards.GuardBlocked)):
                flow._run_lan_pipeline(cfg, LAN_BASE, STARTED, time.monotonic(), allow_shutdown=False,
                                       fy_pass=pass_of(cfg))
        assert env.record_run.call_args.args[4] == "LAN_BLOCKED"
        assert opened.call_args.kwargs["keep_fy"] == expected

    def test_a_blocked_next_fy_pass_leaves_the_configured_fys_pending_approval_open(self, tmp_path):
        """Whole chain with a real approvals table: pipeline -> _open_block_request -> open_approval_request."""
        real = ManifestDB(str(tmp_path / "m.db"))
        try:
            pending_cur = real.open_approval_request(
                leg="cloud", blocked_run_id="r-cur", fy="FY26-27", guards=[], phrase="APPROVE",
                created_at="2027-01-10T01:00:00+05:30")
            with cloud_env() as env:
                env.db.return_value.open_approval_request.side_effect = real.open_approval_request
                env.guards.side_effect = _blocked("cloud", "FY27-28")
                with pytest.raises((flow.PartialRun, guards.GuardBlocked)):
                    flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                             fy_pass=next_pass())
            assert real.get_approval(pending_cur)["status"] == "PENDING"
            [new] = [a for a in real.open_approvals() if a["fy"] == "FY27-28"]
            assert new["status"] == "PENDING"
        finally:
            real.close()


class TestBlockRequestKeepsTheOtherFyPending:
    """M34 at unit level: the real database, the real `_open_block_request`."""

    def _open(self, real, fy, keep_fy):
        proxy = SimpleNamespace(open_approval_request=real.open_approval_request, close=lambda: None)
        with patch("flow.ManifestDB") as mdb, patch("flow.notify"):
            mdb.from_config.return_value = proxy
            flow._open_block_request(NEXT_CLOUD, "cloud", "run-x", _block_record(fy), STARTED, None,
                                     keep_fy=keep_fy, fy=fy, title_fy=fy)

    def _pending(self, real, fy, keep_fy=None):
        return real.open_approval_request(leg="cloud", blocked_run_id=f"old-{fy}", fy=fy, guards=[],
                                          phrase="APPROVE", created_at="2027-01-10T01:00:00+05:30",
                                          keep_fy=keep_fy)

    def test_a_blocked_next_fy_pass_does_not_supersede_the_configured_fys_request(self, tmp_path):
        real = ManifestDB(str(tmp_path / "m.db"))
        try:
            keep = self._pending(real, "FY26-27")
            old_own = self._pending(real, "FY27-28", keep_fy="FY26-27")
            self._open(real, "FY27-28", keep_fy="FY26-27")
            assert real.get_approval(keep)["status"] == "PENDING"
            assert real.get_approval(old_own)["status"] == "SUPERSEDED"   # its own FY's older request is replaced
        finally:
            real.close()

    def test_a_blocked_configured_pass_does_not_supersede_the_next_fys_request(self, tmp_path):
        real = ManifestDB(str(tmp_path / "m.db"))
        try:
            keep = self._pending(real, "FY27-28")
            old_own = self._pending(real, "FY26-27", keep_fy="FY27-28")
            self._open(real, "FY26-27", keep_fy="FY27-28")
            assert real.get_approval(keep)["status"] == "PENDING"
            assert real.get_approval(old_own)["status"] == "SUPERSEDED"
        finally:
            real.close()

    def test_without_keep_fy_the_baseline_supersedes_every_older_request(self, tmp_path):
        real = ManifestDB(str(tmp_path / "m.db"))
        try:
            other = self._pending(real, "FY27-28")
            self._open(real, "FY26-27", keep_fy=None)
            assert real.get_approval(other)["status"] == "SUPERSEDED"
        finally:
            real.close()


# ── 3. a disabled leg gets no FY pass ────────────────────────────────────────────────────────────

def _with_leg(cfg, leg, **update):
    return cfg.model_copy(update={leg: getattr(cfg, leg).model_copy(update=update)})


class TestDisabledLegWithTheOverlapOn:
    def test_a_disabled_nas_gets_no_pass_in_either_fy(self):
        cfg = _with_leg(owner_config(), "lan", enabled=False)
        with backup_env(cfg) as env:
            backup.fn(CONFIG_PATH, "all")
        assert order(env) == [("cloud", "FY26-27"), ("cloud", "FY27-28")]
        env.lan.assert_not_called()

    def test_a_disabled_cloud_gets_no_pass_in_either_fy(self):
        cfg = _with_leg(owner_config(), "cloud", enabled=False)
        with backup_env(cfg) as env:
            backup.fn(CONFIG_PATH, "all")
        assert order(env) == [("lan", "FY26-27"), ("lan", "FY27-28")]
        env.cloud.assert_not_called()

    def test_disabled_nas_and_a_manual_only_cloud_still_runs_both_cloud_passes_by_hand(self):
        cfg = _with_leg(_with_leg(owner_config(), "lan", enabled=False), "cloud", scheduled=False)
        with backup_env(cfg) as env:
            backup.fn(CONFIG_PATH, "all")
        assert order(env) == [("cloud", "FY26-27"), ("cloud", "FY27-28")]

    @pytest.mark.parametrize("leg, fy", [("lan", "FY27-28"), ("cloud", "FY27-28"), ("lan", None), ("cloud", None)])
    def test_an_explicit_run_of_a_disabled_leg_is_refused_and_runs_nothing(self, leg, fy):
        cfg = _with_leg(owner_config(), leg, enabled=False)
        with backup_env(cfg) as env:
            with pytest.raises(ValueError, match="disabled"):
                backup.fn(CONFIG_PATH, leg, fy=fy)
        assert env.calls == []

    def test_a_disabled_leg_with_an_fy_selected_in_mode_all_runs_only_the_enabled_leg(self):
        cfg = _with_leg(owner_config(), "lan", enabled=False)
        with backup_env(cfg) as env:
            backup.fn(CONFIG_PATH, "all", fy="FY27-28")
        assert order(env) == [("cloud", "FY27-28")]


# ── 4. activation: the next FY's folder cannot be made ───────────────────────────────────────────

class TestActivationWhenTheFolderCannotBeMade:
    def _fail(self):
        return patch.object(fy_overlap.fy_rollover, "create_source_folder", side_effect=OSError("disk is full"))

    def test_fails_closed_writes_no_record_and_mails_the_reason(self, cfg, db, fakes, source):
        with self._fail():
            assert _run(cfg, db, _at(2026, 12, 31)) == fy_overlap.FOLDER_FAILED
        assert db.overlap_record(TO) is None
        assert not _new_folder(source).exists()
        [event] = _events(fakes)
        assert event.key == NOT_ADDED_KEY and event.kind == "WARNING"
        assert "disk is full" in event.message and str(_new_folder(source)) in event.message
        assert "Until then only FY26-27 is backed up" in event.message

    def test_the_next_morning_it_asks_for_action_and_a_later_success_activates(self, cfg, db, fakes, source):
        with self._fail():
            _run(cfg, db, _at(2026, 12, 31))
            fakes.notify.reset_mock()
            assert _run(cfg, db, _at(2027, 1, 1)) == fy_overlap.FOLDER_FAILED
        [event] = _events(fakes)
        assert event.key == NOT_ADDED_KEY and event.kind == "ACTION_REQUIRED"
        assert db.overlap_record(TO) is None
        assert _run(cfg, db, _at(2027, 1, 2)) == fy_overlap.ACTIVATED      # the disk was freed
        assert db.overlap_record(TO) is not None and _new_folder(source).is_dir()

    def test_the_configured_fy_is_still_backed_up_and_nothing_is_blocked(self, cfg, db, fakes):
        with self._fail():
            _run(cfg, db, _at(2026, 12, 31))
        passes = fy_overlap.active_passes(cfg, db.overlap_record)
        assert [(p.fy, p.extra) for p in passes] == [("FY26-27", False)]
        assert db.open_approvals() == []


# ── 5. a final backup that raises during the April check ─────────────────────────────────────────

class _RaisingRig(_OverlapRig):
    """The legs named "RAISE" raise instead of returning a status (how a crashing final backup reaches the gate)."""

    def _leg(self, leg, status):
        if status != "RAISE":
            return super()._leg(leg, status)

        def run(config, run_id, started_at, monotonic_start=None, approval_id=None, **kwargs):
            raise RuntimeError("the NAS dropped off the network")
        return run


class TestAFinalBackupThatRaises:
    def test_the_nas_final_copy_is_not_complete_and_both_years_stay_active(self):
        with _RaisingRig(lan="RAISE") as r:
            with pytest.raises(fr.RolloverError, match="lan"):
                r.run()
            r.m["update_config_yaml"].assert_not_called()
            assert r.db.overlap_record("FY27-28").get("ended_at") is None
            reason = r.m["_open_block_request"].call_args.args[3]["results"][0]["reason"]
            assert "lan=LAN_NOT_COMPLETE" in reason
            assert r.metrics("ROLLOVER_BLOCKED")["final_copy"]["lan"]["outstanding"] is True

    def test_a_manual_copy_afterwards_lets_the_next_check_switch(self):
        with _RaisingRig(lan="RAISE") as r:
            with pytest.raises(fr.RolloverError):
                r.run()
            r.copy("lan", "2027-04-01T14:00:00+05:30", "LAN_COMPLETE")
            assert r.run() == "ROLLOVER_COMPLETED"
            r.m["update_config_yaml"].assert_called_once()
            assert r.db.overlap_record("FY27-28")["ended_at"]


# ── 6. verify-failed is not a final copy; the cloud hint ─────────────────────────────────────────

class TestVerifyFailedIsNotAFinalCopy:
    @pytest.mark.parametrize("fy_pass, config", [(None, CUR_CLOUD), (cur_pass(), CUR_CLOUD),
                                                 (next_pass(), NEXT_CLOUD)])
    def test_a_failed_verify_records_a_status_outside_the_final_copy_set(self, fy_pass, config):
        with cloud_env(verified=False) as env:
            with pytest.raises(RuntimeError, match="verification"):
                flow._run_cloud_pipeline(config, CLOUD_BASE, STARTED, time.monotonic(), fy_pass=fy_pass)
        status = env.record_run.call_args.args[4]
        assert status == "CLOUD_VERIFY_FAILED"
        assert status not in statuses.FINAL_COPY_STATUSES


class TestFinalCopyHints:
    def test_the_cloud_hint_names_the_cloud_card_of_the_closing_fy(self):
        assert fy_overlap._hint("cloud", FROM, False) == f"press 'Run backup' on the {FROM} cloud card"
        assert f"press 'Run backup' on the {FROM} cloud card" in fy_overlap._hint("cloud", FROM, True)
        assert "daily check retries" in fy_overlap._hint("cloud", FROM, True)
