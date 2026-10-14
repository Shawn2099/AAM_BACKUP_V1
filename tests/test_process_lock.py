"""Tests for core/process.py — lock management functions.

Covers: pid_alive, write_lock, read_lock_alive, acquire_lock, resolve_binary.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.process import acquire_lock, pid_alive, read_lock_alive, resolve_binary, write_lock


class TestPidAlive:
    def test_current_process_alive(self):
        """Current process is alive."""
        assert pid_alive(os.getpid()) is True

    def test_invalid_pid(self):
        """Invalid PID → False."""
        assert pid_alive(999999) is False

    def test_zero_pid(self):
        """PID 0 on Windows is the System Idle Process (psutil.pid_exists(0) is True).

        pid_alive is a raw existence check; the non-positive guard lives in
        read_lock_alive, which never lets a zero/negative PID own a lock.
        """
        assert pid_alive(0) is True  # Windows platform characteristic


class TestResolveBinary:
    def test_resolve_nonexistent(self):
        """Nonexistent binary → None."""
        result = resolve_binary("definitely_not_a_real_binary_12345")
        assert result is None

    def test_resolve_python(self):
        """Python should be resolvable."""
        result = resolve_binary("python")
        # May or may not be found depending on PATH
        # Just verify it doesn't crash
        assert result is None or isinstance(result, str)


class TestLockFunctions:
    def test_write_and_read_lock(self, tmp_path):
        """Write a lock, then read it back. write_lock(lock_path) takes a Path."""
        lock_path = tmp_path / "test.lock"
        write_lock(lock_path)
        alive, pid = read_lock_alive(lock_path)
        assert alive is True
        assert pid == os.getpid()

    def test_read_nonexistent_lock(self, tmp_path):
        """Nonexistent lock → (False, None)."""
        result = read_lock_alive(tmp_path / "nonexistent.lock")
        assert result == (False, None)

    def test_read_empty_lock(self, tmp_path):
        """Empty lock file → (False, None) (parse fails safe, no exception)."""
        lock_path = tmp_path / "empty.lock"
        lock_path.write_text("")
        # The historical ValueError race is fixed: empty content fails safe.
        result = read_lock_alive(lock_path)
        assert result == (False, None)

    def test_read_corrupt_lock(self, tmp_path):
        """Corrupt content → (False, None)."""
        lock_path = tmp_path / "corrupt.lock"
        lock_path.write_text("not-a-pid")
        assert read_lock_alive(lock_path) == (False, None)

    def test_read_negative_pid_lock(self, tmp_path):
        """Negative PID → stale (False, pid) without psutil raising."""
        lock_path = tmp_path / "neg.lock"
        lock_path.write_text("-1:123.0")
        alive, pid = read_lock_alive(lock_path)
        assert alive is False
        assert pid == -1

    def test_acquire_lock(self, tmp_path):
        """Acquire lock → True (contract: Path in, bool out)."""
        lock_path = tmp_path / "acquire.lock"
        result = acquire_lock(lock_path)
        assert result is True
        assert lock_path.exists()

    def test_acquire_lock_already_held(self, tmp_path):
        """Acquire when a LIVE lock is present → False (never overwritten)."""
        lock_path = tmp_path / "held.lock"
        write_lock(lock_path)  # written by this (live) process
        result = acquire_lock(lock_path)
        assert result is False
        # The live owner's content is untouched.
        alive, pid = read_lock_alive(lock_path)
        assert alive is True and pid == os.getpid()

    def test_acquire_lock_takes_over_dead_owner(self, tmp_path):
        """Acquire when a DEAD owner's lock is present → True (replaced)."""
        lock_path = tmp_path / "dead.lock"
        lock_path.write_text("999999:1234567890.0")  # nobody owns this PID
        result = acquire_lock(lock_path)
        assert result is True
        alive, pid = read_lock_alive(lock_path)
        assert alive is True and pid == os.getpid()
