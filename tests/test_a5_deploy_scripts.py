"""A5 (final audit 2026-10-20): deploy scripts must not widen the firewall or silently restore from a default bucket.

* 03_setup_system.bat opened 8080 and 4200 to ANY address with no duplicate check, so re-running it after
  06_install_services.ps1 undid the LocalSubnet scope of the dashboard rule and exposed Prefect (4200 has no
  login and must stay loopback-only). 06 owns the firewall.
* 09_restore_from_gcs.bat ran `uv run` (syncs the live .venv incl. the dev group, needs the internet, re-creates the
  hard-link ACL problem that crash-looped the agent on 2026-10-19) and, when that failed, silently restored from a
  hard-coded bucket `aam-cloudbackup` - a disaster-recovery restore must never guess the bucket."""

import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def _text(name):
    return (DEPLOY / name).read_text(encoding="utf-8", errors="replace")


def _code_lines(text, comment):
    return [ln for ln in text.splitlines() if not ln.strip().lower().startswith(comment)]


def test_03_does_not_open_any_firewall_port():
    bat = "\n".join(_code_lines(_text("03_setup_system.bat"), "::"))
    assert "New-NetFirewallRule" not in bat
    assert not re.search(r"LocalPort\s+(4200|8080)", bat)


def test_06_owns_the_dashboard_rule_and_removes_a_stale_prefect_rule():
    ps1 = _text("06_install_services.ps1")
    assert "-RemoteAddress $dashRemote" in ps1                      # the scoped dashboard rule
    assert "AAM Backup Prefect" in ps1 and "Remove-NetFirewallRule" in ps1
    stale = [ln for ln in ps1.splitlines() if "AAM Backup Prefect" in ln]
    assert stale and not any("New-NetFirewallRule" in ln for ln in stale)    # removed, never (re)created


def test_09_restore_does_not_use_uv_run():
    bat = "\n".join(_code_lines(_text("09_restore_from_gcs.bat"), "::"))
    assert " run --quiet " not in bat and "UV_EXE" not in bat
    assert r".venv\Scripts\python.exe" in bat


def test_09_restore_never_guesses_the_bucket():
    bat = "\n".join(_code_lines(_text("09_restore_from_gcs.bat"), "::"))
    assert 'set "BUCKET_NAME=aam-cloudbackup"' not in bat
    assert "--default aam-cloudbackup" not in bat
    assert "set /p BUCKET_NAME=" in bat                              # unreadable config -> the operator types it
    assert re.search(r'if "!?%?BUCKET_NAME%?!?"==""', bat)           # and an empty answer stops the restore
