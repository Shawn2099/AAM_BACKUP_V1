"""A6 (final audit 2026-10-20): an interrupted or cancelled flow must not leave the tool running.

`run_tool` waited in ONE blocking Windows wait, so an interrupt (Prefect's cancel sets a flag in the
main thread) was not seen until the tool exited by itself, and nothing killed the child or wrote the
manifest record. Real child processes, real interrupt delivered to the main thread."""

import _thread
import json
import sys
import threading
import time

import psutil
import pytest
from pydantic import ValidationError

from core import tool_runner
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool
from models.config import MaintenanceConfig, model_default

PY = sys.executable
CHILD_SECONDS = 15
SEEN_WITHIN_SECONDS = 8      # far below CHILD_SECONDS, far above any sane poll interval


@pytest.fixture
def art(tmp_path):
    return RunArtefacts(tmp_path / "run_logs", "run-1-cloud", day="2026-10-20")


def _interrupt_main_once_child_runs(pid_file):
    """What Prefect's control listener does on cancel: interrupt the main thread."""
    def fire():
        for _ in range(400):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            time.sleep(0.05)
        _thread.interrupt_main()
    threading.Thread(target=fire, daemon=True).start()


def _long_child(pid_file):
    return [PY, "-c", ("import os, time; "
                       f"open(r'{pid_file}', 'w').write(str(os.getpid())); time.sleep({CHILD_SECONDS})")]


@pytest.mark.parametrize("timeout_s", [None, 600])
def test_interrupt_is_seen_kills_the_child_and_is_recorded(art, tmp_path, timeout_s):
    pid_file = tmp_path / "child.pid"
    _interrupt_main_once_child_runs(pid_file)
    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_tool("slow", _long_child(pid_file), timeout_s=timeout_s, artefacts=art)
    assert time.monotonic() - t0 < SEEN_WITHIN_SECONDS, "the interrupt waited for the tool to finish"
    pid = int(pid_file.read_text())
    assert not psutil.pid_exists(pid), "the tool kept running after the flow was interrupted"
    entry = json.loads((art.dir / "manifest.json").read_text(encoding="utf-8"))["artefacts"][-1]
    assert entry["tool"] == "slow" and entry["interrupted"] == "KeyboardInterrupt"
    assert entry["exit_code"] is None and entry["timed_out"] is False


def test_normal_runs_are_recorded_as_not_interrupted(art):
    r = run_tool("ok", [PY, "-c", "pass"], timeout_s=60, artefacts=art)
    assert r.completed and r.exit_code == 0
    entry = json.loads((art.dir / "manifest.json").read_text(encoding="utf-8"))["artefacts"][-1]
    assert entry["interrupted"] is None


def test_timeout_is_still_exact_with_polling(art):
    t0 = time.monotonic()
    r = run_tool("slow", [PY, "-c", "import time; time.sleep(30)"], timeout_s=1.5, artefacts=art)
    assert r.timed_out and r.exit_code is None
    assert 1.4 <= time.monotonic() - t0 < 15


def test_poll_interval_is_a_bounded_config_key():
    assert model_default(MaintenanceConfig, "tool_poll_seconds") > 0
    for bad in (0, -1, 3600):
        with pytest.raises(ValidationError):
            MaintenanceConfig(tool_poll_seconds=bad)


def test_poll_interval_is_read_from_the_config_file(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("maintenance:\n  tool_poll_seconds: 2.5\n", encoding="utf-8")
    assert tool_runner._configured_poll_seconds(str(cfg)) == 2.5
    cfg.write_text("maintenance: {}\n", encoding="utf-8")
    assert tool_runner._configured_poll_seconds(str(cfg)) == model_default(MaintenanceConfig, "tool_poll_seconds")
    cfg.write_text("maintenance:\n  tool_poll_seconds: 99999\n", encoding="utf-8")   # out of bounds
    assert tool_runner._configured_poll_seconds(str(cfg)) == model_default(MaintenanceConfig, "tool_poll_seconds")
    assert tool_runner._configured_poll_seconds(str(tmp_path / "missing.yaml")) == \
        model_default(MaintenanceConfig, "tool_poll_seconds")
