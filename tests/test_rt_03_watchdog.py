import os
import subprocess
import time
import pytest
from pathlib import Path
import psutil
import threading
import msvcrt

from core.process import write_lock, read_lock_alive, pid_alive
from watchdog import _transfer_process_running

from tests.e2e_helpers import (
    cfg,
    source_test_dir,
    nas_test_dir,
)

def test_wd_01_lock_write_read():
    """WD-01: Lock Write and Read Round-Trip."""
    lock_path = Path("test_backup.lock")
    try:
        write_lock(lock_path)
        
        alive, pid = read_lock_alive(lock_path)
        
        assert alive is True
        assert pid == os.getpid()
        
        # Read raw content
        raw = lock_path.read_text(encoding="utf-8")
        assert ":" in raw
        pid_str, ct_str = raw.split(":", 1)
        assert int(pid_str) == pid
        assert float(ct_str) > 0
    finally:
        lock_path.unlink(missing_ok=True)


def test_wd_02_stale_lock():
    """WD-02: Stale Lock — Process No Longer Exists."""
    lock_path = Path("test_backup_stale.lock")
    try:
        # Spawn short lived process
        proc = subprocess.Popen(["ping", "127.0.0.1", "-n", "1"], stdout=subprocess.DEVNULL)
        pid = proc.pid
        ct = psutil.Process(pid).create_time()
        proc.wait()
        
        # Write fake lock file
        lock_path.write_text(f"{pid}:{ct:.6f}")
        
        alive, read_pid = read_lock_alive(lock_path)
        
        assert alive is False
        assert read_pid == pid
    finally:
        lock_path.unlink(missing_ok=True)


def test_wd_03_pid_reuse():
    """WD-03: PID Reuse — Same PID, Different Process."""
    lock_path = Path("test_backup_reuse.lock")
    try:
        my_pid = os.getpid()
        # Same PID, but fake create time (0.0) -> simulated PID reuse
        lock_path.write_text(f"{my_pid}:0.0")
        
        alive, read_pid = read_lock_alive(lock_path)
        
        assert alive is False
        assert read_pid == my_pid
    finally:
        lock_path.unlink(missing_ok=True)


def test_wd_04_av_locked_file():
    """WD-04: AV-Locked File — Fail-Safe to Alive."""
    lock_path = Path("test_backup_av.lock")
    try:
        # Create it first
        lock_path.write_text(f"{os.getpid()}:0.0")
        
        fd = os.open(str(lock_path), os.O_RDWR)
        try:
            # Place OS lock
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 10)
            
            alive, pid = read_lock_alive(lock_path)
            
            assert alive is True
            assert pid == -1
            
        finally:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 10)
            os.close(fd)
    finally:
        lock_path.unlink(missing_ok=True)


def test_wd_05_transfer_process_detection():
    """WD-05: Watchdog _transfer_process_running() Detects Real Robocopy."""
    source = source_test_dir()
    dest = nas_test_dir()
    
    source.mkdir(parents=True, exist_ok=True)
    # Create a 20MB file so robocopy takes a moment
    with open(source / "slow_copy.bin", "wb") as f:
        f.write(os.urandom(20 * 1024 * 1024))
    
    proc = subprocess.Popen(["robocopy", str(source), str(dest), "/MIR", "/W:5", "/R:0"])
    try:
        found = False
        for _ in range(50):
            if _transfer_process_running():
                found = True
                break
            time.sleep(0.1)
        assert found is True
    finally:
        proc.kill()
        proc.wait()
        
    # Wait for process to really die
    time.sleep(1)
    # Check again (assuming no other robocopy running on the system)
    # Be careful not to assert false if the system happens to be running a legit backup
    # But for a sandbox server this should be fine.
    # Note: on a slow VM killing might take a sec
    assert _transfer_process_running() is False


def test_wd_06_lock_file_atomic_write():
    """WD-06: Lock File Atomic Write — No Partial Read Possible."""
    lock_path = Path("test_backup_atomic.lock")
    
    stop_event = threading.Event()
    
    def writer_thread():
        while not stop_event.is_set():
            try:
                write_lock(lock_path)
            except Exception:
                pass
            time.sleep(0.01)
            
    t = threading.Thread(target=writer_thread)
    t.start()
    
    try:
        for _ in range(100):
            try:
                raw = lock_path.read_text()
                if raw:
                    assert ":" in raw
                    pid_str, ct_str = raw.split(":", 1)
                    int(pid_str)
                    float(ct_str)
            except (FileNotFoundError, PermissionError):
                pass
            time.sleep(0.01)
    finally:
        stop_event.set()
        t.join()
        lock_path.unlink(missing_ok=True)
