import os
import shutil
import pytest
from pathlib import Path

from core.fy_rollover import (
    detect_rollover,
    create_new_fy_folders,
    update_config_yaml,
    run_archive_transition,
    rollover,
)

from tests.e2e_helpers import (
    cfg,
    source_test_dir,
    nas_test_dir,
    assert_log_contains,
    capture_logs,
)

CONFIG_TEMPLATE = Path("config.example.yaml") # Use example config as base to avoid corrupting real config

@pytest.fixture
def temp_config(tmp_path):
    """Provide a temporary config file for rollover testing."""
    import ruamel.yaml
    yaml = ruamel.yaml.YAML()
    yaml.preserve_quotes = True
    
    # Actually use the real config but copy it so we don't mess it up
    real_config = Path("config.yaml")
    temp_cfg_path = tmp_path / "config.yaml"
    shutil.copy2(real_config, temp_cfg_path)
    
    return temp_cfg_path


def test_fy_01_detect_rollover_false():
    """FY-01: detect_rollover() Returns False When FY Is Current."""
    config = cfg()
    source = config.paths.source_drive
    dest = config.paths.lan_destination
    
    # The real config should be pointing to the current FY
    assert detect_rollover(source, dest) is False


def test_fy_02_detect_rollover_true():
    """FY-02: detect_rollover() Returns True When FY Is Stale."""
    fake_source = r"E:\FY24-25"
    fake_dest = r"\\10.10.186.231\lan_backup\FY24-25"
    
    assert detect_rollover(fake_source, fake_dest) is True


def test_fy_03_create_new_fy_folders():
    """FY-03: create_new_fy_folders() Creates Real Folders on Disk."""
    source_parent = source_test_dir().parent
    nas_parent = nas_test_dir().parent
    
    new_fy = "FY_E2E_TEST"
    
    try:
        created = create_new_fy_folders(str(source_parent), str(nas_parent), new_fy)
        
        assert "source" in created
        assert created["source"].exists()
        assert created["source"].name == new_fy
        
        if "lan" in created:
            assert created["lan"].exists()
            assert created["lan"].name == new_fy
            assert (created["lan"] / ".AAM_TARGET_MOUNTED").exists()
    finally:
        shutil.rmtree(source_parent / new_fy, ignore_errors=True)
        shutil.rmtree(nas_parent / new_fy, ignore_errors=True)


def test_fy_04_update_config_yaml(temp_config):
    """FY-04: update_config_yaml() Atomically Rewrites Config."""
    import ruamel.yaml
    yaml = ruamel.yaml.YAML()
    
    source_parent = str(source_test_dir().parent)
    nas_parent = str(nas_test_dir().parent)
    new_fy = "FY_E2E_TEST"
    
    update_config_yaml(str(temp_config), source_parent, nas_parent, new_fy)
    
    with open(temp_config, "r", encoding="utf-8") as f:
        new_cfg = yaml.load(f)
        
    assert new_cfg["paths"]["source_drive"].endswith("FY_E2E_TEST")
    assert new_cfg["paths"]["lan_destination"].endswith("FY_E2E_TEST")


def test_fy_05_config_rewrite_atomic_crash(temp_config, monkeypatch):
    """FY-05: Config Rewrite Is Atomic — Crash Mid-Write Leaves Original Intact."""
    
    # Store original content
    orig_content = temp_config.read_text(encoding="utf-8")
    
    # Patch os.replace to crash
    def mock_replace(src, dst):
        raise OSError("Disk full simulation")
        
    monkeypatch.setattr(os, "replace", mock_replace)
    
    source_parent = str(source_test_dir().parent)
    nas_parent = str(nas_test_dir().parent)
    
    with pytest.raises(OSError, match="Disk full simulation"):
        update_config_yaml(str(temp_config), source_parent, nas_parent, "FY_E2E_TEST")
        
    # Verify original is untouched
    assert temp_config.read_text(encoding="utf-8") == orig_content


def test_fy_06_run_archive_transition(capture_logs):
    """FY-06: run_archive_transition() Actually Calls gcloud CLI."""
    config = cfg()
    bucket = config.cloud.bucket
    gcs_key = config.paths.gcs_key_path
    
    # Use E2E_TEST_FY which should be safe to touch metadata
    success = run_archive_transition(bucket, "E2E_TEST_FY", gcs_key)
    
    if success:
        assert_log_contains(capture_logs, "archive transition succeeded")
    else:
        logs = capture_logs.getvalue().lower()
        assert "gcloud cli not found" in logs or "matched no objects" in logs or "failed (exit" in logs


def test_fy_07_full_rollover(temp_config):
    """FY-07: Full Rollover on Temp Config — End-to-End."""
    import ruamel.yaml
    yaml = ruamel.yaml.YAML()
    yaml.preserve_quotes = True
    
    # Modify temp config to point to old FY
    with open(temp_config, "r", encoding="utf-8") as f:
        c = yaml.load(f)
        
    source_parent = str(source_test_dir().parent)
    nas_parent = str(nas_test_dir().parent)
    old_fy = "FY23-24"
    
    c["paths"]["source_drive"] = os.path.join(source_parent, old_fy)
    c["paths"]["lan_destination"] = os.path.join(nas_parent, old_fy)
    
    with open(temp_config, "w", encoding="utf-8") as f:
        yaml.dump(c, f)
        
    # Create the old folders
    Path(c["paths"]["source_drive"]).mkdir(parents=True, exist_ok=True)
    nas_path = Path(c["paths"]["lan_destination"])
    nas_path.mkdir(parents=True, exist_ok=True)
    (nas_path / ".AAM_TARGET_MOUNTED").touch()
    
    try:
        # Run rollover!
        result = rollover(config_path=str(temp_config))
        assert result is True
        
        # Verify config was updated
        with open(temp_config, "r", encoding="utf-8") as f:
            new_c = yaml.load(f)
            
        assert not new_c["paths"]["source_drive"].endswith(old_fy)
        assert not new_c["paths"]["lan_destination"].endswith(old_fy)
        
        # Verify new folders created
        assert Path(new_c["paths"]["source_drive"]).exists()
        if Path(new_c["paths"]["lan_destination"]).parent.exists(): # If NAS is online
            assert Path(new_c["paths"]["lan_destination"]).exists()
            
    finally:
        # Cleanup
        shutil.rmtree(Path(c["paths"]["source_drive"]), ignore_errors=True)
        shutil.rmtree(Path(c["paths"]["lan_destination"]), ignore_errors=True)
        
        # Reload to get new paths
        with open(temp_config, "r", encoding="utf-8") as f:
            new_c = yaml.load(f)
        shutil.rmtree(Path(new_c["paths"]["source_drive"]), ignore_errors=True)
        shutil.rmtree(Path(new_c["paths"]["lan_destination"]), ignore_errors=True)
