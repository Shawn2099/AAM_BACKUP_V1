"""Cross-platform process utilities — PID alive check, lock helpers."""

import psutil


def pid_alive(pid: int) -> bool:
    """Check if a process is alive (cross-platform)."""
    return psutil.pid_exists(pid)
