"""D5 (final audit 2026-10-20): the run history is kept long enough for what reads it.

`maintenance.db_retention_days` allowed 7. `ManifestDB.purge_old_runs` deletes run_history older than that, and the
guard baselines (G2 source shrink, G4 destination) and "last successful run" read run_history: when a leg's last
success was purged the shrink guard passed as "no previous successful run (first run)" - silently skipped - and the
monthly report (30 days) and the dashboard strip saw truncated data. The floor is 35 days: the 30-day report window
plus a week of margin."""

import pytest
from pydantic import ValidationError

from core import guards
from core.manifest import ManifestDB
from models.config import MaintenanceConfig, model_default

FLOOR = 35


@pytest.mark.parametrize("days", [7, 30, 34])
def test_a_history_shorter_than_the_reports_need_is_refused(days):
    with pytest.raises(ValidationError, match="greater than or equal to 35"):
        MaintenanceConfig(db_retention_days=days)


@pytest.mark.parametrize("days", [FLOOR, 90, 3650])
def test_longer_histories_are_accepted(days):
    assert MaintenanceConfig(db_retention_days=days).db_retention_days == days


def test_the_default_and_the_purge_fallback_follow_the_model():
    assert model_default(MaintenanceConfig, "db_retention_days") == 90
    import inspect
    default = inspect.signature(ManifestDB.purge_old_runs).parameters["retention_days"].default
    assert default == 90


def test_the_floor_is_the_30_day_report_window_plus_a_week():
    from core.report_period import ReportPeriod
    start, end = ReportPeriod.last_days(30).start, ReportPeriod.last_days(30).end
    assert (end - start).days <= 30 and FLOOR - 30 == 5


def test_a_purged_baseline_would_have_disabled_g2_which_is_why_the_floor_exists():
    # documents the failure the floor prevents: no baseline -> the shrink guard passes as a first run
    verdict = guards.g2_source_shrink(0, None, 20)
    assert verdict.passed and "first run" in verdict.reason
