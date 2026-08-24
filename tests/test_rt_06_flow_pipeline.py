import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from core.manifest import ManifestDB
from core.process import read_lock_alive
from core.time_utils import now_iso
from flow import _run_cloud_pipeline, _run_lan_pipeline
from tests.e2e_helpers import (
    cfg,
    clean_test_dirs,
    make_file,
    nas_test_dir,
    source_test_dir,
)


@pytest.fixture(scope="module", autouse=True)
def setup_teardown_pipeline():
    """Setup and teardown for pipeline tests."""
    clean_test_dirs()
    
    source = source_test_dir()
    dest = nas_test_dir()
    
    source.mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".AAM_TARGET_MOUNTED").touch()
    
    # Create 5 test files
    for i in range(5):
        make_file(source / f"test_{i}.txt", 1024)
        
    yield
    
    clean_test_dirs()
    
    # Purge from cloud
    config = cfg()
    import subprocess

    from core.process import resolve_binary
    from core.rclone_config import temp_rclone_config
    
    with temp_rclone_config(
        config.paths.gcs_key_path,
        config.cloud.location,
        config.cloud.project_number,
        config.cloud.storage_class,
    ) as config_path:
        rclone_exe = resolve_binary("rclone") or "rclone"
        subprocess.run([
            rclone_exe, "purge",
            f"aam_gcs:{config.cloud.bucket}/E2E_TEST_FY",
            "--config", config_path
        ], check=False)


@pytest.fixture
def temp_config_and_db(tmp_path):
    """Provide a patched config with temp DB and E2E paths."""
    config = cfg()
    
    # Patch paths
    config.paths.source_drive = str(source_test_dir())
    config.paths.lan_destination = str(nas_test_dir())
    config.paths.database_path = str(tmp_path / "test_manifest.db")
    config.lan.shutdown_after_backup = False
    
    return config


import os as _gate_os
import sys as _gate_sys

# F4/F5: real-hardware acceptance suite — skipped on dev/CI machines:
#   * Windows-only (robocopy, NSSM, sc, msvcrt, production Windows paths)
#   * requires the production deployment (source drive, NAS, GCS key)
# Run it on the production server with:  set AAM_RUN_REAL_HARDWARE=1
pytestmark = [
    pytest.mark.skipif(_gate_sys.platform != "win32",
                       reason="F5: Windows-only real-hardware acceptance test"),
    pytest.mark.skipif(_gate_os.environ.get("AAM_RUN_REAL_HARDWARE") != "1",
                       reason="F4: real-hardware test — set AAM_RUN_REAL_HARDWARE=1 on the production server"),
]


def test_pipe_01_cloud_pipeline(temp_config_and_db):
    """PIPE-01: Cloud Pipeline — Golden Path, All Steps Record to DB."""
    config = temp_config_and_db
    
    # Patch the get_fy_prefix used internally by flow.py
    import core.fy_router
    orig_prefix = core.fy_router.get_fy_prefix
    core.fy_router.get_fy_prefix = lambda: "E2E_TEST_FY"
    
    try:
        result = _run_cloud_pipeline(config, run_id="e2e-cloud-test", started_at=now_iso())
        
        assert result["status"] == "CLOUD_COMPLETE"
        assert result["exit_code"] == 0
        
        db = ManifestDB(config.paths.database_path)
        try:
            last_run = db.last_run("cloud")
            assert last_run is not None
            assert last_run["run_id"] == "e2e-cloud-test"
            assert last_run["status"] == "CLOUD_COMPLETE"
            assert last_run["exit_code"] == 0
            assert last_run["error_message"] is None
            assert last_run["files_copied"] == 5
            assert last_run["bytes_copied"] == 5 * 1024
            
            assert db.file_count("cloud_status") == 5
        finally:
            db.close()
    finally:
        core.fy_router.get_fy_prefix = orig_prefix


