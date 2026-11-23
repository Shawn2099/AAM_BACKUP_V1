"""B2 (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, sections 2.3 and 9): the cloud and LAN pipelines take one FY pass.

`fy_pass=None` is the baseline call (no FY kwargs, no FY metrics, no new keys). A pass for the NEXT FY uses its own
config (paths moved by `fy_overlap.config_for_fy`), suffixed run id, FY-tagged row, own artifact key and alert keys,
writes no file_entries rows and never uses the other FY's approval. The tasks are mocks, as in
tests/test_a2_interrupted_run_status.py; the NAS folder rule runs for real on temp folders that stand in for the
owner's share (`\\\\nas\\backup` -> `<tmp>\\nas\\backup`).
"""

import contextlib
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import flow
from core import fy_overlap, guards
from core.guards import MOUNT_MARKER

CLOUD_BASE = "flowrun-7-cloud"
LAN_BASE = "flowrun-7-lan"
STARTED = "2027-01-10T02:00:00"


def make_config(source, lan_destination, *, hours=1.0, reserve_minutes=10):
    cfg = MagicMock()
    cfg.firm_name = "TestCA"
    cfg.paths.database_path = ":memory:"
    cfg.paths.source_drive = source
    cfg.paths.lan_destination = lan_destination
    cfg.paths.gcs_key_path = "/tmp/key.json"
    cfg.budget.cloud_max_hours = hours
    cfg.budget.cloud_verify_reserve_minutes = reserve_minutes
    cfg.budget.lan_max_hours = hours
    cfg.budget.lan_post_reserve_minutes = reserve_minutes
    for leg in (cfg.cloud, cfg.lan):
        leg.max_attempts = 1
        leg.retry_delay_seconds = 0
        leg.preflight_retries = 0
        leg.preflight_retry_delay_seconds = 0
        leg.verify_retries = 0
        leg.verify_retry_delay_seconds = 0
    cfg.lan.shutdown_on_all_retries_exhausted = False
    return cfg


CUR_CLOUD = make_config("E:\\FY26-27", "\\\\nas\\backup\\FY26-27")
NEXT_CLOUD = make_config("E:\\FY27-28", "\\\\nas\\backup\\FY27-28")


def cur_pass(cfg=CUR_CLOUD):
    return fy_overlap.FyPass(fy="FY26-27", extra=False, config=cfg)


def next_pass(cfg=NEXT_CLOUD):
    return fy_overlap.FyPass(fy="FY27-28", extra=True, config=cfg)


def _patcher(st):
    def p(target, **kw):
        return st.enter_context(patch(target, **kw))
    return p


def _common_db(p, env):
    env.db = p("flow.ManifestDB")
    env.db.from_config.return_value = env.db.return_value
    env.db.return_value.get_cloud_synced_entries.return_value = {}
    env.db.return_value.previous_run_status.return_value = None
    env.db.return_value.previous_run_fault_code.return_value = None
    env.db.return_value.get_approval.return_value = None
    env.notify = p("flow.notify")
    env.record_run = p("flow._record_run")
    env.record_running = p("flow._record_running")
    env.artefacts = p("flow._open_run_artefacts", return_value=None)
    p("flow._backup_slot", side_effect=lambda config: contextlib.nullcontext())


@contextlib.contextmanager
def cloud_env(*, verified=True, sync_status="CLOUD_COMPLETE"):
    result = {"verified": verified, "verify_termination": "normal",
              "diff": {"added": [], "modified": [], "removed": []},
              "size": {"count": 1, "bytes": 1}, "manifest": []}
    with contextlib.ExitStack() as st:
        p = _patcher(st)
        env = SimpleNamespace()
        _common_db(p, env)
        p("flow.health_check_task")
        p("flow.cloud_preflight_task")
        env.access = p("flow.cloud_access_task", new=MagicMock())
        env.guards = p("flow.cloud_guards_task", new=MagicMock(return_value={"max_delete": 10}))
        env.sync = p("flow.cloud_sync_task")
        env.sync.with_options.return_value.return_value = {
            "status": sync_status, "exit_code": 0, "error": None}
        env.verify = p("flow.cloud_verify_and_report_task")
        env.verify.with_options.return_value.return_value = result
        env.record_task = p("flow.cloud_record_task")
        env.publish = p("flow.cloud_publish_artifact_task")
        yield env


