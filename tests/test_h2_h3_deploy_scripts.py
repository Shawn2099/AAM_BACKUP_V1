"""Plan H2/H3: valid 12 h Active Hours on Windows Server 2016; readiness syncs without dev tools."""
import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def test_active_hours_are_a_valid_twelve_hour_window_in_the_keys_windows_really_reads():
    bat = (DEPLOY / "03_setup_system.bat").read_text(encoding="utf-8", errors="replace")
    assert "ISM\\ActiveHours\" /v" not in bat           # that key does not exist on Windows
    start = int(re.search(r"UX\\Settings\" /v ActiveHoursStart /t REG_DWORD /d (\d+)", bat).group(1))
    end = int(re.search(r"UX\\Settings\" /v ActiveHoursEnd /t REG_DWORD /d (\d+)", bat).group(1))
    assert (end - start) % 24 == 12 and (start, end) == (20, 8)       # max 12 h on Server 2016
    assert "SetActiveHours /t REG_DWORD /d 1" in bat


def test_readiness_checks_the_same_window_and_reports_the_update_state():
    ps1 = (DEPLOY / "04_check_readiness.ps1").read_text(encoding="utf-8", errors="replace")
    assert "$ahStart -eq 20 -and $ahEnd -eq 8" in ps1 and "UX\\Settings" in ps1
    assert "wuauserv" in ps1 and "NoAutoUpdate" in ps1


def test_readiness_syncs_without_dev_tools():
    ps1 = (DEPLOY / "04_check_readiness.ps1").read_text(encoding="utf-8", errors="replace")
    assert "sync --frozen --no-dev --link-mode copy" in ps1


def test_readiness_reads_the_optional_update_policy_value_safely_under_strict_mode():
    """Found on the box: $wuPolicy.NoAutoUpdate threw PropertyNotFoundStrict when the value is absent."""
    ps1 = (DEPLOY / "04_check_readiness.ps1").read_text(encoding="utf-8", errors="replace")
    assert 'PSObject.Properties["NoAutoUpdate"]' in ps1
    assert "$wuPolicy.NoAutoUpdate -eq 1" not in ps1


def test_installer_grants_use_computer_slash_name_because_icacls_rejects_dot_slash():
    ps1 = (DEPLOY / "06_install_services.ps1").read_text(encoding="utf-8", errors="replace")
    assert '$AclAccount = if ($ServiceAccount.StartsWith(".\\"))' in ps1
    assert ':(OI)(CI)M" /T /C /Q' in ps1 and '${AclAccount}:(OI)(CI)M' in ps1
    assert '${ServiceAccount}:(OI)(CI)M' not in ps1


def test_readiness_resets_the_venv_acls_after_uv_sync():
    """Found on the box: uv copied _rust.pyd with an Administrators-only ACL; the service account
    could not load it (agent crash-looped with 'DLL load failed ... Access is denied')."""
    ps1 = (DEPLOY / "04_check_readiness.ps1").read_text(encoding="utf-8", errors="replace")
    sync = ps1.index("sync --frozen --no-dev --link-mode copy")
    assert 'icacls (Join-Path $ProjectDir ".venv") /reset /T /C /Q' in ps1[sync:sync + 1500]
