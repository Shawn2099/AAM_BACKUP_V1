"""Safety checks for the Windows service installer script."""

from pathlib import Path


def test_install_services_does_not_kill_all_python_processes():
    script = Path("deploy/install_services.ps1").read_text(encoding="utf-8")
    assert "taskkill /F /IM python.exe /T" not in script
    assert "taskkill /F /IM prefect.exe /T" not in script