@contextlib.contextmanager
def lan_env():
    with contextlib.ExitStack() as st:
        p = _patcher(st)
        env = SimpleNamespace(order=[])
        _common_db(p, env)
        env.wol = p("flow.wol_check_task", side_effect=lambda config: env.order.append("wol"))
        p("flow.health_check_task")
        env.preflight = p("flow.lan_preflight_task")
        env.preflight.with_options.return_value.side_effect = (
            lambda config, artefacts_dir: (env.order.append("preflight"), {})[1])
        env.before = p("flow.lan_snapshot_before_task", return_value={})
        env.after = p("flow.lan_snapshot_after_task", return_value={"a.txt": (100, 1.0)})
        env.guards = p("flow.lan_guards_task", new=MagicMock())
        p("flow.lan_access_task", new=MagicMock())
        env.sync = p("flow.lan_sync_task")
        env.sync.with_options.return_value.return_value = {
            "status": "LAN_COMPLETE", "exit_code": 1, "files_failed": 0,
            "counts": {"copied": 1}, "bytes": {"copied": 100}, "files_deleted": 0}
        env.record_task = p("flow.lan_record_task")
        env.publish = p("flow.lan_publish_artifact_task")
        env.shutdown = p("flow.lan_shutdown_task")
        env.complete_pending = p("flow._complete_pending_rollover_nas_folder")
        yield env


def events(env):
    return [c.args[1] for c in env.notify.call_args_list]


def metrics(env):
    return json.loads(env.record_run.call_args.kwargs["extended_metrics"])


# ── cloud: next-FY pass ──────────────────────────────────────────────────────────────────────────

class TestCloudNextPass:
    def test_uses_the_next_fy_folder_and_prefix(self):
        with cloud_env() as env:
            flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                     fy_pass=next_pass())
        sync_args = env.sync.with_options.return_value.call_args
        assert sync_args is not None
        assert sync_args.args[0] is NEXT_CLOUD
        assert sync_args.args[1] == "FY27-28"
        assert env.verify.with_options.return_value.call_args.args[1] == "FY27-28"
        assert env.guards.call_args.args[1] == "FY27-28"

    def test_run_id_is_suffixed_and_the_row_is_tagged(self):
        with cloud_env() as env:
            flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                     fy_pass=next_pass())
        run_id = CLOUD_BASE + "-FY27-28"
        assert env.record_run.call_args.args[1] == run_id
        assert env.record_run.call_args.args[2] == "cloud"
        assert env.record_run.call_args.args[4] == "CLOUD_COMPLETE"
        assert env.record_running.call_args.args[2] == run_id
        assert env.artefacts.call_args.args[1] == run_id
        m = metrics(env)
        assert m["fy"] == "FY27-28" and m["overlap"] is True
        assert m["cross_check"] == "skipped (overlap pass)"
        assert m["metrics_consistent"] is None

    def test_writes_no_file_entries(self):
        with cloud_env() as env:
            flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                     fy_pass=next_pass())
        env.record_task.assert_not_called()
        env.db.return_value.get_cloud_synced_entries.assert_not_called()

    def test_artifact_key_is_its_own(self):
        with cloud_env() as env:
            flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                     fy_pass=next_pass())
        assert env.publish.call_args.kwargs["key"] == "cloud-backup-summary-fy27-28"

    def test_failure_alert_has_its_own_key_group_and_title(self):
        with cloud_env(verified=False) as env:
            with pytest.raises(RuntimeError):
                flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                         fy_pass=next_pass())
        [ev] = events(env)
        assert ev.key == "backup:cloud:FY27-28:CLOUD_VERIFY_FAILED"
        assert ev.fy == "FY27-28"
        assert ev.title.startswith("CLOUD (FY27-28) backup")

    def test_recovery_is_checked_against_its_own_fy(self):
        with cloud_env() as env:
            env.db.return_value.previous_run_status.return_value = "CLOUD_FAILED"
            flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                     fy_pass=next_pass())
        assert env.db.return_value.previous_run_status.call_args.kwargs == {"fy": "FY27-28"}
        [ev] = events(env)
        assert ev.key == "recovered:cloud:FY27-28" and ev.fy == "FY27-28"

    def test_other_fy_approval_is_not_offered(self):
        with cloud_env() as env:
            env.db.return_value.get_approval.return_value = {"leg": "cloud", "fy": "FY26-27"}
            flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                     approval_id="ap-1", fy_pass=next_pass())
        assert env.guards.call_args.kwargs["approval_id"] is None

    def test_approval_of_its_own_fy_is_offered(self):
        with cloud_env() as env:
            env.db.return_value.get_approval.return_value = {"leg": "cloud", "fy": "FY27-28"}
            flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                     approval_id="ap-1", fy_pass=next_pass())
        assert env.guards.call_args.kwargs["approval_id"] == "ap-1"

    def test_no_budget_left_is_cloud_missed_with_its_own_key(self):
        budget_start = time.monotonic() - 3601   # the leg's one hour is used up before this pass starts
        with cloud_env() as env:
            with pytest.raises(flow.PartialRun):
                flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                         budget_start=budget_start, fy_pass=next_pass())
        assert env.record_run.call_args.args[1] == CLOUD_BASE + "-FY27-28"
        assert env.record_run.call_args.args[4] == "CLOUD_MISSED"
        assert not env.sync.with_options.return_value.called
        [ev] = events(env)
        assert ev.key == "missed:cloud:FY27-28" and ev.fy == "FY27-28"


