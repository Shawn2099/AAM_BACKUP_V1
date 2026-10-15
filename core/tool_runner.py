"""One subprocess wrapper for every external tool (plan §5.1).

Guarantees, for every rclone / robocopy / system-tool invocation:

* stdout and stderr stream to FILES inside the run's artefact folder — the
  full output is kept (WP2) and never accumulated unbounded in memory;
* a timeout kills the whole process tree (gcloud.cmd spawns children) and
  is reported as `timed_out=True`, never as a fake exit code;
* a spawn failure is reported as `spawn_error`, never as a fake exit code;
* `exit_code` is either a real `int` or `None` — never a bool / -1 sentinel;
* every invocation is recorded in the artefact manifest (argv, exit,
  timing, file names).

Decision logic stays in the calling module; this module only executes.

`run_capture` is the same contract for small, frequent status queries
outside a run (the watchdog's `sc` calls every minute): output is captured
in memory, capped at `max_chars`, and no artefact files are written.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from loguru import logger

from core.run_artefacts import RunArtefacts, read_artefact_text
from core.time_utils import now_iso


@dataclass
class ToolResult:
    name: str
    argv: list[str]
    exit_code: int | None
    timed_out: bool
    spawn_error: str | None
    stdout_path: Path
    stderr_path: Path
    log_path: Path | None
    started_at: str
    ended_at: str
    duration_s: float
    timeout_s: float | None = None
    extra: dict = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        """The process ran and exited on its own (any exit code)."""
        return self.exit_code is not None and not self.timed_out and self.spawn_error is None

    def stdout_text(self, encoding: str = "utf-8") -> str:
        return _read(self.stdout_path, encoding)

    def stderr_text(self, encoding: str = "utf-8") -> str:
        return _read(self.stderr_path, encoding)

    def log_text(self, encoding: str = "utf-8") -> str:
        return _read(self.log_path, encoding) if self.log_path else ""

    def describe_failure(self) -> str:
        """Short operator-facing reason when the process did not complete."""
        if self.spawn_error:
            return f"{self.name} could not be started: {self.spawn_error}"
        if self.timed_out:
            return f"{self.name} timed out after {self.timeout_s:.0f}s and was terminated"
        return ""


def _read(path: Path | None, encoding: str) -> str:
    if path is None:
        return ""
    try:
        return read_artefact_text(path, encoding)
    except OSError:
        return ""


def tail_lines(text: str, max_lines: int) -> str:
    """Last `max_lines` lines of `text` (bounded payload for DB/email)."""
    if max_lines <= 0:
        return ""
    lines = (text or "").splitlines()
    return "\n".join(lines[-max_lines:])


def _kill_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.Error:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=30)


@dataclass
class CaptureResult:
    argv: list[str]
    exit_code: int | None
    timed_out: bool
    spawn_error: str | None
    output: str          # stdout + stderr, capped

    @property
    def completed(self) -> bool:
        return self.exit_code is not None and not self.timed_out and self.spawn_error is None

    def describe_failure(self) -> str:
        if self.spawn_error:
            return f"{self.argv[0]} could not be started: {self.spawn_error}"
        if self.timed_out:
            return f"{self.argv[0]} timed out and was terminated"
        return ""


def run_capture(argv: list[str], *, timeout_s: float, max_chars: int = 4000) -> CaptureResult:
    """Run a short status command; same guarantees as run_tool, output in memory."""
    argv = [str(a) for a in argv]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
    except OSError as e:
        return CaptureResult(argv, None, False, f"{type(e).__name__}: {e}", "")
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        logger.error(f"{argv[0]} exceeded {timeout_s}s - terminating process tree")
        _kill_tree(proc.pid)
        try:
            proc.communicate(timeout=30)
        except (subprocess.TimeoutExpired, ValueError):
            pass
        return CaptureResult(argv, None, True, None, "")
    rc = proc.returncode
    text = (out + err).decode("utf-8", errors="replace")
    return CaptureResult(argv, rc if type(rc) is int else None, False, None, text[:max_chars])


def run_tool(
    name: str,
    argv: list[str],
    *,
    timeout_s: float | None,
    artefacts: RunArtefacts,
    log_path: Path | None = None,
    env: dict | None = None,
    cwd: str | os.PathLike | None = None,
) -> ToolResult:
    """Run one external tool; see module docstring for the guarantees.

    `log_path`: a file the tool itself writes (rclone --log-file, robocopy
    /UNILOG) — reserve it with `artefacts.new_path(...)` and pass the same
    path in argv; it is recorded in the manifest.
    """
    stdout_path = artefacts.new_path(f"{name}.stdout", "txt")
    stderr_path = artefacts.new_path(f"{name}.stderr", "txt")
    started_at = now_iso()
    t0 = time.monotonic()
    exit_code: int | None = None
    timed_out = False
    spawn_error: str | None = None
    logger.info(f"Running {name}: {' '.join(str(a) for a in argv[:4])}... "
                f"(artefacts: {artefacts.dir})")
    try:
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            try:
                proc = subprocess.Popen(
                    [str(a) for a in argv], stdout=out, stderr=err,
                    stdin=subprocess.DEVNULL, env=env, cwd=cwd,
                )
            except OSError as e:  # FileNotFoundError included
                spawn_error = f"{type(e).__name__}: {e}"
            else:
                try:
                    rc = proc.wait(timeout=timeout_s)
                    exit_code = rc if type(rc) is int else None
                except subprocess.TimeoutExpired:
                    timed_out = True
                    logger.error(f"{name} exceeded {timeout_s}s - terminating process tree")
                    _kill_tree(proc.pid)
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        logger.error(f"{name} (pid {proc.pid}) did not exit after kill")
    except OSError as e:
        spawn_error = spawn_error or f"output capture failed: {e}"
        artefacts.note(f"{name}: output capture failed: {e}", degraded=True)

    result = ToolResult(
        name=name, argv=[str(a) for a in argv], exit_code=exit_code,
        timed_out=timed_out, spawn_error=spawn_error,
        stdout_path=stdout_path, stderr_path=stderr_path, log_path=log_path,
        started_at=started_at, ended_at=now_iso(),
        duration_s=round(time.monotonic() - t0, 3), timeout_s=timeout_s,
    )
    artefacts.record({
        "tool": name, "argv": result.argv, "exit_code": exit_code,
        "timed_out": timed_out, "spawn_error": spawn_error,
        "started_at": started_at, "ended_at": result.ended_at,
        "duration_s": result.duration_s, "timeout_s": timeout_s,
        "stdout": stdout_path.name, "stderr": stderr_path.name,
        "log": log_path.name if log_path else None,
    })
    if not result.completed:
        logger.error(result.describe_failure())
    else:
        logger.info(f"{name} exited {exit_code} after {result.duration_s:.1f}s")
    return result
