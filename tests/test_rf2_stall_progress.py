"""Tests for RF-2: S6 stall watchdog parses rclone JSON progress and ignores silent tools."""
from __future__ import annotations

import json
import sys
import time

from core.config_help import HELP
from core.process import resolve_binary
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool


def _py_child(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_rf2_1_identical_stats_lines_trigger_stall(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.2)
    art = RunArtefacts.open(tmp_path / "art")
    log = art.new_path("sync", "log")
    line = json.dumps({
        "level": "info",
        "msg": "stats",
        "stats": {"bytes": 32768, "transfers": 0, "checks": 1, "deletes": 0, "deletedDirs": 0,
                  "renames": 0, "listed": 1, "errors": 0, "serverSideCopies": 0, "serverSideMoves": 0},
    })
    code = (
        "import time, pathlib; "
        f"p = pathlib.Path({str(log)!r}); "
        "t0 = time.monotonic()\n"
        "while time.monotonic() - t0 < 10:\n"
        "    with p.open('a', encoding='utf-8') as f: f.write(" + repr(line + "\n") + ")\n"
        "    time.sleep(0.2)\n"
    )
    t0 = time.monotonic()
    res = run_tool("sync", _py_child(code), timeout_s=30, artefacts=art, log_path=log, stall_timeout_s=1.5)
    assert res.stalled is True
    assert time.monotonic() - t0 < 6.0


def test_rf2_2_growing_bytes_stats_does_not_stall(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.2)
    art = RunArtefacts.open(tmp_path / "art")
    log = art.new_path("sync", "log")
    code = (
        "import json, time, pathlib\n"
        f"p = pathlib.Path({str(log)!r})\n"
        "for i in range(1, 9):\n"
        "    with p.open('a', encoding='utf-8') as f:\n"
        "        f.write(json.dumps({'level':'info','stats':{'bytes': i*1000, 'transfers':0}}) + '\\n')\n"
        "    time.sleep(0.45)\n"
    )
    res = run_tool("sync", _py_child(code), timeout_s=30, artefacts=art, log_path=log, stall_timeout_s=1.5)
    assert res.stalled is False and res.exit_code == 0


def test_rf2_3_info_object_lines_count_as_progress(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.2)
    art = RunArtefacts.open(tmp_path / "art")
    log = art.new_path("sync", "log")
    code = (
        "import json, time, pathlib\n"
        f"p = pathlib.Path({str(log)!r})\n"
        "for i in range(8):\n"
        "    with p.open('a', encoding='utf-8') as f:\n"
        "        f.write(json.dumps({'level':'info','msg':'Copied (new)','object':f'f{i}.txt'}) + '\\n')\n"
        "    time.sleep(0.45)\n"
    )
    res = run_tool("sync", _py_child(code), timeout_s=30, artefacts=art, log_path=log, stall_timeout_s=1.5)
    assert res.stalled is False and res.exit_code == 0


def test_rf2_4_notice_error_lines_only_trigger_stall(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.2)
    art = RunArtefacts.open(tmp_path / "art")
    log = art.new_path("sync", "log")
    code = (
        "import json, time, pathlib; "
        f"p = pathlib.Path({str(log)!r}); "
        "t0 = time.monotonic()\n"
        "while time.monotonic() - t0 < 10:\n"
        "    with p.open('a', encoding='utf-8') as f:\n"
        "        f.write(json.dumps({'level':'notice','msg':'waiting','object':'x'}) + '\\n')\n"
        "    time.sleep(0.2)\n"
    )
    res = run_tool("sync", _py_child(code), timeout_s=30, artefacts=art, log_path=log, stall_timeout_s=1.5)
    assert res.stalled is True


def test_rf2_5_silent_no_log_path_with_default_stall_does_not_stall(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.2)
    monkeypatch.setattr("core.tool_runner._configured_stall_seconds", lambda *a, **k: 1.5)
    art = RunArtefacts.open(tmp_path / "art")
    res = run_tool("check", _py_child("import time; time.sleep(3.0)"), timeout_s=30, artefacts=art, log_path=None)
    assert res.stalled is False and res.exit_code == 0


def test_rf2_6_silent_no_log_path_with_explicit_stall_stalls(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.2)
    art = RunArtefacts.open(tmp_path / "art")
    res = run_tool(
        "check", _py_child("import time; time.sleep(6.0)"),
        timeout_s=30, artefacts=art, log_path=None, stall_timeout_s=1.5,
    )
    assert res.stalled is True


def test_rf2_7_partial_json_line_carried_across_writes(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.2)
    art = RunArtefacts.open(tmp_path / "art")
    log = art.new_path("sync", "log")
    code = (
        "import json, time, pathlib\n"
        f"p = pathlib.Path({str(log)!r})\n"
        "for i in range(1, 5):\n"
        "    s = json.dumps({'level':'info','stats':{'bytes': i*100}}) + '\\n'\n"
        "    half = len(s) // 2\n"
        "    with p.open('a', encoding='utf-8') as f: f.write(s[:half])\n"
        "    time.sleep(0.45)\n"
        "    with p.open('a', encoding='utf-8') as f: f.write(s[half:])\n"
        "    time.sleep(0.45)\n"
    )
    res = run_tool("sync", _py_child(code), timeout_s=30, artefacts=art, log_path=log, stall_timeout_s=1.5)
    assert res.stalled is False and res.exit_code == 0


def test_rf2_8_non_json_garbage_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.2)
    art = RunArtefacts.open(tmp_path / "art")
    log = art.new_path("sync", "log")
    code = (
        "import json, time, pathlib\n"
        f"p = pathlib.Path({str(log)!r})\n"
        "for i in range(1, 6):\n"
        "    with p.open('a', encoding='utf-8') as f:\n"
        "        f.write('not-json garbage\\n')\n"
        "        f.write(json.dumps({'level':'info','stats':{'bytes': i*50}}) + '\\n')\n"
        "    time.sleep(0.45)\n"
    )
    res = run_tool("sync", _py_child(code), timeout_s=30, artefacts=art, log_path=log, stall_timeout_s=1.5)
    assert res.stalled is False and res.exit_code == 0


