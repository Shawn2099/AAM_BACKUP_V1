# tests/test_fix_26_1_1_mode_all_status.py
from unittest.mock import MagicMock


def _detect_pipeline_active(pipeline: str, run) -> bool:
    tags = run.tags or []
    parameters = run.parameters or {}
    if "integrity" in tags:
        return False
    run_mode = parameters.get("mode", "")
    return pipeline in tags or run_mode == pipeline or run_mode == "all"


def test_mode_all_detected_as_active_for_cloud_and_lan():
    run = MagicMock(tags=["production"], parameters={"mode": "all"})
    assert _detect_pipeline_active("cloud", run)
    assert _detect_pipeline_active("lan", run)


def test_integrity_audit_does_not_hijack_backup_status():
    """Weekly integrity audit has mode='all' and tags=['maintenance', 'integrity'] - must NOT show as backup running."""
    run = MagicMock(tags=["maintenance", "integrity"], parameters={"mode": "all"})
    assert not _detect_pipeline_active("cloud", run)
    assert not _detect_pipeline_active("lan", run)


def test_pipeline_specific_modes_isolated():
    cloud_run = MagicMock(tags=[], parameters={"mode": "cloud"})
    assert _detect_pipeline_active("cloud", cloud_run)
    assert not _detect_pipeline_active("lan", cloud_run)

    lan_run = MagicMock(tags=[], parameters={"mode": "lan"})
    assert _detect_pipeline_active("lan", lan_run)
    assert not _detect_pipeline_active("cloud", lan_run)


def test_ui_source_excludes_integrity_tag():
    with open("ui.py") as f:
        source = f.read()
    assert '"integrity" in tags' in source or "'integrity' in tags" in source
