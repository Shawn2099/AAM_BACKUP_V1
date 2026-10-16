"""Fake `run_tool` for unit tests of modules built on core.tool_runner.

Usage::

    with fake_run_tool("core.cloud_sync", exit_code=9, log="...json lines...") as calls:
        result = run_cloud_sync(...)
    argv = calls[0]["argv"]

The fake writes `log` into the `log_path` the module reserved (in the
encoding the real tool uses: UTF-16 for robocopy /UNILOG, UTF-8 otherwise),
writes `stderr` into the stderr artefact, and returns a real ToolResult —
so the module's real parsing/decision code runs unchanged.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from core.time_utils import now_iso
from core.tool_runner import ToolResult


@contextmanager
def fake_run_tool(
    module: str,
    *,
    exit_code: int | None = 0,
    log: str = "",
    stderr: str = "",
    stdout: str = "",
    timed_out: bool = False,
    spawn_error: str | None = None,
    log_encoding: str = "utf-8",
):
    calls: list[dict] = []

    def _fake(name, argv, *, timeout_s, artefacts, log_path=None, env=None, cwd=None):
        calls.append({"name": name, "argv": [str(a) for a in argv],
                      "timeout_s": timeout_s, "log_path": log_path,
                      "artefacts": artefacts})
        out = artefacts.new_path(f"{name}.stdout", "txt")
        err = artefacts.new_path(f"{name}.stderr", "txt")
        out.write_text(stdout, encoding="utf-8")
        err.write_text(stderr, encoding="utf-8")
        if log_path is not None and not spawn_error:
            log_path.write_text(log, encoding=log_encoding)
        return ToolResult(
            name=name, argv=[str(a) for a in argv],
            exit_code=None if (timed_out or spawn_error) else exit_code,
            timed_out=timed_out, spawn_error=spawn_error,
            stdout_path=out, stderr_path=err, log_path=log_path,
            started_at=now_iso(), ended_at=now_iso(), duration_s=0.0,
            timeout_s=timeout_s,
        )

    with patch(f"{module}.run_tool", side_effect=_fake):
        yield calls


class SubprocessStyleRunTool:
    """A fake `run_tool` configured like an old `subprocess.run` mock.

    For modules migrated from `subprocess.run` to `run_tool` (plan 5.1),
    so their existing scenario tests keep their intent::

        @patch("core.cloud_verify.run_tool", new_callable=SubprocessStyleRunTool)
        def test_x(self, mock_run):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="...")
            mock_run.side_effect = subprocess.TimeoutExpired("rclone", 30)

    Each call is recorded on an inner mock as `(argv, timeout=timeout_s)`,
    so `call_args[0][0]` is the argv and `call_args[1]["timeout"]` the
    timeout. The configured outcome is translated into a real ToolResult:
    `TimeoutExpired` -> timed_out, `OSError` (incl. FileNotFoundError) ->
    spawn_error "<Type>: <msg>", otherwise exit_code=returncode with the
    stdout/stderr written to the artefact files the module reads back.
    """

    def __init__(self):
        object.__setattr__(self, "_mock", MagicMock())

    def __getattr__(self, item):
        return getattr(self._mock, item)

    def __setattr__(self, key, value):
        setattr(self._mock, key, value)

    def __call__(self, name, argv, *, timeout_s, artefacts, log_path=None, env=None, cwd=None):
        argv = [str(a) for a in argv]
        out = artefacts.new_path(f"{name}.stdout", "txt")
        err = artefacts.new_path(f"{name}.stderr", "txt")
        out.write_text("", encoding="utf-8")
        err.write_text("", encoding="utf-8")
        exit_code, timed_out, spawn_error = None, False, None
        try:
            proc = self._mock(argv, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError as e:
            spawn_error = f"{type(e).__name__}: {e}"
        else:
            rc = proc.returncode
            if type(rc) is not int:
                raise TypeError(f"configure returncode as an int, got {rc!r}")
            exit_code = rc
            out.write_text(proc.stdout or "", encoding="utf-8")
            err.write_text(proc.stderr or "", encoding="utf-8")
        return ToolResult(
            name=name, argv=argv, exit_code=exit_code,
            timed_out=timed_out, spawn_error=spawn_error,
            stdout_path=out, stderr_path=err, log_path=log_path,
            started_at=now_iso(), ended_at=now_iso(), duration_s=0.0,
            timeout_s=timeout_s,
        )


ROBOCOPY_CLEAN_SUMMARY = (
    "\n------------------------------------------------------------------------------\n\n"
    "               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
    "    Dirs :         1         0         1         0         0         0\n"
    "   Files :        {total}         {copied}         {skipped}         0         {failed}         0\n"
    "   Bytes :         {bytes_copied}         {bytes_copied}         0         0         0         0\n"
    "   Ended : Wednesday, October 14, 2026 4:43:36 PM\n"
)


def robocopy_log(total: int = 1, copied: int = 1, skipped: int = 0, failed: int = 0,
                 body: str = "", bytes_copied: int = 0) -> str:
    """A robocopy /UNILOG body with a completed job summary (/BYTES format)."""
    return body + ROBOCOPY_CLEAN_SUMMARY.format(
        total=total, copied=copied, skipped=skipped, failed=failed,
        bytes_copied=bytes_copied)