def test_rf2_9_stall_timeout_zero_never_stalls(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.2)
    art = RunArtefacts.open(tmp_path / "art")
    log = art.new_path("sync", "log")
    res = run_tool(
        "sync", _py_child("import time; time.sleep(2.5)"),
        timeout_s=30, artefacts=art, log_path=log, stall_timeout_s=0,
    )
    assert res.stalled is False and res.exit_code == 0


def test_rf2_10_checks_counter_change_is_progress(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.2)
    art = RunArtefacts.open(tmp_path / "art")
    log = art.new_path("sync", "log")
    code = (
        "import json, time, pathlib\n"
        f"p = pathlib.Path({str(log)!r})\n"
        "for i in range(1, 8):\n"
        "    with p.open('a', encoding='utf-8') as f:\n"
        "        f.write(json.dumps({'level':'info','stats':{'bytes':0,'checks': i}}) + '\\n')\n"
        "    time.sleep(0.45)\n"
    )
    res = run_tool("sync", _py_child(code), timeout_s=30, artefacts=art, log_path=log, stall_timeout_s=1.5)
    assert res.stalled is False and res.exit_code == 0


def test_rf2_11_config_help_text():
    expected = (
        "Stops rclone sync and dry-run if its own progress counters do not change for this long "
        "(0 = off, 300-14400). Check, size and report tools are not stopped by this; they have their own time limits."
    )
    assert HELP["tools.stall_timeout_seconds"][1] == expected


def test_rf2_l_real_rclone_throttled_1k_stalls(tmp_path, monkeypatch):
    monkeypatch.setattr("core.tool_runner._configured_poll_seconds", lambda *a, **k: 0.5)
    rclone = resolve_binary("rclone") or "rclone"
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    # 50 MB at 1 KB/s will write identical/minimal progress or stall under 5s?
    # Note: 50 MB at 1 KiB/s writes 1 KiB/s, wait: bytes grows by 1024 every 1s if copying!
    # Wait: RF2-L in the plan says: "--bwlimit 1k --stats 1s --use-json-log --log-file, stall_timeout_s=5"
    # Let's run it as specified and check!
    (src / "big.bin").write_bytes(b"\0" * (5 * 1024 * 1024))
    art = RunArtefacts.open(tmp_path / "art")
    log = art.new_path("rclone-rf2l", "log")
    cmd = [rclone, "sync", str(src), str(dst), "--bwlimit", "1k", "--stats", "1s", "--use-json-log", "--log-file", str(log)]
    res = run_tool("rclone-rf2l", cmd, timeout_s=60, artefacts=art, log_path=log, stall_timeout_s=5)
    assert res.stalled is True
