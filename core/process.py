"""Cross-platform process utilities — PID alive check, binary resolution."""

import shutil
from pathlib import Path

import psutil


def pid_alive(pid: int) -> bool:
    """Check if a process is alive (cross-platform)."""
    return psutil.pid_exists(pid)


def resolve_binary(name: str) -> str | None:
    """Resolve binary path, checking local deploy/bin first, then system PATH."""
    project_root = Path(__file__).parent.parent
    local_bin = project_root / "deploy" / "bin" / name
    
    # Check exact name
    if local_bin.exists() and local_bin.is_file():
        return str(local_bin)
        
    # Check with .exe on Windows
    if not name.lower().endswith(".exe"):
        local_exe = local_bin.with_suffix(".exe")
        if local_exe.exists() and local_exe.is_file():
            return str(local_exe)
            
    # Fallback to system PATH
    return shutil.which(name)
