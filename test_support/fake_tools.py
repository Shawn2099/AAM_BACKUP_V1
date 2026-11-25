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
from pathlib import Path
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
                      "artefacts": artefacts, "env": env})
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

    def __init__(self, **configure):
        # patch(..., new_callable=SubprocessStyleRunTool, side_effect=...) passes
        # its keyword arguments here, as it would to MagicMock.
        object.__setattr__(self, "_mock", MagicMock(**configure))

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


# ── LAN rclone fake (plan P1 T0) ─────────────────────────────────────
#
# Patches `core.lan_sync.run_tool` (the module attribute; created if absent)
# and answers by step name. A script value is one spec dict (used for every
# call of that step) or a LIST of specs (one per successive call; an extra
# call raises AssertionError, so a retry test cannot pass by accident).
#
# Spec keys (unknown keys are rejected):
#   exit_code   int or None (default 0)
#   log         text written to the path after --log-file in argv (default "")
#   combined    text written to the path after --combined in argv; None = no file
#   stderr      text written to the stderr artefact (default "")
#   stdout      text written to the stdout artefact (default "")
#   timed_out   True -> ToolResult.timed_out, exit_code None (default False)
#   spawn_error text -> ToolResult.spawn_error, exit_code None, no log written
#   raise       exception instance/class raised from run_tool instead of returning
#
# The yielded FakeRcloneLan records every call in `.calls` (dict with "step",
# "name", "argv", "timeout_s", "log_path", "artefacts", "env"); `.steps()` lists
# the step names in call order.

LAN_RCLONE_STEPS = ("rclone-lan-plan", "rclone-lan-sync", "rclone-lan-check", "rclone-lan-hash")
_LAN_SPEC_KEYS = frozenset({"exit_code", "log", "combined", "stderr", "stdout",
                            "timed_out", "spawn_error", "raise"})


class FakeRcloneLan:
    """Every call made through core.lan_sync.run_tool, in order."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __len__(self) -> int:
        return len(self.calls)

    def __getitem__(self, index):
        return self.calls[index]

    def __iter__(self):
        return iter(self.calls)

    def steps(self) -> list[str]:
        return [c["step"] for c in self.calls]


def _lan_argv_path(argv: list[str], flag: str) -> Path | None:
    if flag not in argv:
        return None
    index = argv.index(flag)
    return Path(argv[index + 1]) if index + 1 < len(argv) else None


def _lan_checked_specs(step: str, value) -> tuple[bool, list[dict]]:
    """(repeat, specs) for one script entry; rejects malformed entries at setup time."""
    if isinstance(value, dict):
        specs, repeat = [value], True
    elif isinstance(value, list) and value:
        specs, repeat = list(value), False
    else:
        raise TypeError(f"fake_rclone_lan: script[{step!r}] must be a spec dict "
                        "or a non-empty list of spec dicts")
    for spec in specs:
        if not isinstance(spec, dict):
            raise TypeError(f"fake_rclone_lan: script[{step!r}] holds a non-dict spec {spec!r}")
        unknown = set(spec) - _LAN_SPEC_KEYS
        if unknown:
            raise ValueError(f"fake_rclone_lan: unknown spec key(s) {sorted(unknown)} for {step!r}")
    return repeat, specs


@contextmanager
def fake_rclone_lan(script: dict):
    unknown_steps = set(script) - set(LAN_RCLONE_STEPS)
    if unknown_steps:
        raise ValueError(f"fake_rclone_lan: unknown step name(s) {sorted(unknown_steps)}; "
                         f"use one of {LAN_RCLONE_STEPS}")
    queues: dict[str, list[dict]] = {}
    repeats: dict[str, bool] = {}
    for step, value in script.items():
        repeats[step], queues[step] = _lan_checked_specs(step, value)
    fake = FakeRcloneLan()

    def _take(name: str) -> dict:
        if name not in queues:
            raise AssertionError(f"fake_rclone_lan: no spec for step {name!r} "
                                 f"(script has {sorted(queues)})")
        if repeats[name]:
            return queues[name][0]
        if not queues[name]:
            raise AssertionError(f"fake_rclone_lan: step {name!r} called more often "
                                 "than its list of specs")
        return queues[name].pop(0)

    def _fake(name, argv, *, timeout_s, artefacts, log_path=None, env=None, cwd=None):
        argv = [str(a) for a in argv]
        fake.calls.append({"step": name, "name": name, "argv": argv, "timeout_s": timeout_s,
                           "log_path": log_path, "artefacts": artefacts, "env": env})
        spec = _take(name)
        if spec.get("raise") is not None:
            raise spec["raise"]
        out = artefacts.new_path(f"{name}.stdout", "txt")
        err = artefacts.new_path(f"{name}.stderr", "txt")
        out.write_text(spec.get("stdout", ""), encoding="utf-8")
        err.write_text(spec.get("stderr", ""), encoding="utf-8")
        timed_out = bool(spec.get("timed_out", False))
        spawn_error = spec.get("spawn_error")
        if not spawn_error:
            target_log = _lan_argv_path(argv, "--log-file") or log_path
            if target_log is not None:
                target_log.write_text(spec.get("log", ""), encoding="utf-8")
            target_combined = _lan_argv_path(argv, "--combined")
            if target_combined is not None and spec.get("combined") is not None:
                target_combined.write_text(spec["combined"], encoding="utf-8")
        return ToolResult(
            name=name, argv=argv,
            exit_code=None if (timed_out or spawn_error) else spec.get("exit_code", 0),
            timed_out=timed_out, spawn_error=spawn_error,
            stdout_path=out, stderr_path=err, log_path=log_path,
            started_at=now_iso(), ended_at=now_iso(), duration_s=0.0,
            timeout_s=timeout_s,
        )

    with patch("core.lan_sync.run_tool", side_effect=_fake, create=True):
        yield fake
