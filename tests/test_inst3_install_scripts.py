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


def test_batch_echo_lines_inside_blocks_escape_their_parentheses():
    # an unescaped ")" inside an `if (...)` block ends the block early: "'. was unexpected at this time.' and exit 255"
    # (found in 03 step 2 by the 2026-10-23 review, reproduced with cmd)
    bad = []
    for name in ("03_setup_system.bat", "07_restart_services.bat", "09_restore_from_gcs.bat", "10_recreate_canary.bat"):
        for no, line in enumerate(_t(name).splitlines(), 1):
            if line.startswith((" ", "\t")) and line.strip().lower().startswith("echo "):
                if re.search(r"(?<!\^)[()]", line.strip()[5:]):
                    bad.append(f"{name}:{no}: {line.strip()}")
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("script", ["04_check_readiness.ps1", "06_install_services.ps1", "11_reset_dashboard_passwords.ps1"])
def test_config_location_is_resolved_not_hard_coded_to_the_code_folder(script):
    # the fresh install keeps config.yaml OUTSIDE the locked code folder (C:\BackupAgent\config)
    assert "Resolve-ConfigFile" in _t(script)


def test_fresh_config_lives_outside_the_locked_code_folder_with_its_own_rights():
    common = _t("_common.ps1")
    assert r"C:\BackupAgent\config\config.yaml" in common
    assert "${acl}:(OI)(CI)M" in common
    for bat in ("07_restart_services.bat", "09_restore_from_gcs.bat"):
        assert r"C:\BackupAgent\config\config.yaml" in _t(bat), bat


def test_the_template_the_owner_edits_in_notepad_is_crlf():
    assert re.search(r"^deploy/config\.install\.yaml\s+text\s+eol=crlf", (ROOT / ".gitattributes").read_text(), re.M)


def test_the_defender_check_can_pass_and_the_install_checks_read_stdout_only():
    text = _t("04_check_readiness.ps1")
    assert "Get-MpPreference" in text                      # exclusions present = OK, so a clean box can reach SUCCESS
    assert "install_checks.py\") $cfg 2>$null" in text      # a stderr log line must not become a fake FAIL


def test_the_install_guide_has_no_control_characters_and_names_real_files():
    text = (ROOT / "docs" / "INSTALL_FRESH.md").read_text(encoding="utf-8")
    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text)
    for name in re.findall(r"deploy\\([\w.]+\.(?:ps1|bat|py|yaml))", text):
        assert (DEPLOY / name).exists(), name
    assert "--clear-soft-delete-policy" not in text
