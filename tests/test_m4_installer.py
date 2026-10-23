"""M4 4.5 - installer + readiness (plan WP16, §10 item 2).

Findings: the services start with `uv run python ...`, which syncs packages
from the internet at every boot (Sophos); AAM_CONFIG was never set; all three
services run as LocalSystem, which in a WORKGROUP has no usable credentials
on the NAS (21 nights of "Access is denied") - and nothing verified the
account's rights before going live.
"""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = (ROOT / "deploy" / "06_install_services.ps1").read_text(encoding="utf-8")
READINESS = (ROOT / "deploy" / "04_check_readiness.ps1").read_text(encoding="utf-8")


def _probe_module():
    spec = importlib.util.spec_from_file_location(
        "probe_service_account", ROOT / "deploy" / "probe_service_account.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _config(tmp_path, schedule_extra=""):
    src = tmp_path / "src" / "FY26-27"
    src.mkdir(parents=True)
    (src / "ledger.xlsx").write_bytes(b"x")
    runtime = tmp_path / "rt"
    runtime.mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "paths:\n"
        f"  source_drive: '{src}'\n"
        "  lan_destination: '\\\\nas\\share\\FY26-27'\n"
        f"  runtime_dir: '{runtime}'\n"
        "  gcs_key_path: ''\n"
        "cloud:\n  enabled: false\n"
        "wol:\n  enabled: false\n"
        "dashboard: {}\n"
        + schedule_extra, encoding="utf-8")
    return cfg


@pytest.fixture
def probe(monkeypatch):
    mod = _probe_module()
    from core import permissions
    monkeypatch.setattr(permissions, "probe_destination_write", lambda dest, run_id: None)
    monkeypatch.setattr(permissions, "check_backup_privilege", lambda artefacts, timeout_s: None)
    return mod


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


class TestProbe:
    def test_all_rights_present(self, probe, tmp_path):
        result = tmp_path / "result.txt"
        assert probe.main(["p", str(_config(tmp_path)), str(result)]) == 0
        lines = _lines(result)
        status = {line.split(" ", 1)[1].split(":", 1)[0]: line.split(" ", 1)[0] for line in lines}
        assert status["config"] == "OK" and status["NAS write"] == "OK"
        assert status["runtime folder"] == "OK" and status["config folder"] == "OK"
        assert any(line.startswith("OK source read") and "ledger.xlsx" in line for line in lines)
        assert any(line.startswith("OK backup privilege") for line in lines)
        assert any(line.startswith("SKIP NAS remote shutdown") for line in lines)
        # probe files are cleaned up
        assert not list((tmp_path / "rt").iterdir())
        assert not list(tmp_path.glob(".aam_account_probe_*"))

    def test_nas_access_denied_fails_with_the_os_error(self, probe, tmp_path, monkeypatch):
        from core import permissions

        def denied(dest, run_id):
            raise PermissionError(5, "Access is denied", dest)
        monkeypatch.setattr(permissions, "probe_destination_write", denied)
        result = tmp_path / "result.txt"
        assert probe.main(["p", str(_config(tmp_path)), str(result)]) == 1
        [line] = [x for x in _lines(result) if x.startswith("FAIL")]
        assert line.startswith("FAIL NAS write") and "Access is denied" in line

    def test_missing_backup_privilege_fails(self, probe, tmp_path, monkeypatch):
        from core import permissions

        def missing(artefacts, timeout_s):
            raise permissions.PermissionCheckError("SeBackupPrivilege not held")
        monkeypatch.setattr(permissions, "check_backup_privilege", missing)
        result = tmp_path / "result.txt"
        assert probe.main(["p", str(_config(tmp_path)), str(result)]) == 1
        assert any(x.startswith("FAIL backup privilege") for x in _lines(result))

    def test_a_config_this_version_refuses_is_reported(self, probe, tmp_path):
        bad = _config(tmp_path, "schedule:\n  lan_cron: '0 21 * * *'\n  audit_cron: '0 22 * * SUN'\n")
        result = tmp_path / "result.txt"
        assert probe.main(["p", str(bad), str(result)]) == 1
        [line] = _lines(result)[1:]
        assert line.startswith("FAIL config") and "run budgets" in line


