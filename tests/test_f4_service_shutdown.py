# tests/test_f4_service_shutdown.py
r"""F4 remediation regression tests: NSSM graceful shutdown config contract.

Evidence base (all under C:\ChaosTest\evidence\):
  * F2F4_f4_phase1_idle_stop_20260908_2315.txt — DIRECTLY REPRODUCED on the
    isolated AamF4Rig service (production-identical config + identical NSSM
    binary): with AppRotateOnline=1 the service stays STOP_PENDING 120s+ after
    every child has exited cleanly (nssm.exe alone persists, NSSM event IDs
    1040/1011/1023/1027).
  * A/B/A matrix (F2_F4_LONG_HORIZON_FINAL_REPORT.md §7): RotateOnline=0 ->
    STOPPED in ~1.1s under the SAME 15s stop budget; RotateOnline=1 ->
    STOP_PENDING 75s+ again. Rejected alternatives: stop-budget overrun
    (stop takes 1.1s), uv orphaning (zero orphans in every trial), Session-0
    Window/Threads no-ops (not the long pole).

Contract this file pins (deploy/06_install_services.ps1, v1.1.2):
  * Agent: AppStopMethodConsole=180000 (180s is a MAXIMUM grace allowance, not
    a sleep — clean exits return immediately, observed 1.1-1.2s),
    AppStopMethodWindow=0, AppStopMethodThreads=0, AppStopMethodSkip=6.
  * Skip=6 = Window(2)+Threads(4) skipped; Console(1) and the final
    TerminateProcess fallback (bit 8) RETAINED — stuck trees are still reaped.
  * AppRotateOnline=0 on ALL THREE services (same proven root cause, identical
    binary + config pattern); restart-time rotation retained
    (AppRotateFiles=1 + AppRotateBytes=10485760).
  * PrefectServer/Watchdog stop timeouts intentionally UNCHANGED
    (15000/15000/15000) — no symmetry changes without evidence.
  * NSSM -> uv.exe -> python architecture UNCHANGED (no redesign unless
    evidence forces it — the evidence did not).

Static verification of the installer script (the script IS the deployment
artifact; live-service proof is recorded in the evidence files, and these
tests must stay hermetic — they never touch a live service).
"""
import re
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "deploy" / "06_install_services.ps1"

SERVICE_VARS = {
    "SVC_SERVER": "AamPrefectServer",
    "SVC_AGENT": "AamBackupAgent",
    "SVC_WATCHDOG": "AamWatchdog",
}


def _load_script():
    return INSTALLER.read_text(encoding="utf-8")


def _settings():
    """Parse `& $NSSM set $SVC_X Param value` lines -> {service: {param: value}}."""
    text = _load_script()
    out = {svc: {} for svc in SERVICE_VARS.values()}
    for m in re.finditer(
        r"&\s*\$NSSM\s+set\s+\$(SVC_\w+)\s+([A-Za-z]\w*)\s+(.+)",
        text,
    ):
        svc_var, param, raw_value = m.group(1), m.group(2), m.group(3).strip()
        if svc_var not in SERVICE_VARS:
            continue
        value = raw_value.strip('"')
        out[SERVICE_VARS[svc_var]][param] = value
    return out


def _int(settings, service, param):
    value = settings[service].get(param)
    assert value is not None, f"{service}: {param} not found in installer"
    return int(value)


def test_installer_file_exists():
    assert INSTALLER.is_file()


def test_agent_stop_config_is_180s_console_only():
    s = _settings()
    assert _int(s, "AamBackupAgent", "AppStopMethodConsole") == 180000
    assert _int(s, "AamBackupAgent", "AppStopMethodWindow") == 0
    assert _int(s, "AamBackupAgent", "AppStopMethodThreads") == 0


def test_agent_180000_ms_is_exactly_180_seconds():
    s = _settings()
    assert _int(s, "AamBackupAgent", "AppStopMethodConsole") == 180 * 1000


def test_agent_skip_bits_skip_window_and_threads_only():
    """Skip=6: Window(2)+Threads(4) skipped; Console(1) and the final
    TerminateProcess fallback (bit 8) must be RETAINED."""
    s = _settings()
    skip = _int(s, "AamBackupAgent", "AppStopMethodSkip")
    assert skip == 6
    assert skip & 1 == 0  # Console NOT skipped (the one effective stop method)
    assert skip & 2 == 2  # Window skipped (no-op for a headless console app)
    assert skip & 4 == 4  # Threads skipped (no-op)
    assert skip & 8 == 0  # TerminateProcess fallback RETAINED (stuck trees reaped)


def test_online_rotation_disabled_on_all_three_services():
    """Root cause of STOP_PENDING: NSSM's online-rotation pipe-reader thread
    outlives the app tree. RotateOnline=0 on every service (identical nssm
    binary + same-file stdout/stderr pattern on all three)."""
    s = _settings()
    for service in ("AamPrefectServer", "AamBackupAgent", "AamWatchdog"):
        assert _int(s, service, "AppRotateOnline") == 0, service


def test_restart_time_rotation_retained_on_all_three_services():
    s = _settings()
    for service in ("AamPrefectServer", "AamBackupAgent", "AamWatchdog"):
        assert _int(s, service, "AppRotateFiles") == 1, service
        assert _int(s, service, "AppRotateBytes") == 10485760, service
        # append disposition kept (4 = APPEND) so stop-time logs are not lost
        assert _int(s, service, "AppStdoutCreationDisposition") == 4, service


def test_server_and_watchdog_stop_timeouts_unchanged():
    """No symmetry changes: only the agent needed the 180s max grace (it is
    the service with in-flight Prefect flow runs). Server/watchdog keep 15s."""
    s = _settings()
    for service in ("AamPrefectServer", "AamWatchdog"):
        assert _int(s, service, "AppStopMethodConsole") == 15000, service
        assert _int(s, service, "AppStopMethodWindow") == 15000, service
        assert _int(s, service, "AppStopMethodThreads") == 15000, service
        # Absent AppStopMethodSkip means NSSM default 0 (nothing skipped) —
        # only the agent sets an explicit Skip.
        skip = s[service].get("AppStopMethodSkip", "0")
        assert int(skip) == 0, service


def test_venv_python_process_chain():
    """WP16 (M4 4.5): NSSM -> .venv\\Scripts\\python.exe (a launcher that starts
    the base interpreter as its child - the same two-process shape as the old
    NSSM -> uv.exe -> python chain, which F4 showed does not orphan). No uv at
    boot: `uv run` synced packages from the internet on every start."""
    text = _load_script()
    apps = re.findall(r"&\s*\$NSSM\s+install\s+\$(SVC_\w+)\s+(\S+)", text)
    assert len(apps) == 3
    for svc_var, app in apps:
        assert app == "$VenvPython", f"{svc_var}: Application must be $VenvPython"
    s = _settings()
    assert s["AamBackupAgent"]["AppParameters"] == "launch.py"
