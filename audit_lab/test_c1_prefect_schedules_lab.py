"""C1 probe (TESTMATRIX_C, K-C1-5): does Prefect really drop a deployment's cron when it is re-served with
`schedules=[]`, and does a manual run still start?  Real local Prefect server, clean PREFECT_HOME, dummy flow.

Re-run on every Prefect upgrade (DC4). Nothing here touches the real config, services, ~/.prefect or the box.
The server and every Prefect client run in subprocesses with their own environment; the test process itself
never imports Prefect state from the user's home.

    PYTHONPATH=. <python> -m pytest audit_lab/test_c1_prefect_schedules_lab.py -q -s --timeout=900 -p no:cacheprovider -o addopts=""
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.timeout(900)

NAME = "backup-lan-c1probe"

# One step of the probe per call; the server keeps the state between steps (tests run in file order).
CHILD = r'''
import asyncio, json, os, subprocess, sys, time, warnings
from prefect import flow
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import FlowRunFilter, FlowRunFilterDeploymentId
from prefect.deployments import arun_deployment
from prefect.schedules import Cron

REPO, HERE = sys.argv[2], os.path.dirname(os.path.abspath(__file__))
NAME = "backup-lan-c1probe"
FULL = "aam-backup/" + NAME


@flow(name="aam-backup")
def probe(mode: str = "lan"):
    return mode


def build(sched):
    return probe.ato_deployment(NAME, parameters={"mode": "lan"}, schedules=sched)


async def future_auto_runs(client, dep_id):
    runs = await client.read_flow_runs(flow_run_filter=FlowRunFilter(
        deployment_id=FlowRunFilterDeploymentId(any_=[dep_id])))
    now = time.time()
    fut = [r for r in runs if r.auto_scheduled and r.state and r.state.type.value == "SCHEDULED"
           and r.expected_start_time and r.expected_start_time.timestamp() > now]
    return fut, runs


async def wait_future(client, dep_id, tries=15):
    fut = []
    for _ in range(tries):
        fut, _a = await future_auto_runs(client, dep_id)
        if fut:
            break
        await asyncio.sleep(2)
    return fut


async def step(name):
    if name == "P1":
        dep = await build([Cron("* * * * *", "Asia/Kolkata")])
        await dep.aapply()
        async with get_client() as c:
            d = await c.read_deployment_by_name(FULL)
            fut = await wait_future(c, d.id)
            return len(d.schedules) == 1 and bool(fut), f"schedules={len(d.schedules)}, future auto runs={len(fut)}"
    if name == "P2":
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            dep = await build([])
            await dep.aapply()          # exactly the runner.py call: no arguments
        preserve = [x for x in w if "Preserving existing schedules" in str(x.message)]
        async with get_client() as c:
            d = await c.read_deployment_by_name(FULL)
            return (d.schedules == [] and not preserve,
                    f"schedules={len(d.schedules)}, preserve-warnings={len(preserve)}, "
                    f"warnings={[str(x.message)[:70] for x in w]}")
    if name == "P3":
        async with get_client() as c:
            d = await c.read_deployment_by_name(FULL)
            f1, _a = await future_auto_runs(c, d.id)
            await asyncio.sleep(12)          # >= 5 scheduler loops of 2 s
            f2, allr = await future_auto_runs(c, d.id)
            return (not f1 and not f2,
                    f"future auto runs now={len(f1)}, after 12 s={len(f2)}, total runs={len(allr)}")
    if name == "P4":
        run = await arun_deployment(FULL, parameters={"mode": "lan"}, timeout=0)
        runner_py = os.path.join(HERE, "runner_child.py")
        proc = subprocess.Popen([sys.executable, runner_py], env=os.environ.copy(),
                                stdout=open(os.path.join(HERE, "runner.log"), "w"), stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        state = None
        try:
            async with get_client() as c:
                for _ in range(30):
                    r = await c.read_flow_run(run.id)
                    state = r.state.type.value
                    if state in ("COMPLETED", "FAILED", "CRASHED"):
                        break
                    await asyncio.sleep(2)
        finally:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        return run.auto_scheduled is False and state == "COMPLETED", \
            f"auto_scheduled={run.auto_scheduled}, final state={state}"
    if name == "P5":
        dep = await build([Cron("* * * * *", "Asia/Kolkata")])
        await dep.aapply()
        async with get_client() as c:
            d = await c.read_deployment_by_name(FULL)
            fut = await wait_future(c, d.id)
            return len(d.schedules) == 1 and bool(fut), f"schedules={len(d.schedules)}, future auto runs={len(fut)}"
    if name == "P6":
        dep = await build([])
        await dep.aapply()
        sys.path.insert(0, REPO)
        os.chdir(REPO)
        import launch
        assert os.path.realpath(launch.__file__).startswith(os.path.realpath(REPO)), launch.__file__
        out = await asyncio.to_thread(launch._reconcile_disabled_legs, None, {NAME: True}, set())
        async with get_client() as c:
            d = await c.read_deployment_by_name(FULL)
            return (out.get(NAME) == "active" and not d.paused and d.schedules == [],
                    f"result={out}, paused={d.paused}, schedules={len(d.schedules)}")
    raise SystemExit("unknown step " + name)


if __name__ == "__main__":
    ok, msg = asyncio.run(step(sys.argv[1]))
    print("RESULT " + json.dumps({"ok": bool(ok), "msg": msg}))
'''

RUNNER_CHILD = '''
from prefect import flow, serve


@flow(name="aam-backup")
def probe(mode: str = "lan"):
    return mode


if __name__ == "__main__":
    serve(probe.to_deployment("backup-lan-c1probe", parameters={"mode": "lan"}, schedules=[]))
'''


@pytest.fixture(scope="module")
def prefect_probe(tmp_path_factory):
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="c1probe_"))
    home = base / "prefect_home"
    home.mkdir()
    assert Path(home).resolve() != (Path.home() / ".prefect").resolve()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PREFECT_")}
    api = f"http://127.0.0.1:{port}/api"
    env.update(PREFECT_HOME=str(home), PREFECT_API_URL=api, PYTHONIOENCODING="utf-8",
               PREFECT_SERVER_SERVICES_SCHEDULER_LOOP_SECONDS="2")
    (base / "child.py").write_text(CHILD, encoding="utf-8")
    (base / "runner_child.py").write_text(RUNNER_CHILD, encoding="utf-8")
    srv = subprocess.Popen([sys.executable, "-m", "prefect", "server", "start", "--host", "127.0.0.1",
                            "--port", str(port)], env=env, stdout=open(base / "server.log", "w"),
                           stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        t0 = time.time()
        while time.time() - t0 < 120:
            try:
                urllib.request.urlopen(api + "/health", timeout=2)
                break
            except Exception:
                time.sleep(2)
        else:
            pytest.skip("local Prefect server did not become healthy within 120 s (see " + str(base / "server.log") + ")")
        yield {"env": env, "base": base}
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(srv.pid)], capture_output=True)


def _step(probe, name: str) -> dict:
    p = subprocess.run([sys.executable, str(probe["base"] / "child.py"), name, str(REPO)], env=probe["env"],
                       capture_output=True, text=True, timeout=300)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, f"{name}: no result\nSTDOUT:{p.stdout[-1500:]}\nSTDERR:{p.stderr[-2500:]}"
    res = json.loads(lines[-1][7:])
    print(f"[C1-PROBE] {name} {'PASS' if res['ok'] else 'FAIL'} {res['msg']}")
    return res


def test_p1_scheduled_deployment_gets_runs(prefect_probe):
    r = _step(prefect_probe, "P1")
    assert r["ok"], r["msg"]


def test_p2_reserve_with_empty_list_removes_the_cron(prefect_probe):
    r = _step(prefect_probe, "P2")
    assert r["ok"], r["msg"]


def test_p3_no_scheduled_runs_after_reserve(prefect_probe):
    r = _step(prefect_probe, "P3")
    assert r["ok"], r["msg"]


def test_p4_manual_run_still_starts(prefect_probe):
    r = _step(prefect_probe, "P4")
    assert r["ok"], r["msg"]


def test_p5_back_to_scheduled_restores_the_cron(prefect_probe):
    r = _step(prefect_probe, "P5")
    assert r["ok"], r["msg"]


def test_p6_reconcile_keeps_the_manual_deployment_resumed(prefect_probe):
    r = _step(prefect_probe, "P6")
    assert r["ok"], r["msg"]