# ── cloud: configured-FY pass while the overlap is active ──────────────────────────────────────────

class TestCloudConfiguredPassWithOverlap:
    def test_keeps_run_id_entries_and_metrics_of_the_baseline(self):
        with cloud_env() as env:
            flow._run_cloud_pipeline(CUR_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                     fy_pass=cur_pass())
        assert env.record_run.call_args.args[1] == CLOUD_BASE
        m = metrics(env)
        assert m["fy"] == "FY26-27"
        assert "overlap" not in m and "cross_check" not in m
        env.record_task.assert_called_once()
        assert "key" not in env.publish.call_args.kwargs

    def test_alert_key_unchanged_title_names_the_fy(self):
        with cloud_env(verified=False) as env:
            with pytest.raises(RuntimeError):
                flow._run_cloud_pipeline(CUR_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                         fy_pass=cur_pass())
        [ev] = events(env)
        assert ev.key == "backup:cloud:CLOUD_VERIFY_FAILED"
        assert ev.fy is None
        assert ev.title.startswith("CLOUD (FY26-27) backup")


class TestBaselineCall:
    def test_no_pass_is_todays_call(self):
        with cloud_env(verified=False) as env:
            with pytest.raises(RuntimeError):
                flow._run_cloud_pipeline(CUR_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                         approval_id="ap-1")
        assert env.record_run.call_args.args[1] == CLOUD_BASE
        assert "fy" not in metrics(env)
        assert env.guards.call_args.kwargs["approval_id"] == "ap-1"
        [ev] = events(env)
        assert ev.key == "backup:cloud:CLOUD_VERIFY_FAILED"
        assert ev.title.startswith("CLOUD backup ") and ev.fy is None

    def test_no_pass_writes_entries_and_cross_checks(self):
        with cloud_env() as env:
            flow._run_cloud_pipeline(CUR_CLOUD, CLOUD_BASE, STARTED, time.monotonic())
        env.record_task.assert_called_once()
        env.db.return_value.get_cloud_synced_entries.assert_called_once()
        assert metrics(env)["metrics_consistent"] is False   # rclone reported no count: cross-check ran
        assert "cross_check" not in metrics(env)
        assert "key" not in env.publish.call_args.kwargs


# ── LAN ──────────────────────────────────────────────────────────────────────────────────────────

