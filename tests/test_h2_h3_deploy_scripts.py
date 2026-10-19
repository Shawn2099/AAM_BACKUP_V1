"""Plan H2/H3: valid 12 h Active Hours on Windows Server 2016; readiness syncs without dev tools."""
import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def test_active_hours_are_a_valid_twelve_hour_window():
    bat = (DEPLOY / "03_setup_system.bat").read_text(encoding="utf-8", errors="replace")
    start = int(re.search(r"ActiveHours\" /v Start /t REG_DWORD /d (\d+)", bat).group(1))
    end = int(re.search(r"ActiveHours\" /v End /t REG_DWORD /d (\d+)", bat).group(1))
    assert (end - start) % 24 == 12 and (start, end) == (20, 8)       # max 12 h on Server 2016


def test_readiness_checks_the_same_window_and_reports_the_update_state():
    ps1 = (DEPLOY / "04_check_readiness.ps1").read_text(encoding="utf-8", errors="replace")
    assert "$ah.Start -eq 20 -and $ah.End -eq 8" in ps1
    assert "wuauserv" in ps1 and "NoAutoUpdate" in ps1


def test_readiness_syncs_without_dev_tools():
    ps1 = (DEPLOY / "04_check_readiness.ps1").read_text(encoding="utf-8", errors="replace")
    assert "sync --frozen --no-dev --link-mode copy" in ps1


def test_readiness_reads_the_optional_update_policy_value_safely_under_strict_mode():
    """Found on the box: $wuPolicy.NoAutoUpdate threw PropertyNotFoundStrict when the value is absent."""
    ps1 = (DEPLOY / "04_check_readiness.ps1").read_text(encoding="utf-8", errors="replace")
    assert 'PSObject.Properties["NoAutoUpdate"]' in ps1
    assert "$wuPolicy.NoAutoUpdate -eq 1" not in ps1
