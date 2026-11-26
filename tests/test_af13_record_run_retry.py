"""AF-13: _record_run retries transient DB lock errors before logging CRITICAL."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import flow
from models.config import MaintenanceConfig


def test_af13_maintenance_config_has_record_run_retry_settings():
    cfg = MaintenanceConfig()
    assert cfg.record_run_retries >= 2
    assert cfg.record_run_retry_delay_ms >= 50


def test_af13_record_run_retries_and_succeeds_after_transient_failure():
    cfg = MagicMock()
    cfg.maintenance.record_run_retries = 3
    cfg.maintenance.record_run_retry_delay_ms = 10
    with patch("flow.ManifestDB") as mdb, \
         patch("flow.record_run_history", side_effect=[False, False, True]) as rrh, \
         patch("flow.time.sleep"):
        flow._record_run(
            cfg, "run-1", "cloud", "2026-10-14T22:00:00+05:30", "CLOUD_COMPLETE", 0, None,
        )
    assert rrh.call_count == 3
    assert mdb.from_config.return_value.close.called