class TestInstallerContract:
    def test_services_run_the_venv_python_never_uv(self):
        code = [line for line in INSTALLER.splitlines() if not line.lstrip().startswith("#")]
        assert not [line for line in code if "uv run" in line]   # no `uv run` launch at boot
        for svc in ("SVC_SERVER", "SVC_AGENT", "SVC_WATCHDOG"):
            assert f"& $NSSM install ${svc} $VenvPython" in INSTALLER
        assert '"-m prefect server start --host 127.0.0.1 --port 4200"' in INSTALLER

    def test_every_service_gets_aam_config(self):
        env_lines = [line for line in INSTALLER.splitlines()
                     if "AAM_CONFIG=$ConfigFile" in line]
        assert len(env_lines) == 3

    def test_only_the_agent_runs_as_the_service_account(self):
        assert "& $NSSM set $SVC_AGENT ObjectName $ServiceAccount $AgentPassword" in INSTALLER
        assert "$SVC_SERVER ObjectName" not in INSTALLER
        assert "$SVC_WATCHDOG ObjectName" not in INSTALLER

    def test_password_is_prompted_never_a_parameter(self):
        assert "Read-Host -AsSecureString" in INSTALLER
        assert "[string]$ServiceAccount" in INSTALLER and "Password =" not in INSTALLER.split(
            "param(")[1].split(")")[0]

    def test_installer_validates_the_config_before_touching_services(self):
        check = INSTALLER.index("load_config(r'$ConfigFile')")
        assert check < INSTALLER.index("& $NSSM stop $SVC_WATCHDOG")

    def test_readiness_syncs_frozen_and_probes_as_the_account(self):
        assert "sync --frozen" in READINESS
        # the probe runs as the account via a short-lived scheduled task (non-interactive
        # logon like the service); Start-Process -Credential fails without a desktop
        assert "Register-ScheduledTask" in READINESS and "probe_service_account.py" in READINESS
        assert "Unregister-ScheduledTask" in READINESS.split("finally", 1)[-1]
        assert "load_config(r'$cfg')" in READINESS

    @pytest.mark.skipif(shutil.which("powershell") is None, reason="needs Windows PowerShell")
    @pytest.mark.parametrize("script", ["06_install_services.ps1", "04_check_readiness.ps1"])
    def test_scripts_parse(self, script):
        path = ROOT / "deploy" / script
        cmd = ("$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{path}',[ref]$t,[ref]$e);$e.Count")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                             capture_output=True, text=True, timeout=120)
        assert out.stdout.strip() == "0", out.stdout + out.stderr


def test_unattended_password_file_is_dpapi_only_and_always_deleted():
    """Owner 2026-10-15 ("do everything for me"): an optional -PasswordFile for remote
    installs. Only a DPAPI secure string of the same Windows user is accepted, and it is
    deleted right after reading (finally) - never a plain-text password parameter."""
    for script in (INSTALLER, READINESS):
        assert "[string]$PasswordFile" in script
        helper = script[script.index("function Read-PasswordFile"):]
        helper = helper[:helper.index("\n}\n")]
        assert "ConvertTo-SecureString -ErrorAction Stop" in helper      # DPAPI, no -AsPlainText
        assert "-AsPlainText" not in helper
        assert "finally" in helper and "Remove-Item -Path $Path" in helper


def test_installer_checks_the_account_windows_actually_applied():
    """Trial box 2026-10-15: an unreadable password left the agent on LocalSystem while the
    installer printed [OK]. It now reads the account back and stops before starting services."""
    assert "Get-CimInstance Win32_Service" in INSTALLER and ".StartName" in INSTALLER
    check = INSTALLER.index(".StartName")
    assert check < INSTALLER.index("net start $SVC_SERVER")
    assert "Services were installed but NOT started" in INSTALLER


def test_password_file_is_trimmed_before_decrypting():
    for script in (INSTALLER, READINESS):
        assert "-Raw -ErrorAction Stop).Trim() | ConvertTo-SecureString" in script


