"""The Google key file is readable only by SYSTEM, Administrators and the service account.

The box had `BUILTIN\\Users:(I)(RX)` on deploy\\keys\\aam-gcs-key.json (inherited from C:\\), so any
account that could log on could read the key. `deploy/lock_gcs_key.ps1` removes that; the installer
runs it before it touches any service. These tests run the real script with the real `icacls` on a
throw-away file (Windows only).
"""
import getpass
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "lock_gcs_key.ps1"
INSTALLER = (ROOT / "deploy" / "06_install_services.ps1").read_text(encoding="utf-8")

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="icacls is Windows only")


def _run(key: Path, account: str = ""):
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
           "-KeyPath", str(key)]
    if account:
        cmd += ["-Account", account]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _acl(path: Path) -> str:
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    return out.replace(str(path), "")                          # the path itself contains "Users"


def _open_to_users(path: Path):
    """What the box had: the Users group may read the key (S-1-5-32-545)."""
    subprocess.run(["icacls", str(path), "/grant", "*S-1-5-32-545:(R)", "/Q"], check=True,
                   capture_output=True)


@pytest.fixture
def key(tmp_path):
    p = tmp_path / "aam-gcs-key.json"
    p.write_text('{"type": "service_account"}', encoding="utf-8")
    return p


@windows_only
def test_users_lose_access_and_the_owner_keeps_it(key):
    _open_to_users(key)
    assert "BUILTIN\\Users" in _acl(key)
    me = f"{os.environ['COMPUTERNAME']}\\{getpass.getuser()}"
    r = _run(key, me)
    assert r.returncode == 0, r.stdout + r.stderr
    acl = _acl(key)
    for who in ("BUILTIN\\Users", "Everyone", "Authenticated Users"):
        assert who not in acl
    assert "NT AUTHORITY\\SYSTEM:(F)" in acl and "BUILTIN\\Administrators:(F)" in acl
    assert f"{getpass.getuser()}:(R)" in acl
    assert key.read_text(encoding="utf-8")                     # the account can still read it


@windows_only
def test_without_an_account_only_system_and_administrators_remain(key):
    _open_to_users(key)
    assert _run(key).returncode == 0
    acl = _acl(key)
    assert "SYSTEM" in acl and "Administrators" in acl and "Users" not in acl


@windows_only
def test_it_can_run_twice(key):
    assert _run(key).returncode == 0 and _run(key).returncode == 0


@windows_only
def test_a_missing_key_is_a_warning_not_a_failure(tmp_path):
    r = _run(tmp_path / "nope.json")
    assert r.returncode == 0 and "nothing to lock" in r.stdout


@windows_only
def test_a_bad_account_fails_and_leaves_the_file_open_to_the_administrators(key):
    r = _run(key, "NoSuchAccount12345")
    assert r.returncode == 1 and "not locked" in r.stdout
    assert key.exists()


def test_the_installer_locks_the_key_before_it_touches_a_service():
    assert "lock_gcs_key.ps1" in INSTALLER
    assert INSTALLER.index("lock_gcs_key.ps1") < INSTALLER.index("Stopping any running AAM services")
    assert "paths.gcs_key_path" in INSTALLER
    block = INSTALLER.split("lock_gcs_key.ps1", 1)[1][:600]
    assert "exit 1" in block and "Services were NOT changed" in block
