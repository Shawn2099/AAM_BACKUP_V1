"""LAN preflight — robocopy /L dry-run before real /MIR sync.

Validates UNC reachability, permissions, and junction point handling
before committing to a multi-hour copy.
"""

import subprocess
from pathlib import Path

from loguru import logger

from core.health import HealthError  # P1-EXC: THE domain exception - single identity
from core.process import resolve_binary

def run_lan_dry_run(source: str, dest: str, timeout: int = 300) -> dict:
    """Run robocopy in list-only mode to validate paths and permissions.

    /L = list-only — reports what WOULD happen, zero bytes moved.
    /MIR = mirror logic (same as real run).
    /XJ = exclude junction points.
    /NJH /NJS /NP = minimal output (no headers, summaries, or progress).

    Args:
        source: Source drive path (e.g. "D:\\").
        dest: LAN UNC destination.
        timeout: Max seconds for dry-run (default 300s).

    Returns:
        {"ok": bool, "exit_code": int, "error": str | None}
    """
    dest_path = Path(dest)
    canary_file = dest_path / ".AAM_TARGET_MOUNTED"
    if not canary_file.exists():
        # G11: make the failure self-recovering. The preflight deliberately
        # refuses to run robocopy /MIR against a destination it cannot prove
        # is the mounted FY share — but the fix used to be undocumented. The
        # message now carries the exact recovery command (and the operator can
        # run deploy/10_recreate_canary.bat <dest>).
        msg = (
            f"Canary file {canary_file} missing — refusing to mirror into an "
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
        "/NJH", "/NJS", "/NP",
        "/XF", ".AAM_TARGET_MOUNTED",
        "/XD", "System Volume Information", "$RECYCLE.BIN",
    ]

    logger.info(f"LAN dry-run: validating {source} → {dest}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
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

        logger.info(f"LAN dry-run passed (exit {code})")
        return {"ok": True, "exit_code": code, "error": None}

    except subprocess.TimeoutExpired:
        logger.error(f"LAN dry-run timed out after {timeout}s")
        return {"ok": False, "exit_code": -1, "error": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        logger.error("robocopy.exe not found")
        return {"ok": False, "exit_code": -1, "error": "robocopy.exe not found"}
    except OSError as e:
        logger.error(f"LAN dry-run OS error: {e}")
        return {"ok": False, "exit_code": -1, "error": str(e)}
