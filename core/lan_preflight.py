"""LAN preflight — robocopy /L dry-run before real /MIR sync.

Validates UNC reachability, permissions, and junction point handling
before committing to a multi-hour copy.
"""

import socket
import subprocess
from pathlib import Path

from loguru import logger
import time

from core.smb_ready import wait_for_smb_ready

from core.health import HealthError  # P1-EXC: THE domain exception - single identity
from core.lan_sync import count_extra_file_lines, summarize_log_counts
from core.process import resolve_binary


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
    timeout: float = 15.0,
    attempts: int = 8,
    wait_s: float = 15.0,
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


def run_lan_dry_run(
    source: str,
    dest: str,
    timeout: int = 900,
    smb_attempts: int = 8,
    smb_wait_seconds: float = 15.0,
    smb_connect_timeout_seconds: float = 15.0,
) -> dict:
    """Run robocopy in list-only mode to validate paths and permissions.

    /L = list-only — reports what WOULD happen, zero bytes moved.
    /MIR = mirror logic (same as real run).
    /XJ = exclude junction points.
    /NJH /NP = no job header, no progress. The job SUMMARY is kept (M2 2.2):
    Files Total = source file count (G2). Planned deletions (G3) = the
    "*EXTRA File" lines, NOT the Extras column: that column also counts
    /XF-excluded destination files (the mount marker) which /MIR keeps
    (verified on robocopy 10.0, 2026-10-14).

    Args:
        source: Source drive path (e.g. "D:\\").
        dest: LAN UNC destination.
        timeout: Max seconds for dry-run (default 300s).

    Returns:
        {"ok": bool, "exit_code": int, "error": str | None,
         "counts": summarize_log_counts() dict, or None when the summary
                   is missing/unreadable (the guards then fail the run),
         "planned_deletes": number of "*EXTRA File" lines}
    """
    # 1. Fast SMB Reachability Probe for UNC destinations (bypasses 45s OS hang)
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
        raise HealthError(msg)

    # 2. Strict Canary File Verification (.is_file() instead of .exists())
    dest_path = Path(dest)
    canary_file = dest_path / ".AAM_TARGET_MOUNTED"
    try:
        canary_valid = canary_file.is_file()
    except OSError as e:
        msg = f"Cannot access LAN destination '{dest}': {e}"
        logger.error(msg)
        raise HealthError(msg) from e

    if not canary_valid:
        # G11: make the failure self-recovering.
        msg = (
            f"Canary file {canary_file} missing or is not a regular file — refusing to mirror into an "
            "unverified destination. Recovery: verify the FY share is mounted, "
            f"then create the canary:  cmd /c type nul > \"{canary_file}\"  "
            "(or run deploy\\10_recreate_canary.bat from the project's deploy "
            "folder). NOTE: if this file is missing on a destination that "
            "already holds backup data, check why before re-creating it — a "
            "deleted canary on a populated share usually means the share or "
            "mount was touched manually."
        )
        logger.error(msg)
        raise HealthError(msg)

    robocopy_exe = resolve_binary("robocopy") or "robocopy"
    cmd = [
        robocopy_exe,
        source,
        dest,
        "/L", "/MIR", "/XJ",
        "/NJH", "/NP",
        "/XF", ".AAM_TARGET_MOUNTED",
        "/XD", "System Volume Information", "$RECYCLE.BIN",
    ]

    logger.info(f"LAN dry-run: validating {source} → {dest}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

        code = result.returncode
        # Exit codes 0-7: success (various file-change combinations).
        # Exit codes 8+: error (copy failures, fatal errors).
        ok = code < 8

        if not ok:
            # Robocopy writes errors to stdout, not stderr.
            out_err = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
            error_output = out_err or "no output"
            logger.error(f"LAN dry-run failed (exit {code}): {error_output}")
            return {"ok": False, "exit_code": code, "error": f"Robocopy /L failed with exit {code}\nOutput: {error_output}"}

        counts = summarize_log_counts(result.stdout or "")
        planned_deletes = count_extra_file_lines(result.stdout or "")
        logger.info(f"LAN dry-run passed (exit {code}); summary Files row: {counts}; "
                    f"planned deletions (*EXTRA File lines): {planned_deletes}")
        return {"ok": True, "exit_code": code, "error": None, "counts": counts,
                "planned_deletes": planned_deletes}

    except subprocess.TimeoutExpired:
        logger.error(f"LAN dry-run timed out after {timeout}s")
        return {"ok": False, "exit_code": -1, "error": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        logger.error("robocopy.exe not found")
        return {"ok": False, "exit_code": -1, "error": "robocopy.exe not found"}
    except OSError as e:
        logger.error(f"LAN dry-run OS error: {e}")
        return {"ok": False, "exit_code": -1, "error": str(e)}
