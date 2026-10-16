"""LAN preflight — robocopy /L dry-run before real /MIR sync.

Validates UNC reachability, permissions, and junction point handling
before committing to a multi-hour copy.
"""

import socket
import time
from pathlib import Path

from loguru import logger

from core.health import HealthError  # P1-EXC: THE domain exception - single identity
from core.lan_sync import (
    _read_log_tail,
    scan_robocopy_log,
    summarize_log_bytes,
    summarize_log_counts,
)
from core.permissions import PROBE_PATTERN
from core.process import resolve_binary
from core.run_artefacts import RunArtefacts
from core.smb_ready import wait_for_smb_ready
from core.tool_runner import run_tool
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


def run_lan_dry_run(
    source: str,
    dest: str,
    timeout: int = model_default(LanConfig, "dry_run_timeout_seconds"),
    smb_attempts: int = model_default(LanConfig, "preflight_smb_attempts"),
    smb_wait_seconds: float = model_default(LanConfig, "preflight_smb_wait_seconds"),
    smb_connect_timeout_seconds: float = model_default(WolConfig, "smb_connect_timeout_seconds"),
    artefacts: RunArtefacts | None = None,
    log_tail_kb: int = model_default(LanConfig, "log_tail_kb"),
) -> dict:
    """Run robocopy in list-only mode to validate paths and permissions.

    The FULL listing is written with /UNILOG into the run's artefact folder
    and kept (plan WP2/WP4): it is the evidence behind a guard decision
    (every file a mirror would delete). run_tool kills the process tree on
    timeout. log_tail_kb bounds the error text (lan.log_tail_kb).

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
         "planned_deletes": number of "*EXTRA File" lines,
         "canary_present": mount marker is a regular file (guard G5),
         "bytes": Bytes-row counts (/BYTES, exact) - "copied" feeds the
                  NAS free-space check, or None when unreadable}
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
        # M2 2.3 (G5): no longer a hard failure here. The marker state is
        # returned and guard G5 BLOCKS the run for a human decision; an
        # approval on the dashboard recreates the marker, so the decision is
        # recorded. Only the read-only /L listing below still runs.
        logger.error(
            f"Mount marker {canary_file} missing or not a regular file - guard G5 "
            "will block this run until a person confirms the destination on the "
            "dashboard. Nothing will be copied or deleted.")

    robocopy_exe = resolve_binary("robocopy") or "robocopy"
    cmd = [
        robocopy_exe,
        source,
        dest,
        "/L", "/MIR", "/XJ",
        "/NJH", "/NP", "/BYTES",
        "/XF", ".AAM_TARGET_MOUNTED", PROBE_PATTERN,
        "/XD", "System Volume Information", "$RECYCLE.BIN",
    ]
    artefacts = artefacts or RunArtefacts.ephemeral("lan-dry-run")
    log_path = artefacts.new_path("robocopy-dry-run", "log")
    cmd.append(f"/UNILOG:{log_path}")

    logger.info(f"LAN dry-run: validating {source} → {dest} (full log: {log_path})")
    result = run_tool("robocopy-dry-run", cmd, timeout_s=timeout,
                      artefacts=artefacts, log_path=log_path)

    if result.spawn_error:
        missing = "FileNotFoundError" in result.spawn_error
        logger.error(f"LAN dry-run could not start robocopy: {result.spawn_error}")
        return {"ok": False, "exit_code": -1,
                "error": "robocopy.exe not found" if missing else result.spawn_error}
    if result.timed_out:
        logger.error(f"LAN dry-run timed out after {timeout}s (full log: {log_path})")
        return {"ok": False, "exit_code": -1, "error": f"Timeout after {timeout}s"}

    code = result.exit_code
    # Exit codes 0-7: success (various file-change combinations).
    # Exit codes 8+: error (copy failures, fatal errors).
    if code >= 8:
        tail = _read_log_tail(log_path, int(log_tail_kb) * 1024, encoding="utf-16").strip()
        error_output = tail or result.stdout_text().strip() or "no output"
        logger.error(f"LAN dry-run failed (exit {code}); full log: {log_path}\n{error_output}")
        return {"ok": False, "exit_code": code,
                "error": f"Robocopy /L failed with exit {code}\nOutput: {error_output}"}

    planned_deletes, summary = scan_robocopy_log(log_path)
    counts = summarize_log_counts(summary)
    logger.info(f"LAN dry-run passed (exit {code}); summary Files row: {counts}; "
                f"planned deletions (*EXTRA File lines): {planned_deletes}; full log: {log_path}")
    return {"ok": True, "exit_code": code, "error": None, "counts": counts,
            "planned_deletes": planned_deletes, "canary_present": canary_valid,
            "bytes": summarize_log_bytes(summary), "log_path": str(log_path)}