class TestLanNextPass:
    def test_creates_the_nas_folder_after_wol_and_uses_its_folder(self, tmp_path):
        nas_root = tmp_path / "nas" / "backup"
        nas_root.mkdir(parents=True)
        next_folder = nas_root / "FY27-28"
        cfg = make_config("E:\\FY27-28", str(next_folder))
        with lan_env() as env:
            env.preflight.with_options.return_value.side_effect = (
                lambda config, artefacts_dir: (
                    env.order.append("preflight"),
                    {"checked_folder": (next_folder / MOUNT_MARKER).is_file()})[1])
            flow._run_lan_pipeline(cfg, LAN_BASE, STARTED, time.monotonic(),
                                   allow_shutdown=False, fy_pass=next_pass(cfg))
        assert env.order == ["wol", "preflight"]
        assert (next_folder / MOUNT_MARKER).is_file()
        assert env.sync.with_options.return_value.call_args.args[0] is cfg
        assert env.record_run.call_args.args[1] == LAN_BASE + "-FY27-28"
        assert env.record_run.call_args.args[4] == "LAN_COMPLETE"

    def test_unreachable_nas_share_is_lan_failed_aam_lan_01(self, tmp_path):
        cfg = make_config("E:\\FY27-28", str(tmp_path / "missing" / "backup" / "FY27-28"))
        with lan_env() as env:
            with pytest.raises(RuntimeError):
                flow._run_lan_pipeline(cfg, LAN_BASE, STARTED, time.monotonic(),
                                       allow_shutdown=False, fy_pass=next_pass(cfg))
        assert env.record_run.call_args.args[4] == "LAN_FAILED"
        assert metrics(env)["fault_code"] == "AAM-LAN-01"
        assert not env.sync.with_options.return_value.called
        [ev] = events(env)
        assert ev.key == "backup:lan:FY27-28:LAN_FAILED"
        assert ev.fy == "FY27-28" and ev.fault_code == "AAM-LAN-01"

    def test_destination_of_another_year_is_refused(self, tmp_path):
        nas_root = tmp_path / "nas" / "backup"
        nas_root.mkdir(parents=True)
        cfg = make_config("E:\\FY27-28", str(nas_root / "Archive"))
        with lan_env() as env:
            with pytest.raises(RuntimeError):
                flow._run_lan_pipeline(cfg, LAN_BASE, STARTED, time.monotonic(),
                                       allow_shutdown=False, fy_pass=next_pass(cfg))
        assert not env.sync.with_options.return_value.called
        assert not (nas_root / "FY27-28").exists()
        assert env.record_run.call_args.args[4] == "LAN_FAILED"

    def test_next_pass_writes_no_file_entries(self, tmp_path):
        nas_root = tmp_path / "nas" / "backup"
        nas_root.mkdir(parents=True)
        cfg = make_config("E:\\FY27-28", str(nas_root / "FY27-28"))
        with lan_env() as env:
            flow._run_lan_pipeline(cfg, LAN_BASE, STARTED, time.monotonic(),
                                   allow_shutdown=False, fy_pass=next_pass(cfg))
        env.record_task.assert_not_called()
        assert metrics(env)["fy"] == "FY27-28" and metrics(env)["overlap"] is True

    def test_artifact_key_is_its_own(self, tmp_path):
        nas_root = tmp_path / "nas" / "backup"
        nas_root.mkdir(parents=True)
        cfg = make_config("E:\\FY27-28", str(nas_root / "FY27-28"))
        with lan_env() as env:
            flow._run_lan_pipeline(cfg, LAN_BASE, STARTED, time.monotonic(),
                                   allow_shutdown=False, fy_pass=next_pass(cfg))
        assert env.publish.call_args.kwargs["key"] == "lan-backup-summary-fy27-28"

    def test_missing_nas_folder_step_is_not_the_rollover_step(self, tmp_path):
        nas_root = tmp_path / "nas" / "backup"
        nas_root.mkdir(parents=True)
        cfg = make_config("E:\\FY27-28", str(nas_root / "FY27-28"))
        with lan_env() as env, patch("core.fy_overlap.ensure_nas_folder", return_value="ready") as ensure:
            flow._run_lan_pipeline(cfg, LAN_BASE, STARTED, time.monotonic(),
                                   allow_shutdown=False, fy_pass=next_pass(cfg))
        ensure.assert_called_once_with(str(nas_root), "FY27-28")
        env.complete_pending.assert_not_called()

    def test_no_budget_left_is_lan_missed_with_its_own_key(self, tmp_path):
        nas_root = tmp_path / "nas" / "backup"
        nas_root.mkdir(parents=True)
        cfg = make_config("E:\\FY27-28", str(nas_root / "FY27-28"), hours=1.0)
        budget_start = time.monotonic() - 3601
        with lan_env() as env:
            with pytest.raises(flow.PartialRun):
                flow._run_lan_pipeline(cfg, LAN_BASE, STARTED, time.monotonic(),
                                       budget_start=budget_start, allow_shutdown=False,
                                       fy_pass=next_pass(cfg))
        assert env.record_run.call_args.args[4] == "LAN_MISSED"
        assert not env.wol.called
        [ev] = events(env)
        assert ev.key == "missed:lan:FY27-28" and ev.fy == "FY27-28"


