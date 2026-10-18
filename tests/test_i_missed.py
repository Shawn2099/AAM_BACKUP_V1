"""Plan group I (I1, I2): a backup that starts late (server was off at the scheduled time) counts
its run budget from the SCHEDULED start; with no budget left it is recorded *_MISSED with one
WARNING email per leg, never run into office hours."""

from unittest.mock import MagicMock, patch

import pytest

import flow
from core import faults, statuses
from core.budget import RunBudget, late_seconds
from core.manifest import ManifestDB
from core.time_utils import now_iso
from models.config import BudgetConfig, MaintenanceConfig

H = 3600


# ---- I1: vocabulary ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["CLOUD_MISSED", "LAN_MISSED"])
def test_missed_status_vocabulary(status):
    assert not statuses.is_success(status)
    assert statuses.category(status) == statuses.CATEGORY_FAILED
    assert statuses.label(status) == "Missed"
    assert "off" in statuses.describe(status).lower()
    assert status not in statuses.CLOUD_SUCCESS_STATUSES | statuses.LAN_SUCCESS_STATUSES


def test_missed_fault_code_is_a_warning_with_steps_and_classified_from_the_status():
    fault = faults.get("AAM-SYS-07")
    assert fault.code == "AAM-SYS-07" and fault.severity == faults.WARNING and fault.steps
    assert faults.classify_run("lan", "LAN_MISSED") == "AAM-SYS-07"
    assert faults.classify_run("cloud", "CLOUD_MISSED") == "AAM-SYS-07"


# ---- I2: budget from the scheduled start ------------------------------------------------------

def test_late_seconds_is_zero_for_an_on_time_or_future_start():
    assert late_seconds(1000.0, 1000.0) == 0.0
    assert late_seconds(1000.0, 990.0) == 0.0


def test_late_seconds_counts_the_delay():
    assert late_seconds(1000.0, 1000.0 + 2 * H) == 2 * H


def test_run_budget_started_earlier_has_less_left():
    clock = lambda: 10 * H  # noqa: E731
    on_time = RunBudget("lan", 4 * H, 10 * H)
    late = RunBudget("lan", 4 * H, 10 * H - 3 * H)
    assert on_time.remaining(clock) == 4 * H
    assert late.remaining(clock) == 1 * H


def _flow_run(auto_scheduled, lateness_s):
    import pendulum
    expected = pendulum.now("UTC").subtract(seconds=lateness_s)
    return MagicMock(auto_scheduled=auto_scheduled, expected_start_time=expected)


def test_scheduled_flow_run_reports_how_late_it_started():
    ctx = MagicMock(flow_run=_flow_run(True, 2 * H))
    with patch("prefect.context.FlowRunContext.get", return_value=ctx):
        assert flow._scheduled_lateness_seconds() == pytest.approx(2 * H, abs=5)


def test_manual_flow_run_counts_from_its_actual_start():
    ctx = MagicMock(flow_run=_flow_run(False, 2 * H))
    with patch("prefect.context.FlowRunContext.get", return_value=ctx):
        assert flow._scheduled_lateness_seconds() == 0.0


def test_no_flow_context_means_no_lateness():
    with patch("prefect.context.FlowRunContext.get", return_value=None):
        assert flow._scheduled_lateness_seconds() == 0.0


def _cfg(tmp_path):
    cfg = MagicMock()
    cfg.paths.database_path = str(tmp_path / "m.db")
    cfg.maintenance = MaintenanceConfig()
    cfg.budget = BudgetConfig()
    return cfg


@pytest.mark.parametrize("leg,pipeline", [("lan", "_run_lan_pipeline"),
                                          ("cloud", "_run_cloud_pipeline")])
def test_no_budget_left_records_missed_and_one_warning_without_taking_the_slot(
        tmp_path, leg, pipeline):
    cfg = _cfg(tmp_path)
    cfg.cloud.enabled = cfg.lan.enabled = True
    max_h = getattr(cfg.budget, f"{leg}_max_hours")
    with patch("flow._backup_slot") as slot, patch("flow.notify") as notify:
        with pytest.raises(flow.PartialRun, match="MISSED"):
            getattr(flow, pipeline)(cfg, f"run-{leg}", now_iso(), None, late_seconds=(max_h + 1) * H)
    slot.assert_not_called()
    db = ManifestDB(cfg.paths.database_path)
    try:
        row = db.last_run(leg)
    finally:
        db.close()
    assert row["status"] == f"{leg.upper()}_MISSED" and row["ended_at"]
    assert "scheduled" in row["error_message"].lower()
    [(_, event), _kw] = notify.call_args_list[0]
    assert notify.call_count == 1
    assert event.key == f"missed:{leg}" and event.kind == "WARNING"
    assert event.fault_code == "AAM-SYS-07" and event.status == f"{leg.upper()}_MISSED"


def test_a_late_start_with_some_budget_left_still_runs(tmp_path):
    cfg = _cfg(tmp_path)
    max_h = cfg.budget.lan_max_hours
    with patch("flow._pipeline_slot") as slot, patch("flow.RunBudget", wraps=RunBudget) as rb, \
         patch("flow.notify") as notify:
        slot.side_effect = RuntimeError("stop here - the slot was reached")
        with pytest.raises(RuntimeError, match="slot was reached"):
            flow._run_lan_pipeline(cfg, "run-late", now_iso(), None, late_seconds=(max_h - 1) * H)
    notify.assert_not_called()
    budget = rb.call_args.args[2]
    assert RunBudget("lan", max_h * H, budget).remaining() == pytest.approx(1 * H, abs=5)


def test_manual_budget_override_with_no_lateness_is_not_affected(tmp_path):
    cfg = _cfg(tmp_path)
    with patch("flow._pipeline_slot") as slot, patch("flow.notify") as notify:
        slot.side_effect = RuntimeError("slot reached")
        with pytest.raises(RuntimeError, match="slot reached"):
            flow._run_lan_pipeline(cfg, "run-man", now_iso(), None, budget_hours=1.0)
    notify.assert_not_called()


def test_backup_flow_passes_the_lateness_to_the_first_leg_only(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.cloud.enabled = cfg.lan.enabled = True
    cfg.firm_name = "x"
    with patch("flow.load_config", return_value=cfg), patch("flow.configure_logging"), \
         patch("flow.configure_prefect_bridge"), patch("flow.cleanup_orphaned_robocopy_logs"), \
         patch("flow._scheduled_lateness_seconds", return_value=7200.0), \
         patch("flow._run_cloud_pipeline") as cloud, patch("flow._run_lan_pipeline") as lan, \
         patch("flow.ManifestDB"):
        flow.backup.fn(config_path="x", mode="all")
    assert cloud.call_args.kwargs["late_seconds"] == 7200.0
    assert lan.call_args.kwargs["late_seconds"] == 0.0
