"""Remote backup server shutdown via Windows shutdown.exe.

Reference: AAM_BACKUP_V2/tasks/shutdown_server_task.py — proven logic.
"""

import subprocess

from loguru import logger


def shutdown_server(server_ip: str) -> dict:
    """Send shutdown command to backup server with 5-minute delay.

    Command: shutdown /s /m \\\\SERVER /t 300 /f

    Staff can cancel within 5 minutes by running 'shutdown /a' on target.

    Args:
        server_ip: IPv4 address of the backup server.

    Returns:
        {"shutdown_initiated": bool, "server_ip": str, "error": str | None}
    """
    cmd = [
        "shutdown",
        "/s",
        "/m", f"\\\\{server_ip}",
        "/t", "300",
        "/f",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            logger.info(f"Shutdown initiated for {server_ip} (5-minute delay)")
            return {"shutdown_initiated": True, "server_ip": server_ip, "error": None}

        error_msg = result.stderr.strip() or f"exit code {result.returncode}"
        logger.warning(f"Shutdown command failed for {server_ip}: {error_msg}")
        return {"shutdown_initiated": False, "server_ip": server_ip, "error": error_msg}

    except FileNotFoundError:
        logger.warning("shutdown.exe not found — not running on Windows?")
        return {"shutdown_initiated": False, "server_ip": server_ip, "error": "shutdown.exe not found"}
    except subprocess.TimeoutExpired:
        logger.error(f"Shutdown command timed out for {server_ip}")
        return {"shutdown_initiated": False, "server_ip": server_ip, "error": "timeout"}
    except OSError as e:
        logger.error(f"Shutdown failed: {e}")
        return {"shutdown_initiated": False, "server_ip": server_ip, "error": str(e)}