class TestLanConfiguredAndBaseline:
    def test_configured_pass_keeps_the_rollover_step_and_entries(self, tmp_path):
        nas_root = tmp_path / "nas" / "backup"
        nas_root.mkdir(parents=True)
        cur = make_config("E:\\FY26-27", str(nas_root / "FY26-27"))
        with lan_env() as env, patch("core.fy_overlap.ensure_nas_folder") as ensure:
            flow._run_lan_pipeline(cur, LAN_BASE, STARTED, time.monotonic(),
                                   allow_shutdown=False, fy_pass=cur_pass(cur))
        ensure.assert_not_called()
        env.complete_pending.assert_called_once()
        env.record_task.assert_called_once()
        assert env.record_run.call_args.args[1] == LAN_BASE
        assert "key" not in env.publish.call_args.kwargs
        assert metrics(env)["fy"] == "FY26-27" and "overlap" not in metrics(env)

    def test_no_pass_is_todays_call(self, tmp_path):
        nas_root = tmp_path / "nas" / "backup"
        nas_root.mkdir(parents=True)
        cur = make_config("E:\\FY26-27", str(nas_root / "FY26-27"))
        with lan_env() as env:
            flow._run_lan_pipeline(cur, LAN_BASE, STARTED, time.monotonic(), allow_shutdown=False)
        env.complete_pending.assert_called_once()
        env.record_task.assert_called_once()
        assert env.record_run.call_args.args[1] == LAN_BASE
        assert "fy" not in metrics(env)
        assert "key" not in env.publish.call_args.kwargs


# ── helpers used by the passes ───────────────────────────────────────────────────────────────────

class TestPassHelpers:
    def test_fy_pass_is_a_plain_frozen_record(self):
        p = fy_overlap.FyPass(fy="FY27-28", extra=True, config=NEXT_CLOUD)
        assert (p.fy, p.extra, p.config) == ("FY27-28", True, NEXT_CLOUD)
        with pytest.raises(Exception):
            p.fy = "FY28-29"

    def test_prev_fy_is_the_inverse_of_next_fy(self):
        assert fy_overlap.prev_fy("FY27-28") == "FY26-27"
        assert fy_overlap.prev_fy("FY00-01") == "FY99-00"
        for fy in ("FY26-27", "FY99-00", "FY00-01"):
            assert fy_overlap.prev_fy(fy_overlap.next_fy(fy)) == fy

    def test_ensure_pass_nas_folder_creates_folder_and_marker(self, tmp_path):
        nas_root = tmp_path / "backup"
        nas_root.mkdir()
        cfg = make_config("E:\\FY27-28", str(nas_root / "FY27-28"))
        assert fy_overlap.ensure_pass_nas_folder(cfg, "FY27-28") == fy_overlap.NAS_CREATED
        assert (nas_root / "FY27-28" / MOUNT_MARKER).is_file()

    def test_ensure_pass_nas_folder_unreachable_parent_raises(self, tmp_path):
        cfg = make_config("E:\\FY27-28", str(tmp_path / "missing" / "FY27-28"))
        with pytest.raises(OSError):
            fy_overlap.ensure_pass_nas_folder(cfg, "FY27-28")

    def test_ensure_pass_nas_folder_refuses_another_year_leaf(self, tmp_path):
        nas_root = tmp_path / "backup"
        nas_root.mkdir()
        cfg = make_config("E:\\FY27-28", str(nas_root / "FY26-27"))
        with pytest.raises(ValueError):
            fy_overlap.ensure_pass_nas_folder(cfg, "FY27-28")
        assert not (nas_root / "FY27-28").exists()


