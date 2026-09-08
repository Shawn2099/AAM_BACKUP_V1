# tests/test_fix_24_1_1_orphan_filter.py
from unittest.mock import AsyncMock, MagicMock, patch


class MockFlow:
    def __init__(self, id, name):
        self.id = id
        self.name = name


class MockRun:
    def __init__(self, id, flow_id, name):
        self.id = id
        self.flow_id = flow_id
        self.name = name


def test_cancel_orphaned_runs_functional_filtering():
    """Verify _cancel_orphaned_runs strictly cancels flows matching registered AAM flow_ids."""
    import launch

    aam_flows = [
        MockFlow("flow-backup-id", "aam-backup"),
        MockFlow("flow-weekly-id", "weekly-report"),
        MockFlow("flow-monthly-id", "monthly-report"),
        MockFlow("flow-rollover-id", "rollover-check"),
        MockFlow("flow-audit-id", "integrity-audit"),
    ]

    # Flow runs present on the server
    all_flow_runs = [
        # Genuine AAM flow runs (should be cancelled)
        MockRun("run-1", "flow-backup-id", "backup-cloud-run"),
        MockRun("run-2", "flow-backup-id", "random-slug-courageous-lynx"),  # Random slug run name
        MockRun("run-3", "flow-audit-id", "integrity-audit-weekly"),
        # Foreign / unrelated flow runs with substrings (must NOT be cancelled)
        MockRun("run-4", "foreign-sec-audit-id", "security-audit-flow"),
        MockRun("run-5", "foreign-fin-report-id", "financial-report-quarterly"),
        MockRun("run-6", "foreign-random-id", "playful-otter-etl"),
    ]

    mock_client = MagicMock()
    mock_client.read_flows = AsyncMock(return_value=aam_flows)
    # read_flow_runs called for PENDING and RUNNING
    mock_client.read_flow_runs = AsyncMock(side_effect=[all_flow_runs, []])
    mock_client.set_flow_run_state = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__.return_value = mock_client
    cm.__aexit__.return_value = None

    with (
        patch("prefect.client.orchestration.get_client", return_value=cm),
        patch("core.process.read_lock_alive", return_value=(False, None)),
    ):
        launch._cancel_orphaned_runs()

    # Verify set_flow_run_state was only called for AAM runs (run-1, run-2, run-3)
    cancelled_run_ids = [call.kwargs["flow_run_id"] for call in mock_client.set_flow_run_state.call_args_list]
    assert "run-1" in cancelled_run_ids, "AAM backup run must be cancelled"
    assert "run-2" in cancelled_run_ids, "AAM random slug run must be cancelled"
    assert "run-3" in cancelled_run_ids, "AAM integrity audit run must be cancelled"
    assert "run-4" not in cancelled_run_ids, "Unrelated security-audit must NOT be cancelled"
    assert "run-5" not in cancelled_run_ids, "Unrelated financial-report must NOT be cancelled"
    assert "run-6" not in cancelled_run_ids, "Unrelated random ETL run must NOT be cancelled"
    assert len(cancelled_run_ids) == 3


def test_missing_aam_flow_ids_fails_safely_and_cancels_nothing():
    """If server has no AAM flows registered, orphan cleanup must abort safely without cancelling any runs."""
    import launch

    mock_client = MagicMock()
    mock_client.read_flows = AsyncMock(return_value=[])  # No AAM flows found
    mock_client.read_flow_runs = AsyncMock(return_value=[
        MockRun("run-foreign", "some-foreign-id", "some-flow-run")
    ])
    mock_client.set_flow_run_state = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__.return_value = mock_client
    cm.__aexit__.return_value = None

    with (
        patch("prefect.client.orchestration.get_client", return_value=cm),
        patch("core.process.read_lock_alive", return_value=(False, None)),
    ):
        launch._cancel_orphaned_runs()

    assert not mock_client.set_flow_run_state.called, "Zero runs must be cancelled when AAM flows cannot be resolved"


def test_partial_aam_flow_resolution_does_not_broaden_scope():
    """If only a subset of AAM flows exist on the server, only runs for resolved AAM flow IDs are cancelled."""
    import launch

    # Only aam-backup and integrity-audit are registered on server
    partial_flows = [
        MockFlow("flow-backup-id", "aam-backup"),
        MockFlow("flow-audit-id", "integrity-audit"),
    ]
    all_runs = [
        MockRun("run-backup", "flow-backup-id", "backup-run"),
        MockRun("run-audit", "flow-audit-id", "integrity-audit-run"),
        MockRun("run-weekly", "flow-weekly-id", "weekly-report-orphan"),  # not resolved
        MockRun("run-foreign", "flow-other-id", "other-run"),
    ]

    mock_client = MagicMock()
    mock_client.read_flows = AsyncMock(return_value=partial_flows)
    mock_client.read_flow_runs = AsyncMock(side_effect=[all_runs, []])
    mock_client.set_flow_run_state = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__.return_value = mock_client
    cm.__aexit__.return_value = None

    with (
        patch("prefect.client.orchestration.get_client", return_value=cm),
        patch("core.process.read_lock_alive", return_value=(False, None)),
    ):
        launch._cancel_orphaned_runs()

    cancelled_run_ids = [call.kwargs["flow_run_id"] for call in mock_client.set_flow_run_state.call_args_list]
    assert "run-backup" in cancelled_run_ids
    assert "run-audit" in cancelled_run_ids
    assert "run-weekly" not in cancelled_run_ids, "Unresolved flow ID must not be cancelled"
    assert "run-foreign" not in cancelled_run_ids
    assert len(cancelled_run_ids) == 2


def test_launch_source_uses_aam_flow_ids_not_loose_substrings():
    """Verify launch.py source strictly filters by flow_id and no longer uses loose substring matching."""
    with open("launch.py") as f:
        source = f.read()

    assert "aam_flow_ids" in source, "launch.py must resolve and use aam_flow_ids"
    assert "getattr(r, \"flow_id\", None) in aam_flow_ids" in source
    assert 'any(k in str(getattr(r, "name"' not in source, "Loose name substring matching must be removed"

