"""v1.1.3 targeted remediation tests (NOT part of the 29-test v1.1.2 suite)."""
import threading

import pytest
from loguru import logger
from pydantic import ValidationError


def _base_paths(**over):
    d = {
        "source_drive": "E:\\FY26-27",
        "lan_destination": "\\\\srv\\share\\FY26-27",
        "runtime_dir": "C:\\BackupAgent",
        "gcs_key_path": "C:\\k.json",
    }
    d.update(over)
    return d


# A — logging bridge lifecycle
def test_a_reconfigure_resets_bridge(tmp_path):
    import core.logging as app_logging
    app_logging.configure(str(tmp_path))
    assert app_logging._bridge_configured is False
    app_logging.configure_prefect_bridge()
    assert app_logging._bridge_configured is True
    n1 = len(logger._core.handlers)
    app_logging.configure_prefect_bridge()  # idempotent: no duplicate sink
    assert len(logger._core.handlers) == n1
    app_logging.configure(str(tmp_path))  # second configure must reset
    assert app_logging._bridge_configured is False
    app_logging.configure_prefect_bridge()  # bridge functional again
    assert app_logging._bridge_configured is True
    assert len(logger._core.handlers) == n1


# E — GCP project fail-closed
def test_e_cloud_enabled_requires_project():
    from models.config import CloudConfig
    with pytest.raises(ValidationError):
        CloudConfig(enabled=True, bucket="bkt-abc", project_number="")
    with pytest.raises(ValidationError):
        CloudConfig(enabled=True, bucket="bkt-abc", project_number="   ")
    ok = CloudConfig(enabled=True, bucket="bkt-abc", project_number="123456")
    assert ok.project_number == "123456"
    off = CloudConfig(enabled=False, bucket="", project_number="")
    assert off.enabled is False


def test_e_no_hardcoded_default():
    from models.config import CloudConfig
    assert CloudConfig.model_fields["project_number"].default == ""


# F — LAN-only without GCS key
def test_f_lan_only_no_key_ok():
    from models.config import AppConfig
    cfg = AppConfig(paths=_base_paths(gcs_key_path=""),
                    cloud={"enabled": False, "bucket": "", "project_number": ""},
                    lan={"enabled": True},
                    wol={"enabled": False},
                    dashboard={"auth_enabled": False})
    assert cfg.paths.gcs_key_path == ""


def test_f_cloud_enabled_no_key_fails():
    from models.config import AppConfig
    with pytest.raises(ValidationError):
        AppConfig(paths=_base_paths(gcs_key_path=""),
                  cloud={"enabled": True, "bucket": "bkt-abc", "project_number": "123"},
                  lan={"enabled": False},
                  wol={"enabled": False},
                  dashboard={"auth_enabled": False})


def test_f_health_requires_key_for_cloud():
    from core.health import HealthError, pre_backup_health
    with pytest.raises(HealthError):
        pre_backup_health("C:\\nonexistent-xyz", mode="cloud", gcs_key_path="")


# G — unknown/corrupt lock fail-closed in cleanup paths
def test_g_launch_leaves_unknown_lock(tmp_path):
    from core.process import read_lock_alive
    lp = tmp_path / "backup.lock"
    lp.write_text("not-json{{{", encoding="utf-8")
    alive, pid = read_lock_alive(lp)
    assert (alive, pid) == (False, None)
    # launcher rule: only unlink when pid is not None
    if pid is not None:
        lp.unlink(missing_ok=True)
    assert lp.exists()  # fail-closed: left in place


def test_g_watchdog_leaves_unknown_lock(tmp_path):
    from core.process import read_lock_alive
    lp = tmp_path / "backup.lock"
    lp.write_text("", encoding="utf-8")
    alive, pid = read_lock_alive(lp)
    assert pid is None
    if pid is not None and not alive:
        lp.unlink(missing_ok=True)
    assert lp.exists()


def test_g_acquire_path_reaps_corrupt(tmp_path):
    from core.process import acquire_lock, read_lock_alive
    lp = tmp_path / "backup.lock"
    lp.write_text("garbage", encoding="utf-8")
    assert acquire_lock(lp) is True
    alive, _ = read_lock_alive(lp)
    assert alive is True


def test_g_never_kills_live(tmp_path):
    from pathlib import Path

    from core.process import acquire_lock
    lp = tmp_path / "backup.lock"
    assert acquire_lock(lp) is True
    assert acquire_lock(Path(str(lp))) is False  # live never overwritten


# D — dashboard supervision is observable-only
def test_d_supervisor_detects_dead_thread():
    from launch import _supervise_dashboard
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join(timeout=5)
    assert not dead.is_alive()
    mon = _supervise_dashboard(dead, interval_seconds=1)
    mon.join(timeout=10)
    assert not mon.is_alive()  # exits after logging, never restarts anything


# J — WoL whitespace
def test_j_wol_strips_whitespace():
    from models.config import WolConfig
    w = WolConfig(enabled=True, mac_address="  6C-4B-90-25-70-5F  ",
                  server_ip="  10.10.186.231  ", broadcast_address="  ")
    assert w.mac_address == "6C-4B-90-25-70-5F"
    assert w.server_ip == "10.10.186.231"
    assert w.broadcast_address == ""


def test_j_wol_still_rejects_malformed():
    from models.config import WolConfig
    with pytest.raises(ValidationError):
        WolConfig(enabled=True, mac_address="not-a-mac", server_ip="10.0.0.1")
    with pytest.raises(ValidationError):
        WolConfig(enabled=True, mac_address="6C-4B-90-25-70-5F", server_ip="999.1.1.1")


# K — timeout source of truth
def test_k_fallbacks_match_model():
    import inspect

    from core import cloud_reporter as cr
    from core import lan_preflight as lp
    from models.config import CloudConfig, LanConfig
    assert inspect.signature(cr.get_cloud_size).parameters["timeout"].default == CloudConfig.model_fields["cloud_size_timeout_seconds"].default == 300
    assert inspect.signature(cr.get_cloud_manifest).parameters["timeout"].default == CloudConfig.model_fields["manifest_timeout_seconds"].default == 900
    assert inspect.signature(cr.get_cloud_diff).parameters["timeout"].default == CloudConfig.model_fields["diff_timeout_seconds"].default == 1800
    assert inspect.signature(lp.run_lan_dry_run).parameters["timeout"].default == LanConfig.model_fields["dry_run_timeout_seconds"].default == 900
