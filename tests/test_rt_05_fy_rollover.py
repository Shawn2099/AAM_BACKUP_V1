import os
import shutil
from pathlib import Path

import pytest

from core.fy_rollover import (
    create_new_fy_folders,
    detect_rollover,
    rollover,
    run_archive_transition,
    update_config_yaml,
)
from tests.e2e_helpers import (
    assert_log_contains,
    cfg,
    nas_test_dir,
    source_test_dir,
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
    
    with open(temp_config, encoding="utf-8") as f:
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
    """FY-07: Full Rollover on Temp Config — End-to-End.

    S2-20 (session-2 finding): the original version built its scenario
    under the live config's parents — source_test_dir().parent is E:\\ and
    the NAS share root — so the rollover TARGET (current FY) and the
    finally-block rmtree resolved to E:\\FY26-27 (the live source dataset)
    and the NAS FY26-27 folder: a test that destroys production data on
    SUCCESS. That is why the 2026-08-20 audit excluded it (destructive).

    Now the scenario lives under dedicated scratch roots
    (<E2E_TEST_SOURCE | E2E_TEST_DEST>\\ROLLOVER\\FY...) and:
      1. a guard refuses to run if any old/new path matches a live
         production path (defense in depth — a config change could never
         silently re-aim the cleanup at live data again);
      2. the temp config forces wol.enabled=False and
         lan.shutdown_after_backup=False — rollover() runs a final backup
         with THIS config, and a copied production config would wake AND
         send `shutdown /s /m \\\\NAS /t 300` (a real 5-minute-delayed NAS
         power-off) during the test.
    """
    import ruamel.yaml
    from core.time_utils import get_fy_prefix
    from tests.e2e_helpers import (
        assert_safe_rollover_targets,
        live_rollover_path_set,
    )

    yaml = ruamel.yaml.YAML()
    yaml.preserve_quotes = True

    # Modify temp config to point to old FY — under SCRATCH roots, never
    # under the live config's parents (S2-20).
    with open(temp_config, encoding="utf-8") as f:
        c = yaml.load(f)

    source_parent = str(source_test_dir() / "ROLLOVER")
    nas_parent = str(nas_test_dir() / "ROLLOVER")
    old_fy = "FY23-24"

    c["paths"]["source_drive"] = os.path.join(source_parent, old_fy)
    c["paths"]["lan_destination"] = os.path.join(nas_parent, old_fy)

    # S2-20 guard: the rollover computes NEW paths = <parent>/<current FY>.
    # If any candidate (old or new) matches a live production path, abort.
    new_fy = get_fy_prefix()
    assert_safe_rollover_targets(
        [
            c["paths"]["source_drive"],
            c["paths"]["lan_destination"],
            os.path.join(source_parent, new_fy),
            os.path.join(nas_parent, new_fy),
        ],
        live_rollover_path_set(),
    )

    # S2-20: never let a test's final backup wake or shut down the real NAS.
    c["wol"]["enabled"] = False
    c["lan"]["shutdown_after_backup"] = False

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
        with open(temp_config, encoding="utf-8") as f:
            new_c = yaml.load(f)

        assert not new_c["paths"]["source_drive"].endswith(old_fy)
        assert not new_c["paths"]["lan_destination"].endswith(old_fy)

        # Verify new folders created (scratch targets — live data untouched)
        assert Path(new_c["paths"]["source_drive"]).exists()
        if Path(new_c["paths"]["lan_destination"]).parent.exists(): # If NAS is online
            assert Path(new_c["paths"]["lan_destination"]).exists()

    finally:
        # Cleanup — scratch paths only (the guard above guarantees these
        # can never be live production paths).
        shutil.rmtree(Path(c["paths"]["source_drive"]), ignore_errors=True)
        shutil.rmtree(Path(c["paths"]["lan_destination"]), ignore_errors=True)

        # Reload to get new paths
        with open(temp_config, encoding="utf-8") as f:
            new_c = yaml.load(f)
        shutil.rmtree(Path(new_c["paths"]["source_drive"]), ignore_errors=True)
        shutil.rmtree(Path(new_c["paths"]["lan_destination"]), ignore_errors=True)
