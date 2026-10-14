"""M1 1.4 / 1.5 — tool runner + run artefact store (real child processes)."""

import gzip
import json
import os
import sys
import time
from pathlib import Path

import pytest

from core.run_artefacts import RunArtefacts, purge_older_than, read_artefact_text
from core.tool_runner import run_tool, tail_lines

PY = sys.executable


@pytest.fixture
def art(tmp_path):
    return RunArtefacts(tmp_path / "run_logs", "run-1-cloud", day="2026-10-14")


def _manifest(art):
    return json.loads((art.dir / "manifest.json").read_text(encoding="utf-8"))


def test_exit_code_and_full_output_captured_to_files(art):
    code = "import sys; print('out-line'); print('err-line', file=sys.stderr); sys.exit(7)"
    r = run_tool("probe", [PY, "-c", code], timeout_s=60, artefacts=art)
    assert r.completed and r.exit_code == 7 and type(r.exit_code) is int
    assert "out-line" in r.stdout_text() and "err-line" in r.stderr_text()
    assert r.stdout_path.parent == art.dir
    entry = _manifest(art)["artefacts"][-1]
    assert entry["tool"] == "probe" and entry["exit_code"] == 7 and entry["timed_out"] is False


def test_large_output_is_streamed_not_truncated(art):
    code = "import sys; [sys.stdout.write('line %d' % i + chr(10)) for i in range(200000)]"
    r = run_tool("big", [PY, "-c", code], timeout_s=120, artefacts=art)
    assert r.exit_code == 0, r.stderr_text()
    text = r.stdout_text()
    assert text.count(chr(10)) == 200000 and "line 199999" in text
    assert tail_lines(text, 2) == "line 199998" + chr(10) + "line 199999"


def test_timeout_kills_whole_process_tree(art, tmp_path):
    marker = tmp_path / "child_alive.txt"
    child = f"import time; time.sleep(3); open(r'{marker}', 'w').write('x')"
    parent = ("import subprocess, sys, time; "
              f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(60)")
    t0 = time.monotonic()
    r = run_tool("slow", [PY, "-c", parent], timeout_s=1.5, artefacts=art)
    assert r.timed_out and r.exit_code is None and not r.completed
    assert time.monotonic() - t0 < 30
    time.sleep(4)
    assert not marker.exists(), "grandchild survived the timeout kill"
    assert "timed out" in r.describe_failure()


def test_spawn_error_is_explicit(art):
    r = run_tool("missing", [str(Path("no") / "such" / "tool.exe")], timeout_s=5, artefacts=art)
    assert r.spawn_error and r.exit_code is None and not r.timed_out
    assert "could not be started" in r.describe_failure()
    assert _manifest(art)["artefacts"][-1]["spawn_error"]


def test_artefact_paths_are_ordered_and_safe(art):
    a = art.new_path("rclone sync/../x", "log")
    b = art.new_path("robocopy", "log")
    assert a.parent == art.dir and ".." not in a.name and "/" not in a.name
    assert a.name < b.name


def test_finalize_compresses_and_reader_is_transparent(art):
    p = art.new_path("big", "log")
    p.write_text("é" * 5000, encoding="utf-8")
    small = art.new_path("small", "log")
    small.write_text("tiny", encoding="utf-8")
    art.finalize(compress_after_kb=1)
    assert not p.exists() and Path(str(p) + ".gz").exists()
    assert small.exists()
    assert read_artefact_text(p) == "é" * 5000
    assert _manifest(art)["notes"][-1]["text"] == "finalized"


def test_utf16_robocopy_unilog_round_trip(art):
    p = art.new_path("robocopy", "log")
    p.write_text("New File  C:\Données\été.txt\n", encoding="utf-16")
    assert "été.txt" in read_artefact_text(p, encoding="utf-16")


def test_free_space_below_minimum_marks_degraded(art):
    assert art.check_free_space(10**9) is False
    assert _manifest(art)["degraded"] is True


def test_purge_removes_only_old_day_folders_and_files(tmp_path):
    root = tmp_path / "run_logs"
    old_day, new_day, foreign = root / "2026-01-01", root / "2026-10-13", root / "keep-me"
    for d in (old_day, new_day, foreign):
        (d / "r").mkdir(parents=True)
    now = time.time()
    old = now - 91 * 86400
    for d in (old_day, foreign):
        os.utime(d, (old, old))
    assert purge_older_than(root, 90, now=now) == 1
    assert not old_day.exists() and new_day.exists() and foreign.exists()

    logs = tmp_path / "svc"
    logs.mkdir()
    f_old, f_new = logs / "agent_svc-1.log", logs / "agent_svc-2.log"
    f_old.write_text("x"); f_new.write_text("y")
    os.utime(f_old, (old, old))
    assert purge_older_than(logs, 90, glob="agent_svc-*.log", now=now) == 1
    assert not f_old.exists() and f_new.exists()


def test_for_config_uses_configured_root(tmp_path):
    from models.config import load_config, CONFIG_PATH

    cfg = load_config(CONFIG_PATH)
    art = RunArtefacts.for_config(cfg, "r-x")
    assert art.dir.is_relative_to(cfg.run_logs_dir)
    assert cfg.run_logs_dir == Path(cfg.paths.runtime_dir) / "run_logs"