def test_our_own_services_on_the_ports_are_not_a_failure():
    assert "already installed - fine for a re-install" in READINESS


def test_venv_is_built_with_copies_not_hard_links():
    """Trial box 2026-10-15: uv hard-linked .venv packages to its cache in the admin's
    profile; a hard link shares that file's permissions, so the service account got
    'Permission denied' and the agent did not start. The readiness probe caught it."""
    assert "sync --frozen --no-dev --link-mode copy" in READINESS


def test_runtime_rights_reach_existing_files_on_an_upgrade():
    """Trial box 2026-10-15: logs/DB created earlier by LocalSystem kept their old rights,
    NSSM (as the account) could not open agent_svc.log and the agent did not start."""
    common = (ROOT / "deploy" / "_common.ps1").read_text(encoding="utf-8")
    assert '(OI)(CI)M" /T /C /Q' in common and "Grant-AgentRights" in INSTALLER


class TestProbeCloud:
    """R-12: the first cloud proof happens in the readiness probe, AS the service account."""

    def _cloud_cfg(self, tmp_path):
        cfg = _config(tmp_path)
        key = tmp_path / "key.json"
        key.write_text("{}", encoding="utf-8")
        text = cfg.read_text(encoding="utf-8").replace("cloud:\n  enabled: false\n",
                                                       "cloud:\n  enabled: true\n  bucket: my-bucket-1\n  project_number: '12345'\n")
        text = text.replace("gcs_key_path: ''", f"gcs_key_path: '{key}'")
        cfg.write_text(text, encoding="utf-8")
        return cfg

    def test_cloud_leg_off_skips_the_cloud_check(self, probe, tmp_path):
        result = tmp_path / "result.txt"
        probe.main(["p", str(_config(tmp_path)), str(result)])
        assert any(x.startswith("SKIP cloud bucket") for x in _lines(result))

    def test_bucket_read_and_write_are_proven(self, probe, tmp_path, monkeypatch):
        from core import cloud_preflight, permissions
        calls = []
        monkeypatch.setattr(cloud_preflight, "run_cloud_dry_run",
                            lambda **kw: calls.append(("read", kw["bucket"], kw["fy_prefix"])) or {"ok": True})
        monkeypatch.setattr(permissions, "probe_cloud_write",
                            lambda bucket, run_id, conf, timeout, artefacts, error_tail_lines=100:
                            calls.append(("write", bucket)))
        result = tmp_path / "result.txt"
        code = probe.main(["p", str(self._cloud_cfg(tmp_path)), str(result)])
        assert code == 0, _lines(result)
        assert ("read", "my-bucket-1", "FY26-27") in calls and ("write", "my-bucket-1") in calls
        assert any(x.startswith("OK cloud bucket") for x in _lines(result))

    def test_a_bucket_the_account_cannot_read_fails_with_the_reason(self, probe, tmp_path, monkeypatch):
        from core import cloud_preflight
        monkeypatch.setattr(cloud_preflight, "run_cloud_dry_run",
                            lambda **kw: {"ok": False, "error": "403 forbidden"})
        result = tmp_path / "result.txt"
        assert probe.main(["p", str(self._cloud_cfg(tmp_path)), str(result)]) == 1
        [line] = [x for x in _lines(result) if x.startswith("FAIL")]
        assert line.startswith("FAIL cloud bucket") and "403 forbidden" in line

    def test_a_bucket_the_account_cannot_write_fails(self, probe, tmp_path, monkeypatch):
        from core import cloud_preflight, permissions
        monkeypatch.setattr(cloud_preflight, "run_cloud_dry_run", lambda **kw: {"ok": True})

        def denied(*a, **k):
            raise permissions.PermissionCheckError("Cloud write check failed")
        monkeypatch.setattr(permissions, "probe_cloud_write", denied)
        result = tmp_path / "result.txt"
        assert probe.main(["p", str(self._cloud_cfg(tmp_path)), str(result)]) == 1
        assert any(x.startswith("FAIL cloud bucket") and "Cloud write check failed" in x for x in _lines(result))
