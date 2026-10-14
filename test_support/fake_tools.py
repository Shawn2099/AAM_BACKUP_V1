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

from contextlib import contextmanager
from unittest.mock import patch

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