def test_pipe_02_lan_pipeline(temp_config_and_db):
    """PIPE-02: LAN Pipeline — Golden Path, Diff Recorded to DB."""
    config = temp_config_and_db
    
    result = _run_lan_pipeline(config, run_id="e2e-lan-test", started_at=now_iso())
    
    assert result["status"] == "LAN_COMPLETE"
    assert result["exit_code"] in (1, 3)
    
    db = ManifestDB(config.paths.database_path)
    try:
        last_run = db.last_run("lan")
        assert last_run is not None
        assert last_run["run_id"] == "e2e-lan-test"
        assert last_run["status"] == "LAN_COMPLETE"
        assert last_run["files_copied"] == 5
        
        # 5 source files + 1 canary file (.AAM_TARGET_MOUNTED) = 6 files total on NAS
        assert db.file_count("lan_status") == 6
    finally:
        db.close()


def test_pipe_03_cloud_pipeline_health_fails(temp_config_and_db):
    """PIPE-03: Cloud Pipeline — Health Check Fails Fast, DB Records the Error."""
    config = temp_config_and_db
    
    # Hide source
    hidden = source_test_dir().parent / "E2E_TEST_SOURCE_HIDDEN"
    os.replace(str(source_test_dir()), str(hidden))
    
    try:
        with pytest.raises(Exception):
            _run_cloud_pipeline(config, run_id="e2e-cloud-fail", started_at=now_iso())
            
        db = ManifestDB(config.paths.database_path)
        try:
            last_run = db.last_run("cloud")
            assert last_run is not None
            assert last_run["status"] == "CLOUD_SKIPPED"
            assert "Source drive" in str(last_run["error_message"])
        finally:
            db.close()
    finally:
        os.replace(str(hidden), str(source_test_dir()))


def test_pipe_04_lan_pipeline_canary_missing(temp_config_and_db):
    """PIPE-04: LAN Pipeline — Canary Missing, DB Records the Error."""
    config = temp_config_and_db
    
    canary = nas_test_dir() / ".AAM_TARGET_MOUNTED"
    canary.unlink()
    
    try:
        with pytest.raises(Exception):
            _run_lan_pipeline(config, run_id="e2e-lan-fail", started_at=now_iso())
            
        db = ManifestDB(config.paths.database_path)
        try:
            last_run = db.last_run("lan")
            assert last_run is not None
            assert last_run["status"] == "LAN_SKIPPED"
            assert "Canary" in str(last_run["error_message"]) or str(nas_test_dir()) in str(last_run["error_message"])
        finally:
            db.close()
    finally:
        canary.touch()


def test_pipe_05_backup_lock_lifecycle(tmp_path):
    """PIPE-05: Backup Lock Written and Released.
    P2-CONC: the lock lifecycle now lives in _backup_slot (per pipeline);
    this test holds the slot directly and verifies write + release."""
    import ruamel.yaml
    yaml = ruamel.yaml.YAML()
    
    real_config = Path("config.yaml")
    temp_cfg_path = tmp_path / "config.yaml"
    shutil.copy2(real_config, temp_cfg_path)
    
    with open(temp_cfg_path, encoding="utf-8") as f:
        c = yaml.load(f)
    
    db_path = tmp_path / "test_manifest.db"
    c["paths"]["database_path"] = str(db_path)
    c["paths"]["source_drive"] = str(source_test_dir())
    c["paths"]["lan_destination"] = str(nas_test_dir())
    
    with open(temp_cfg_path, "w", encoding="utf-8") as f:
        yaml.dump(c, f)
        
    lock_path = db_path.parent / "backup.lock"

    from models.config import load_config as _lc

    cfgx = _lc(str(temp_cfg_path))
    from flow import _backup_slot

    try:
        with _backup_slot(cfgx):
            alive, pid = read_lock_alive(lock_path)
            lock_was_alive = alive

        assert lock_was_alive is True
        
        # Lock should be deleted after release
        assert not lock_path.exists()
    finally:
        pass
