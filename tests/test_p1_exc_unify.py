"""P1-EXC - RED tests: single HealthError identity across the codebase.

Evidence: core/health.py:14 and core/lan_preflight.py:15 each defined their
own HealthError; callers catching one variant silently missed the other.
"""
import pytest

import core.health
import core.lan_preflight


def test_healtherror_is_single_class():
    """The two modules must expose THE SAME exception class."""
    assert core.health.HealthError is core.lan_preflight.HealthError


def test_lan_preflight_raise_is_caught_as_core_health_error():
    """An exception raised by the LAN preflight must be catchable as
    core.health.HealthError - the canonical domain exception."""
    with pytest.raises(core.health.HealthError):
        # canary check fires before robocopy is ever spawned
        core.lan_preflight.run_lan_dry_run(
            source="C:\\nonexistent_src", dest="Z:\\definitely_not_mounted",
        )