class TestBlockRequestOfAPass:
    def test_supersedes_only_its_own_fy_and_names_it(self):
        guard_record = {"fy": "FY27-28", "approved_guards": [],
                        "results": [{"verdict": guards.BLOCK, "guard": "G2_SOURCE_SHRINK",
                                     "reason": "shrunk", "metrics": {}}]}
        with patch("flow.ManifestDB") as db, patch("flow.notify") as notify:
            db.from_config.return_value = db.return_value
            db.return_value.open_approval_request.return_value = "ap-9"
            flow._open_block_request(NEXT_CLOUD, "cloud", CLOUD_BASE + "-FY27-28", guard_record,
                                     STARTED, None, keep_fy="FY26-27", fy="FY27-28",
                                     title_fy="FY27-28")
        kw = db.return_value.open_approval_request.call_args.kwargs
        assert kw["fy"] == "FY27-28" and kw["keep_fy"] == "FY26-27"
        ev = notify.call_args.args[1]
        assert ev.key == "blocked:cloud:FY27-28" and ev.fy == "FY27-28"
        assert ev.title == "CLOUD (FY27-28) backup blocked - approval needed"


# ── MISSED and SLOT_TIMEOUT rows carry the pass's FY (review F5) ───────────────────────────────────

class TestRowsThatNeverStartCarryTheFy:
    def _row_metrics(self, env):
        raw = env.record_run.call_args.kwargs.get("extended_metrics")
        assert raw, "the row has no extended_metrics"
        return json.loads(raw)

    def test_cloud_missed_row_of_the_next_fy(self):
        with cloud_env() as env:
            with pytest.raises(flow.PartialRun):
                flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                         budget_start=time.monotonic() - 3601, fy_pass=next_pass())
        assert self._row_metrics(env)["fy"] == "FY27-28"
        assert self._row_metrics(env)["overlap"] is True

    def test_lan_missed_row_of_the_next_fy(self, tmp_path):
        cfg = make_config("E:\\FY27-28", str(tmp_path / "FY27-28"))
        with lan_env() as env:
            with pytest.raises(flow.PartialRun):
                flow._run_lan_pipeline(cfg, LAN_BASE, STARTED, time.monotonic(),
                                       budget_start=time.monotonic() - 3601, allow_shutdown=False,
                                       fy_pass=next_pass(cfg))
        assert self._row_metrics(env)["fy"] == "FY27-28"
        assert self._row_metrics(env)["overlap"] is True

    def test_cloud_slot_timeout_row_of_the_next_fy(self):
        with cloud_env() as env, patch("flow._backup_slot", side_effect=TimeoutError("slot wait ran out")):
            with pytest.raises(TimeoutError):
                flow._run_cloud_pipeline(NEXT_CLOUD, CLOUD_BASE, STARTED, time.monotonic(),
                                         fy_pass=next_pass())
        assert env.record_run.call_args.args[4] == "CLOUD_SLOT_TIMEOUT"
        assert self._row_metrics(env)["fy"] == "FY27-28"
        assert self._row_metrics(env)["overlap"] is True

    def test_baseline_rows_stay_without_metrics(self):
        with cloud_env() as env, patch("flow._backup_slot", side_effect=TimeoutError("slot wait ran out")):
            with pytest.raises(TimeoutError):
                flow._run_cloud_pipeline(CUR_CLOUD, CLOUD_BASE, STARTED, time.monotonic())
        assert env.record_run.call_args.kwargs.get("extended_metrics") is None
