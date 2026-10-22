"""Fresh-install plan (docs/PLAN_FRESH_INSTALL_2026-10-23.md): properties of the deploy scripts that a
clean Windows Server 2016 depends on. Text-level checks (the real proof is the clean-VM run)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"


def _t(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("script", ["04_check_readiness.ps1", "06_install_services.ps1"])
def test_scripts_refuse_instead_of_self_elevating(script):
    # a script that reopens itself in a new window loses its output when it ends
    text = _t(script)
    assert "Assert-Administrator" in text and "-Verb RunAs" not in text


def test_uv_environment_is_set_before_the_first_uv_call():
    text = _t("04_check_readiness.ps1")
    first_env = text.index("Set-InstallEnvironment")
    first_uv = min(i for i in (text.find("& $uvPath"), text.find('"`"$uvPath`"')) if i >= 0)
    assert first_env < first_uv
    assert "run python --version" not in text


def test_install_aborts_before_touching_services_and_never_claims_success_unverified():
    text = _t("06_install_services.ps1")
    # the dashboard settings (firewall) are read before the first service is installed
    assert text.index("$dashInfo = ") < text.index("& $NSSM install")
    assert "INSTALL FAILED" in text and "Wait-Http" in text
    assert text.index("INSTALL FAILED") < text.index("INSTALLATION COMPLETE")
    # every "complete" claim is preceded by a check of the services
    assert "Get-Service $svc" in text


def test_readiness_runs_the_install_checks_the_mail_step_and_the_rights():
    text = _t("04_check_readiness.ps1")
    for needle in ("install_checks.py", "set_mail_password.py", "send_test_mail.py", "Grant-AgentRights"):
        assert needle in text, needle
    # rights are granted before the probe runs as the account
    assert text.index("Grant-AgentRights") < text.index("Register-ScheduledTask -TaskName $taskName")
    # no fixed-folder key check any more: the key path comes from config.yaml
    assert 'keys\\aam-gcs-key.json' not in text


def test_setup_unpacks_gcloud_itself_locks_the_code_folder_and_has_no_control_characters():
    text = _t("03_setup_system.bat")
    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text.replace("\r", ""))  # a "\b" once became a backspace
    assert '"%SEVENZIP%" x "%GCLOUD_ZIP%"' in text
    assert "icacls" in text and "*S-1-5-32-544" in text and "/inheritance:r" in text


def test_gitattributes_forces_crlf_on_windows_scripts():
    attrs = (ROOT / ".gitattributes").read_text()
    for pattern in ("*.bat", "*.ps1"):
        assert re.search(rf"^{re.escape(pattern)}\s+text\s+eol=crlf", attrs, re.M), pattern


@pytest.mark.skipif(not shutil.which("powershell"), reason="needs Windows PowerShell")
@pytest.mark.parametrize("script", ["04_check_readiness.ps1", "06_install_services.ps1", "_common.ps1"])
def test_powershell_scripts_parse(script):
    cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{DEPLOY / script}',[ref]$null,[ref]$e); if ($e.Count) {{ $e | % {{ $_.Message }}; exit 1 }}")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
