"""LAN preflight: destination reachability and mount marker (plan P1 C8).

The dry-run itself is lan_sync.plan_lan_mirror (one rclone --dry-run); this
module only proves the destination is reachable and reports the canary.
"""

import socket
import time
from pathlib import Path

from loguru import logger

from core.faults import coded
from core.health import HealthError  # P1-EXC: THE domain exception - single identity
from core.smb_ready import wait_for_smb_ready
from core.tool_runner import run_tool  # noqa: F401  (tests patch core.lan_preflight.run_tool to prove nothing runs here)
from models.config import LanConfig, WolConfig, model_default


def _extract_unc_host(path_str: str) -> str | None:
    """Extract host/IP from a UNC path (e.g. '\\\\192.168.10.10\\share' or '//nas/share').

    Returns None for local drive paths (e.g. 'D:\\...') or relative paths.
    """
    clean = str(path_str).replace("/", "\\")
    if not clean.startswith(r"\\"):
        return None
    parts = [p for p in clean.lstrip("\\").split("\\") if p]
    return parts[0].strip() if parts else None


def _tcp_probe(host: str, port: int, timeout_s: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, "tcp open"
    except (OSError, socket.timeout, TimeoutError) as e:
        return False, f"tcp {port}: {type(e).__name__}: {e}"


def _is_smb_reachable(
    host: str,
    port: int = 445,
    timeout: float = model_default(WolConfig, "smb_connect_timeout_seconds"),
    attempts: int = model_default(LanConfig, "preflight_smb_attempts"),
    wait_s: float = model_default(LanConfig, "preflight_smb_wait_seconds"),
) -> bool:
    """SMB port reachability with `attempts` tries `wait_s` apart (WP10).

    All values come from config (wol.smb_connect_timeout_seconds,
    lan.preflight_smb_attempts, lan.preflight_smb_wait_seconds); the old
    hard-coded 2 s x 4 (~15 s) budget failed slow-booting NAS units.
    """
    result = wait_for_smb_ready(
        host, probe_path=None, port=port, connect_timeout_s=timeout,
        max_wait_s=0, poll_interval_s=wait_s, stability_wait_s=0,
        min_attempts=max(int(attempts), 1), sleep=time.sleep, tcp_probe=_tcp_probe,
    )
    return result.ready


def check_lan_destination(
    dest: str,
    smb_attempts: int = model_default(LanConfig, "preflight_smb_attempts"),
    smb_wait_seconds: float = model_default(LanConfig, "preflight_smb_wait_seconds"),
    smb_connect_timeout_seconds: float = model_default(WolConfig, "smb_connect_timeout_seconds"),
) -> bool:
    """SMB 445 probe (UNC only) and strict mount-marker check.

    Returns whether the marker is a regular file (guard G5 decides on it).
    An unreachable host or an unreadable marker raises HealthError AAM-NAS-01
    (a FAIL, retried next night, never a guard BLOCK).
    """
    host = _extract_unc_host(dest)
    if host and not _is_smb_reachable(
        host, port=445, timeout=smb_connect_timeout_seconds,
        attempts=smb_attempts, wait_s=smb_wait_seconds,
    ):
        msg = (
            f"Cannot reach LAN host '{host}' on SMB port 445 after {smb_attempts} "
            f"attempt(s) ({smb_connect_timeout_seconds:.0f}s each, "
            f"{smb_wait_seconds:.0f}s apart). "
            "Verify the backup server/NAS is powered on, Wake-on-LAN succeeded, "
            "and network connectivity is operational."
        )
        logger.error(msg)
        raise coded(HealthError(msg), "AAM-NAS-01")

    canary_file = Path(dest) / ".AAM_TARGET_MOUNTED"
    try:
        canary_valid = canary_file.is_file()
    except OSError as e:
        msg = f"Cannot access LAN destination '{dest}': {e}"
        logger.error(msg)
        raise coded(HealthError(msg), "AAM-NAS-01") from e
    if not canary_valid:
        logger.error(
            f"Mount marker {canary_file} missing or not a regular file - guard G5 "
            "will block this run until a person confirms the destination on the "
            "dashboard. Nothing will be copied or deleted.")
    return canary_valid
