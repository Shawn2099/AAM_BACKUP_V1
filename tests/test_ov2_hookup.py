"""OV-2.7 - the activation runs at the start of every backup flow run and of the daily rollover check;
whichever comes first activates, the other is a no-op. OV-5.1 wiring - flow hands the small-folder keys
to every G2 / G3 / backstop call."""
import inspect
from unittest.mock import MagicMock, patch

import pendulum
import pytest

import flow
from core import fy_overlap as ov
from core.manifest import ManifestDB
from tests.test_ov2_activation import RUN_DATE, T0, Clock, Mail, env, frozen  # noqa: F401 (fixtures)
from tests.test_ov2_fy_overlap import NEXT


def test_whichever_flow_runs_first_activates_and_the_other_is_a_no_op(env, frozen, monkeypatch):  # noqa: F811
    mail, clock = Mail(), Clock()
    real = ov.ensure_overlap_activated
    monkeypatch.setattr(ov, "ensure_overlap_activated",
                        lambda cfg, d, db=None: real(cfg, d, db, clock_check=clock, notify_fn=mail))
    monkeypatch.setattr(flow, "_scheduled_time", lambda: pendulum.datetime(2027, 1, 1, 2, 1, tz="Asia/Kolkata"))

    flow._activate_overlap(env.cfg, env.db)          # e.g. the 02:01 cloud run
    flow._activate_overlap(env.cfg, env.db)          # then the 06:00 rollover check
    flow._activate_overlap(env.cfg, env.db)          # then the 21:01 LAN run

    assert env.db.get_activation(NEXT) is not None
    assert mail.keys() == [f"overlap:start:{NEXT}"]  # one activation, one mail
    assert clock.calls == 1                          # later calls stop at the activation row


def test_activation_never_raises_into_a_backup(monkeypatch):
    monkeypatch.setattr(ov, "ensure_overlap_activated", MagicMock(side_effect=RuntimeError("boom")))
    flow._activate_overlap(MagicMock())              # must not raise


@patch("flow.load_config")
@patch("flow.configure_logging")
@patch("flow.configure_prefect_bridge")
@patch("flow.cleanup_orphaned_robocopy_logs")
@patch("flow.ManifestDB")
@patch("flow._run_lan_pipeline")
@patch("flow._run_cloud_pipeline")
@patch("flow._activate_overlap")
def test_every_backup_flow_run_starts_with_the_activation(mock_act, mock_cloud, mock_lan, mock_db, mock_clean,
                                                          mock_bridge, mock_log, mock_cfg):
    calls = []
    mock_act.side_effect = lambda *a, **k: calls.append("activate")
    mock_cloud.side_effect = lambda *a, **k: calls.append("cloud")
    mock_lan.side_effect = lambda *a, **k: calls.append("lan")
    cfg = MagicMock()
    cfg.cloud.enabled = cfg.lan.enabled = True
    mock_cfg.return_value = cfg
    flow.backup.fn("config.yaml", "all")
    assert calls == ["activate", "cloud", "lan"]


def test_the_rollover_check_activates_before_the_sibling_alarm(monkeypatch):
    calls = []
    cfg = MagicMock()
    monkeypatch.setattr(flow, "load_config", lambda p: cfg)
    monkeypatch.setattr(flow, "log_retention_task", MagicMock())
    monkeypatch.setattr(flow, "ManifestDB", MagicMock())
    monkeypatch.setattr(flow, "_activate_overlap", lambda c, db=None: calls.append("activate"))
    monkeypatch.setattr(flow, "_alarm_siblings", lambda c, db, d: calls.append("siblings"))
    monkeypatch.setattr(flow, "_rollover_resume", lambda *a, **k: calls.append("resume") or "NO_ROLLOVER_NEEDED")
    assert flow.rollover_check_flow.fn("config.yaml") == "NO_ROLLOVER_NEEDED"
    assert calls == ["activate", "siblings", "resume"]


def test_flow_passes_the_small_folder_keys_to_every_g2_g3_and_backstop_call():
    src = inspect.getsource(flow)
    assert src.count("small_folder_files=limits.small_folder_files") == 4      # G2 + G3, cloud and LAN
    assert src.count("small_folder_max_deletes=limits.small_folder_max_deletes") == 4
    assert "limits.small_folder_files, limits.small_folder_max_deletes)" in src   # the cloud --max-delete
