"""Cross-platform process utilities — PID alive check, lock helpers."""

import os
import subprocess
import sys


def pid_alive(pid: int) -> bool:
    """Check if a process is alive (cross-platform).

    Uses os.kill(pid, 0) on POSIX, falls back to tasklist on Windows.
    """
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return f"{pid}" in result.stdout
        except Exception:
            return False
    return False
