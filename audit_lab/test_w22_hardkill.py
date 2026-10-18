"""WAVE 22 - plan I4: the process tree is hard-killed mid-run (reboot / power cut / SCM kill),
with the REAL robocopy and rclone, a REAL shared Prefect server and the production flows.

For every interrupted run: the dead run is recorded ABANDONED with exactly one alert, the backup
slot is freed by the server-side lease (the next run waits for it, then starts), the next run
completes, the destination equals the source, nothing is BLOCKED and no approval is needed.
Scenarios: mid cloud sync, mid LAN mirror, mid weekly audit, mid FY rollover (final backups).
The audit is slowed by a lab seam (it is read-only); the syncs by rclone --bwlimit and robocopy /IPG.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import psutil
import pytest
import yaml

from audit_lab import labconf as L
from audit_lab.test_w6_flow import _db_rows, rig  # noqa: F401 - the production-wired rig fixture

pytestmark = pytest.mark.timeout(1800)

CHILD = Path(__file__).resolve().parent / "w22_child.py"
LEASE_WAIT_SECONDS = 420          # > the 300 s slot lease; production waits 3600
BIG_FILES = 16
BIG_FILE_BYTES = 4 * 1024 * 1024


def _tree(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_size for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != ".AAM_TARGET_MOUNTED"}


def _edit_config(path: Path, edit) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _tool_children(proc: psutil.Process) -> list[psutil.Process]:
    try:
        return [c for c in proc.children(recursive=True)
                if c.name().lower() in ("robocopy.exe", "rclone.exe")]
    except psutil.Error:
        return []


def _kill_tree(proc: psutil.Process) -> list[int]:
    victims = proc.children(recursive=True) + [proc]
    for p in victims:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=30)
    return [p.pid for p in victims]


@pytest.fixture()
def real_prefect_server(tmp_path_factory):
    """A real `prefect server start` (as the AamPrefectServer service runs it): the ephemeral
    test harness does not run the repossessor that frees the slot of a dead holder."""
    import socket
    import urllib.request

    from prefect.settings import temporary_settings, PREFECT_API_URL

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    home = tmp_path_factory.mktemp("prefect_home")
    env = {k: v for k, v in os.environ.items() if k != "PREFECT_TEST_MODE"}
    env.update(PREFECT_HOME=str(home), PREFECT_SERVER_ANALYTICS_ENABLED="false")
    env.pop("PREFECT_API_URL", None)
    log = (home / "server.log").open("wb")
    srv = subprocess.Popen([sys.executable, "-m", "prefect", "server", "start", "--port", str(port),
                            "--no-ui"], env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}/api"
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f"{url}/health", timeout=2)
                break
            except Exception:
                time.sleep(1)
        else:
            raise AssertionError("prefect server did not start: " + (home / "server.log").read_text(
                errors="replace")[-1500:])
        with temporary_settings({PREFECT_API_URL: url}):
            yield url
    finally:
        victims = psutil.Process(srv.pid).children(recursive=True) + [psutil.Process(srv.pid)]
        for p in victims:
            try:
                p.kill()
            except psutil.Error:
                pass
        log.close()


@pytest.fixture()
def hardkill_rig(rig, real_prefect_server):  # noqa: F811
    from launch import _ensure_concurrency_limit
    _ensure_concurrency_limit()          # production startup code, on the REAL server
    for i in range(BIG_FILES):
        L.write_file(rig["src"] / "Big" / f"big_{i:02d}.bin", os.urandom(BIG_FILE_BYTES))
    cfg_path = rig["cfg_path"]
    slow = cfg_path.with_name("config_slow.yaml")
    slow.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
    _edit_config(slow, lambda d: d["cloud"].update(bandwidth_limit="1M"))
    _edit_config(cfg_path, lambda d: d["maintenance"].update(
        concurrency_wait_seconds=LEASE_WAIT_SECONDS))
    _edit_config(slow, lambda d: d["maintenance"].update(
        concurrency_wait_seconds=LEASE_WAIT_SECONDS))
    return {**rig, "slow_cfg": slow}


def _drop_local_backend_partials(rig) -> None:
    """Lab artefact: rclone's LOCAL backend stages an upload as `<name>.<hex>.partial` and a kill
    leaves it behind. A real GCS upload is atomic (no half object is ever visible), so the stand-in
    must not show guard 2 stray files that GCS can never have."""
    for stray in (rig["buckets"] / "lab-bucket").rglob("*.partial"):
        stray.unlink()


def _spawn_and_kill(rig, entry: str, *, need_tool: bool) -> dict:
    """Run the production flow in a child process, kill its whole tree while it holds the slot."""
    from prefect.settings import PREFECT_API_URL

    env = {k: v for k, v in os.environ.items() if k != "PREFECT_TEST_MODE"}
    env.update(PREFECT_API_URL=PREFECT_API_URL.value(), PYTHONIOENCODING="utf-8")
    spec = {"cfg": str(rig["slow_cfg"]), "rc": rig["rc"], "entry": entry, "ipg": 100,
            "audit_sleep": 600}
    from models.config import load_config
    cfg = load_config(str(rig["cfg_path"]))
    lock = cfg.paths.backup_lock_path
    log = rig["sandbox"] / f"child_{entry}.log"
    with log.open("wb") as out:
        child = subprocess.Popen([sys.executable, str(CHILD), json.dumps(spec)], env=env,
                                 cwd=str(rig["buckets"]), stdout=out, stderr=subprocess.STDOUT)
    proc = psutil.Process(child.pid)
    deadline = time.monotonic() + 300
    seen_since = None
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise AssertionError(f"child ended before it could be killed (exit {child.returncode}): "
                                 f"{log.read_text(errors='replace')[-2000:]}")
        ready = lock.exists() and (bool(_tool_children(proc)) or not need_tool)
        if ready:
            seen_since = seen_since or time.monotonic()
            if time.monotonic() - seen_since >= 4:      # really in the middle of the work
                break
        else:
            seen_since = None
        time.sleep(0.5)
    else:
        raise AssertionError("the child never reached the middle of its run: "
                             f"{log.read_text(errors='replace')[-2000:]}")
    killed = _kill_tree(proc)
    return {"killed_pids": killed, "lock_left": lock.exists(), "lock": lock,
            "survivors": [p for p in killed if psutil.pid_exists(p)]}


def _statuses(cfg_path: Path, mode: str) -> list[str]:
    return [r["status"] for r in _db_rows(cfg_path)[0] if r["mode"] == mode]


def _no_blocks(cfg_path: Path) -> tuple[list[str], int]:
    from core.manifest import ManifestDB
    from models.config import load_config
    cfg = load_config(str(cfg_path))
    db = ManifestDB(cfg.paths.database_path)
    try:
        blocked = [r["status"] for r in db.get_recent_runs(20) if "BLOCKED" in r["status"]]
        return blocked, len(db.open_approvals())
    finally:
        db.close()


@pytest.mark.parametrize("leg", ["cloud", "lan"])
def test_w22_kill_mid_sync_then_next_run_completes(hardkill_rig, leg):
    rig = hardkill_rig
    flow = rig["flow"]
    sweep_alerts: list = []
    outcome = _spawn_and_kill(rig, leg, need_tool=True)
    running = _statuses(rig["cfg_path"], leg)
    assert running == [f"{leg.upper()}_RUNNING"], running
    assert outcome["lock_left"], "a killed run leaves its (dead-PID) lock file behind"
    assert not outcome["survivors"]

    if leg == "cloud":
        _drop_local_backend_partials(rig)
    t0 = time.monotonic()
    with patch("core.backup_repository.notify",
               side_effect=lambda *a, **k: sweep_alerts.append(a[1]) or True):
        flow.backup(config_path=str(rig["cfg_path"]), mode=leg)
    waited = time.monotonic() - t0

    statuses = _statuses(rig["cfg_path"], leg)
    src = _tree(rig["src"])
    dest = _tree(rig["nas"]) if leg == "lan" else _tree(
        rig["buckets"] / "lab-bucket" / rig["src"].name)
    blocked, approvals = _no_blocks(rig["cfg_path"])
    L.record(f"W22.kill_mid_{leg}", f"hard-kill mid {leg} sync, then the next run",
             f"rows={statuses} | waited {waited:.0f}s for the slot lease | sweep alerts="
             f"{[(a.key, a.fault_code) for a in sweep_alerts]} | files src={len(src)} dest={len(dest)} "
             f"| blocked={blocked} approvals={approvals} | lock after={outcome['lock'].exists()}",
             "OK" if dest == src else "ISSUE")
    assert f"{leg.upper()}_ABANDONED" in statuses
    assert statuses[0].endswith("_COMPLETE") or statuses[0].endswith("_NO_CHANGES_COMPLETE"), statuses
    assert len(sweep_alerts) == 1 and sweep_alerts[0].fault_code == "AAM-SYS-02"
    assert dest == src, "after the next run the destination must equal the source"
    assert not blocked and approvals == 0
    assert not outcome["lock"].exists(), "the finished run removes the lock"
    assert waited < LEASE_WAIT_SECONDS, "the slot must come back within the wait (lease expiry)"


def test_w22_kill_mid_audit_leaves_no_false_verdict_and_the_next_audit_runs(hardkill_rig):
    rig = hardkill_rig
    flow = rig["flow"]
    from core.manifest import ManifestDB
    from models.config import load_config

    flow.backup(config_path=str(rig["cfg_path"]), mode="all")          # something to audit
    cfg = load_config(str(rig["cfg_path"]))
    outcome = _spawn_and_kill(rig, "audit", need_tool=False)
    db = ManifestDB(cfg.paths.database_path)
    try:
        before = (db.latest_audit("cloud"), db.latest_audit("lan"))
    finally:
        db.close()
    assert before == (None, None), "a killed audit must leave no verdict behind"

    t0 = time.monotonic()
    flow.integrity_audit_flow(config_path=str(rig["cfg_path"]))
    waited = time.monotonic() - t0
    db = ManifestDB(cfg.paths.database_path)
    try:
        after = (db.latest_audit("cloud"), db.latest_audit("lan"))
    finally:
        db.close()
    blocked, approvals = _no_blocks(rig["cfg_path"])
    L.record("W22.kill_mid_audit", "hard-kill mid weekly audit, then the next audit",
             f"no verdict after the kill | waited {waited:.0f}s | next audit="
             f"{[a and a['status'] for a in after]} | blocked={blocked} approvals={approvals}",
             "OK" if all(a and a["status"] == "VERIFIED" for a in after) else "ISSUE")
    assert all(a and a["status"] == "VERIFIED" for a in after)
    assert not blocked and approvals == 0
    assert not outcome["lock"].exists()


def test_w22_kill_mid_rollover_then_the_next_morning_completes(hardkill_rig, monkeypatch):
    rig = hardkill_rig
    flow = rig["flow"]
    from core import rollover_journal as rj
    from core.manifest import ManifestDB
    from models.config import load_config

    sweep_alerts: list = []
    outcome = _spawn_and_kill(rig, "rollover", need_tool=True)
    statuses = _statuses(rig["cfg_path"], "rollover")
    assert statuses == ["ROLLOVER_RUNNING"], statuses

    _drop_local_backend_partials(rig)
    monkeypatch.setattr(flow, "get_fy_prefix", lambda *a, **k: "FY27-28")
    t0 = time.monotonic()
    with patch("core.backup_repository.notify",
               side_effect=lambda *a, **k: sweep_alerts.append(a[1]) or True):
        result = flow.rollover_check_flow(config_path=str(rig["cfg_path"]))
    waited = time.monotonic() - t0
    after = load_config(str(rig["cfg_path"]))
    db = ManifestDB(after.paths.database_path)
    try:
        journal = db.rollover_get("FY26-26", "FY27-28")
        steps = {k: v["state"] for k, v in journal["steps"].items()}
    finally:
        db.close()
    blocked, approvals = _no_blocks(rig["cfg_path"])
    keys = sorted(a.key for a in sweep_alerts)
    L.record("W22.kill_mid_rollover", "hard-kill mid FY rollover (final backups), then the next morning",
             f"result={result!r} | waited {waited:.0f}s | lan_destination={after.paths.lan_destination!r} "
             f"| steps={steps} | sweep alerts={keys} | blocked={blocked} approvals={approvals}",
             "OK" if result == "ROLLOVER_COMPLETED" and after.paths.lan_destination.endswith("FY27-28")
             else "ISSUE")
    assert result == "ROLLOVER_COMPLETED"
    assert after.paths.lan_destination.endswith("FY27-28")
    assert steps[rj.CONFIG_SWITCH] == rj.DONE and steps[rj.FINAL_BACKUPS] == rj.DONE
    assert "rollover:abandoned" in keys
    assert not blocked and approvals == 0
    assert not outcome["lock"].exists()
