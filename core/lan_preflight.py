"""LAN preflight — robocopy /L dry-run before real /MIR sync.

Validates UNC reachability, permissions, and junction point handling
before committing to a multi-hour copy.
"""

import subprocess

from loguru import logger


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
    cmd = [
        "robocopy",
        source,
        dest,
        "/L", "/MIR", "/XJ",
        "/NJH", "/NJS", "/NP",
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
        ok = code < 16  # Bit 4 (16) = fatal error

        if not ok:
            stderr_snippet = result.stderr[:200] if result.stderr else "no stderr"
            logger.error(f"LAN dry-run failed (exit {code}): {stderr_snippet}")
            return {"ok": False, "exit_code": code, "error": f"Robocopy /L failed with exit {code}"}

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
