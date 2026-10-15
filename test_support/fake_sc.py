"""Fake sc.exe for watchdog tests (plan WP13).

The watchdog runs sc.exe through core.services -> core.tool_runner.run_capture.
`fake_sc(return_value=..., side_effect=...)` mirrors unittest.mock.patch's API
for the old `watchdog.subprocess.run` seam: the fake receives the sc argv
and returns an object with `returncode` / `stdout` (/ `stderr`), or raises
(subprocess.TimeoutExpired -> timed out; any other exception -> spawn error).
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from core.tool_runner import CaptureResult


def adapt(func):
    """Turn a subprocess.run-style fake into a run_capture-style one."""
    def run_capture(argv, *, timeout_s, max_chars=4000):
        argv = [str(a) for a in argv]
        try:
            r = func(argv)
        except subprocess.TimeoutExpired:
            return CaptureResult(argv, None, True, None, "")
        except Exception as e:  # noqa: BLE001 - mirrors a failed spawn
            return CaptureResult(argv, None, False, f"{type(e).__name__}: {e}", "")
        rc = getattr(r, "returncode", 0)
        out = getattr(r, "stdout", "")
        err = getattr(r, "stderr", "")
        text = (out if isinstance(out, str) else "") + (err if isinstance(err, str) else "")
        return CaptureResult(argv, rc if type(rc) is int else 0, False, None, text[:max_chars])
    return run_capture


def fake_sc(return_value=None, side_effect=None):
    if side_effect is not None:
        if isinstance(side_effect, BaseException) or (
                isinstance(side_effect, type) and issubclass(side_effect, BaseException)):
            exc = side_effect

            def func(argv):
                raise exc
        else:
            def func(argv):
                return side_effect(argv)
    else:
        def func(argv):
            return return_value
    return patch("core.services.run_capture", adapt(func))
