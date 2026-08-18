"""Safety checks for the Windows service installer script."""

from pathlib import Path


def test_install_services_does_not_kill_all_python_processes():
    # F18: the deploy scripts are numbered — this test still pointed at the
    # pre-numbering name and failed with FileNotFoundError on any fresh checkout.
    script = Path("deploy/06_install_services.ps1").read_text(encoding="utf-8")
    assert "taskkill /F /IM python.exe /T" not in script
    assert "taskkill /F /IM prefect.exe /T" not in script
