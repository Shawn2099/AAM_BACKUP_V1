"""F7 (Windows PowerShell 5.1 review 2026-11-23).

F7: `Test-Connection -TimeoutSeconds` does not exist in Windows PowerShell 5.1 (it is PowerShell 7 only), so the
discovery script failed on a Windows Server 2016 box. The pings go through one helper built on
System.Net.NetworkInformation.Ping with a millisecond timeout.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
DISCOVERY = DEPLOY / "01_server_discovery.ps1"
INSTALLER = DEPLOY / "06_install_services.ps1"
PS51 = shutil.which("powershell.exe")


def _parse_errors(path: Path) -> list[str]:
    cmd = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{path}',[ref]$t,[ref]$e);$e|ForEach-Object{{$_.Message}}")
    out = subprocess.run([PS51, "-NoProfile", "-NonInteractive", "-Command", cmd],
                         capture_output=True, text=True, timeout=120)
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def test_discovery_uses_no_ps7_only_parameter():
    text = DISCOVERY.read_text(encoding="utf-8")
    assert "-TimeoutSeconds" not in text
    assert len(re.findall(r"Test-HostPing\b", text)) >= 4        # one definition + three uses


def test_the_ping_helper_has_a_millisecond_timeout():
    text = DISCOVERY.read_text(encoding="utf-8")
    assert "System.Net.NetworkInformation.Ping" in text and ".Send(" in text


@pytest.mark.skipif(PS51 is None, reason="needs Windows PowerShell")
@pytest.mark.parametrize("path", [DISCOVERY, INSTALLER], ids=lambda p: p.name)
def test_scripts_parse_with_the_windows_powershell_parser(path):
    assert _parse_errors(path) == []
