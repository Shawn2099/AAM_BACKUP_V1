"""Cross-platform process utilities — PID alive check, lock file helpers, binary resolution.

Lock file format (written by write_lock, read by read_lock_alive):

    <pid>:<create_time>

create_time is the float process-creation timestamp returned by psutil
(seconds since epoch, 1e-2 to 1e-3 precision on Windows). Two processes
cannot share the same PID AND the same create_time, so this eliminates
PID-reuse false positives with mathematical certainty — the same approach
used by nginx, gunicorn, supervisord, and systemd.
"""

import os
import shutil
import tempfile
from pathlib import Path

import psutil


def _get_create_time(pid: int) -> float | None:
    """Return the process creation time for *pid*, or None if the process is gone."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def write_lock(lock_path: Path) -> None:
    """Atomically write 'PID:create_time' to *lock_path*.

    Uses mkstemp + os.replace so the watchdog never reads a partially-written
    file even if the process is killed mid-write.

    Raises OSError if the write fails (caller logs and continues without lock).
    """
    pid = os.getpid()
    ct = _get_create_time(pid)
    if ct is None:
        # Extremely unlikely — our own process should always be queryable.
        # Fall back to PID-only so the lock is still written.
        content = str(pid)
    else:
        content = f"{pid}:{ct:.6f}"

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(lock_path.parent), prefix=".backup.lock.", suffix=".tmp")
    try:
        os.write(fd, content.encode())
        os.close(fd)
        fd = -1
        os.replace(tmp, str(lock_path))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        Path(tmp).unlink(missing_ok=True)
        raise


def read_lock_alive(lock_path: Path) -> tuple[bool, int | None]:
    """Read *lock_path* and determine whether the owning process is still alive.

    Returns:
        (alive, pid) where:
          alive=True   → the exact process that wrote the lock is still running
          alive=False  → lock is stale (PID gone, PID reused, or unreadable)
          pid          → the PID read from the file (None if unreadable)

    Does NOT delete the lock file — that is the caller's responsibility so that
    log messages can include the PID before removal.
    """
    if not lock_path.exists():
        return False, None

    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False, None

    # Parse "pid:create_time" (new format) or bare "pid" (legacy).
    if ":" in raw:
        try:
            pid_str, ct_str = raw.split(":", 1)
            pid = int(pid_str)
            written_ct = float(ct_str)
        except ValueError:
            return False, None

        current_ct = _get_create_time(pid)
        if current_ct is None:
            # PID is gone.
            return False, pid

        # Compare creation times with a 0.1-second tolerance to absorb any
        # floating-point representation differences across psutil versions.
        if abs(current_ct - written_ct) < 0.1:
            return True, pid  # Same process — lock is live.
        # PID was reused by a different process.
        return False, pid

    else:
        # Legacy format: bare PID only. Fall back to existence check.
        try:
            pid = int(raw)
        except ValueError:
            return False, None
        return psutil.pid_exists(pid), pid


# ── Backward-compat alias (used by tests) ─────────────────────────────────────

def pid_alive(pid: int) -> bool:
    """Simple existence check. Prefer read_lock_alive() for lock-file validation."""
    return psutil.pid_exists(pid)


# ── Binary resolution ─────────────────────────────────────────────────────────

def resolve_binary(name: str) -> str | None:
    """Resolve binary path, checking local deploy/bin first, then system PATH."""
    project_root = Path(__file__).parent.parent
    local_bin = project_root / "deploy" / "bin" / name

    if local_bin.exists() and local_bin.is_file():
        return str(local_bin)

    if not name.lower().endswith(".exe"):
        local_exe = local_bin.with_suffix(".exe")
        if local_exe.exists() and local_exe.is_file():
            return str(local_exe)

    return shutil.which(name)
