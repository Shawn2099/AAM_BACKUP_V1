"""WAVE 24 - the Prefect SERVER restarts while a backup holds the `aam-backup` slot.

Found on the trial box 2026-10-22: a cloud run took the slot at 21:48, the services were restarted
at 21:49-21:50, and from then on every backup timed out waiting ("Attempt to acquire concurrency
slots timed out") - the slot count lives in the database, the lease that expires it lived only in
the server's memory (Prefect default `lease_storage.memory`), so the restart forgot the holder and
nothing ever freed the slot.

The server is started exactly as the AamPrefectServer service starts it: `prefect server start`
with the PREFECT_SERVER_* environment taken from the installer (`deploy/06_install_services.ps1`),
so this test fails if the installer stops carrying the lease storage setting.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import textwrap
import time
import urllib.request
import json
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.timeout(600)

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "deploy" / "06_install_services.ps1"
LEASE_SECONDS = 60               # Prefect's minimum lease; production uses 300
FREE_WITHIN_SECONDS = 150        # lease + the repossessor's 15 s sweep, with slack
LIMIT = "aam-backup"


def installer_server_env() -> dict[str, str]:
    """The PREFECT_SERVER_* settings the installer gives the AamPrefectServer service."""
    block = INSTALLER.read_text(encoding="utf-8").split("$SVC_SERVER AppEnvironmentExtra", 1)[1]
    block = block.split("sc.exe failure", 1)[0]
    return dict(m.groups() for m in re.finditer(r'"(PREFECT_SERVER_[A-Z_]+)=([^"$]+)"', block))


def _api(url: str, path: str, body: dict | None = None, method: str | None = None):
    req = urllib.request.Request(
        url + path, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _start_server(home: Path, port: int, log_name: str) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if k not in ("PREFECT_TEST_MODE", "PREFECT_API_URL")}
    env.update(installer_server_env())
    env.update(PREFECT_HOME=str(home), PREFECT_SERVER_ANALYTICS_ENABLED="false")
    log = (home / log_name).open("wb")
    srv = subprocess.Popen([sys.executable, "-m", "prefect", "server", "start", "--port", str(port),
                            "--no-ui"], env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}/api"
    for _ in range(120):
        try:
            urllib.request.urlopen(f"{url}/health", timeout=2)
            return srv
        except Exception:
            time.sleep(1)
    raise AssertionError("prefect server did not start: " + (home / log_name).read_text(
        errors="replace")[-1500:])


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        victims = psutil.Process(proc.pid).children(recursive=True) + [psutil.Process(proc.pid)]
    except psutil.Error:
        return
    for p in victims:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=30)


HOLDER = textwrap.dedent("""
    import sys, time
    from prefect.concurrency.sync import concurrency
    with concurrency("%s", occupy=1, timeout_seconds=30, lease_duration=%d):
        print("HELD", flush=True)
        time.sleep(600)
""" % (LIMIT, LEASE_SECONDS))


def _active_slots(url: str) -> int:
    return _api(url, f"/v2/concurrency_limits/{LIMIT}")["active_slots"]


def test_slot_of_a_run_killed_with_the_server_is_freed_after_the_restart(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/api"
    home = tmp_path / "prefect_home"
    home.mkdir()

    server = _start_server(home, port, "server1.log")
    holder = None
    try:
        _api(url, "/v2/concurrency_limits/", {"name": LIMIT, "limit": 1, "active": True})
        env = {k: v for k, v in os.environ.items() if k != "PREFECT_TEST_MODE"}
        env.update(PREFECT_HOME=str(home), PREFECT_API_URL=url)
        holder = subprocess.Popen([sys.executable, "-c", HOLDER], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        first = holder.stdout.readline().strip()
        assert first == "HELD", "the holder did not get the slot: " + first + (home / "server1.log").read_text(errors="replace")[-1500:]
        assert _active_slots(url) == 1

        # the service restart of 2026-10-20 21:49: the holder and the server die together
        _kill_tree(holder)
        _kill_tree(server)

        server = _start_server(home, port, "server2.log")
        deadline = time.time() + FREE_WITHIN_SECONDS
        while time.time() < deadline and _active_slots(url) != 0:
            time.sleep(3)
        assert _active_slots(url) == 0, (
            f"the slot of a dead holder is still taken {FREE_WITHIN_SECONDS}s after the server "
            "restarted: every later backup would wait for it and time out")
    finally:
        if holder is not None:
            _kill_tree(holder)
        _kill_tree(server)


# ---------------------------------------------------------------------------------------------
# The Prefect server restarts while a backup is in the middle of a transfer (real tools, real flow)
# ---------------------------------------------------------------------------------------------
from audit_lab import labconf as L                                          # noqa: E402
from audit_lab.test_w6_flow import _db_rows, rig  # noqa: E402,F401
from audit_lab.test_w22_hardkill import (                                    # noqa: E402,F401
    _drop_local_backend_partials, _spawn_mid_run, _tree, hardkill_rig)

_SERVER: dict = {}


@pytest.fixture()
def real_prefect_server(tmp_path_factory):
    """Replaces the W22 fixture of the same name: the production server environment, restartable."""
    from prefect.settings import PREFECT_API_URL, temporary_settings

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    home = tmp_path_factory.mktemp("prefect_home_w24")
    _SERVER.update(home=home, port=port, proc=_start_server(home, port, "server1.log"), n=1)
    url = f"http://127.0.0.1:{port}/api"
    try:
        with temporary_settings({PREFECT_API_URL: url}):
            yield url
    finally:
        _kill_tree(_SERVER["proc"])


def _restart_server(outage_seconds: float) -> None:
    _kill_tree(_SERVER["proc"])
    time.sleep(outage_seconds)
    _SERVER["n"] += 1
    _SERVER["proc"] = _start_server(_SERVER["home"], _SERVER["port"], f"server{_SERVER['n']}.log")


@pytest.mark.parametrize("leg", ["cloud", "lan"])
def test_server_restart_mid_transfer(hardkill_rig, leg):
    rig = hardkill_rig
    child, proc, lock, log = _spawn_mid_run(rig, leg, need_tool=True)
    _restart_server(outage_seconds=30)
    try:
        child.wait(timeout=900)
    except subprocess.TimeoutExpired:
        _kill_tree(child)
        raise
    statuses = [r["status"] for r in _db_rows(rig["cfg_path"])[0] if r["mode"] == leg]
    dest = _tree(rig["nas"]) if leg == "lan" else _tree(
        rig["buckets"] / "lab-bucket" / rig["src"].name)
    src = _tree(rig["src"])
    slots = _active_slots(f"http://127.0.0.1:{_SERVER['port']}/api")
    L.record(f"W24.server_restart_mid_{leg}", f"Prefect server down 30 s in the middle of a {leg} sync",
             f"child exit={child.returncode} rows={statuses} files src={len(src)} dest={len(dest)} "
             f"lock left={lock.exists()} slots={slots}",
             "OK" if dest == src and child.returncode == 0 else "ISSUE")
    assert child.returncode == 0, log.read_text(errors="replace")[-3000:]
    assert statuses == [f"{leg.upper()}_COMPLETE"], statuses
    assert dest == src, "the copy must be complete after the server came back"
    assert not lock.exists() and slots == 0
