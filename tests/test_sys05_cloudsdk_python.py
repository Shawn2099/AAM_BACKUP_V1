"""SYS-05 - P3-GCP verification: CLOUDSDK_PYTHON machine-scope wiring.

Evidence: REP/FY-10 - bundled deploy/bin/google-cloud-sdk gcloud.cmd needs
Python; without CLOUDSDK_PYTHON every ARCHIVE transition silently no-ops.
deploy/07_configure_gcloud.ps1 sets the Machine-scope variable; services
inherit it after restart.
"""
import os
import subprocess
from pathlib import Path

import pytest

from tests.scenario_support import record_op, real_gate

pytestmark = real_gate()

PROJECT = Path(__file__).resolve().parent.parent
GCLOUD = PROJECT / "deploy" / "bin" / "google-cloud-sdk" / "bin" / "gcloud.cmd"


def _fresh_boot_env() -> dict:
    """Simulate the env a freshly-booted service/session would get.

    A child process inherits its PARENT'S env block - setting a Machine-
    scope value does NOT retroactively appear in existing sessions until
    WM_SETTINGCHANGE/relogon. A fresh session merges HKLM Environment over
    the system defaults, which is exactly what we reconstruct here.
    """
    import ctypes
    import winreg

    def _expand(raw: str) -> str:
        if "%" not in raw:
            return raw
        n = ctypes.windll.kernel32.ExpandEnvironmentStringsW(str(raw), None, 0)
        buf = ctypes.create_unicode_buffer("", max(n, 1))
        ctypes.windll.kernel32.ExpandEnvironmentStringsW(str(raw), buf, max(n, 1))
        return buf.value

    env = {k: v for k, v in os.environ.items() if k != "CLOUDSDK_PYTHON"}
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
    ) as key:
        i = 0
        while True:
            try:
                name, value, vtype = winreg.EnumValue(key, i)
            except OSError:
                break
            value = _expand(value) if isinstance(value, str) else value
            if name.lower() == "path":
                # append machine PATH rather than clobbering the inherited one
                env["PATH"] = env.get("PATH", "") + os.pathsep + str(value)
            else:
                env[name] = str(value)
            i += 1
    return env


def test_SYS_05_cloudsdk_python_machine_scope():
    sid = "SYS-05"
    ops = {}
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as key:
            try:
                machine_val, _ = winreg.QueryValueEx(key, "CLOUDSDK_PYTHON")
            except FileNotFoundError:
                machine_val = ""
        ops["machine_scope_value"] = machine_val
        assert machine_val, (
            f"CLOUDSDK_PYTHON not set at Machine scope - run "
            f"deploy\\07_configure_gcloud.ps1 (op={ops})"
        )
        assert Path(machine_val).exists(), f"points at missing python: {ops}"

        # The decisive probe: bundled gcloud.cmd must work in a process where
        # the variable is NOT manually set (i.e., it inherits Machine scope).
        proc = subprocess.run(
            ["cmd", "/c", str(GCLOUD), "--version"],
            capture_output=True, text=True, timeout=120,
            env=_fresh_boot_env(),
        )
        ops.update({
            "gcloud_rc": proc.returncode,
            "gcloud_head": (proc.stdout or proc.stderr).splitlines()[:2],
            "process_var_cleared": "CLOUDSDK_PYTHON" not in os.environ
                                   or True,  # child env built explicitly
        })
        assert proc.returncode == 0, f"gcloud failed without manual var: {ops}"
        record_op(sid, "PASS", {**ops,
                                "inference": ("bundled gcloud resolves Python "
                                              "via Machine-scope CLOUDSDK_PYTHON")})
    except Exception as e:
        ops.setdefault("error", f"{type(e).__name__}: {e}"[:250])
        record_op(sid, "FAIL", ops)
        raise
