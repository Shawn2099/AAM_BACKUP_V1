"""WAVE 8 — Prefect orchestration: exactly how production starts a backup.

Waves 0-6 ran the pipelines and `flow.backup()` in-process (real Prefect tasks,
states, retries and task runs inside `prefect_test_harness()`). The layer ABOVE
that — the one the Prefect scheduler, the dashboard's "Run Backup" button and
the Prefect UI's "Quick run" really talk to — is the DEPLOYMENT layer, and only
WAVE 7 exercised it, on a box where all three services are installed and
running. This wave closes that gap on the lab rig (no services required).

Everything here runs against a REAL ephemeral Prefect API server started by
`prefect_test_harness()`, with the production "aam-backup" global concurrency
limit created by `launch._ensure_concurrency_limit()` — the same startup code
the deployed agent runs:

  W8.1  serve.deployments()   -> the 6 production deployments are really
                                 registered server-side with config cron + tz
  W8.2  arun_deployment("aam-backup/backup-lan")  (ui.py's exact call) creates a
         run, and the dashboard's duplicate-click guard then REFUSES a second
         trigger (400 already_running) instead of queueing a second backup
  W8.3  the same trigger endpoint with the Prefect API UNREACHABLE fails CLOSED
         (503) — the H3 tri-state — instead of silently starting a duplicate
  W8.4  launch._cancel_orphaned_runs() reaps the orphaned runs a crashed agent
         leaves behind, and leaves a RUNNING backup alone while its lock is live
  W8.5  two simultaneous backup runs (cron + operator click) serialise on the
         real global concurrency limit; the robocopy mirrors never interleave
  W8.6  weekly-report / monthly-report run as real flow runs and CONTAIN an
         SMTP outage (flow COMPLETED, no exception)
  W8.7  backup(mode="cloud") / backup(mode="lan") record only their own leg —
         the deployment parameters' real effect on run_history

No production module is edited. Lab seams are the same ones Waves 4-6 use: an
rclone LOCAL backend (config section [aam_gcs]), the /ZB flag dropped (this
account has no SeBackupPrivilege — proven in C1), and alert capture.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(1200)


def write_lab_config(base: Path, cfg) -> Path:
    """Serialise the validated AppConfig to a real config.yaml (the production
    loader reads this file, exactly as in production)."""
    import yaml

    p = base / "config.yaml"
    yaml.safe_dump(json.loads(cfg.model_dump_json()), p.open("w", encoding="utf-8"),
                   sort_keys=False, allow_unicode=True)
    return p


@pytest.fixture()
def pf_rig(sandbox, monkeypatch):
    """Full production wiring + a REAL Prefect API server, for lab paths only.

    Same construction as the WAVE 6 rig: real config.yaml on disk, real flow
    runtime, local rclone backend, /ZB stripped, alerts captured — plus the
    production concurrency limit created by launch._ensure_concurrency_limit().
    """
    import core.lan_sync as ls
    from core.time_utils import get_fy_prefix

    src = sandbox / "source" / "FY26-26"
    for i in range(800):
        L.write_file(src / "Clients" / f"f_{i:04d}.txt", f"LEDGER {i}\n".encode() * 40)
    nas = L.make_canary(sandbox / "nas" / "FY26-26")
    L.write_file(sandbox / "lab-key.json", b'{"type": "service_account"}')

    cfg = L.build_config(sandbox, source=src, lan_dest=str(nas), runtime=sandbox / "runtime",
                         cloud={"bandwidth_limit": "20M"},
                         # a queued run must WAIT for the slot like production
                         # (config default is 3600s); 30s aborts under load
                         maintenance={"concurrency_wait_seconds": 300})

    # v1.1.3: the cloud leg syncs to _effective_fy_prefix(config) — the source
    # leaf when it is a valid FY dir — not the calendar prefix. Create the
    # bucket dir for the prefix the production code will actually use.
    from flow import _effective_fy_prefix

    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    (bucket_root / "lab-bucket" / _effective_fy_prefix(cfg)).mkdir(parents=True, exist_ok=True)

    cfg_path = write_lab_config(sandbox, cfg)

    rc = L.write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc)

    import flow

    orig_build = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(orig_build(s, d, c))
    alerts: list = []
    monkeypatch.setattr(flow, "notify", lambda *a, **k: alerts.append(a) or True)
    monkeypatch.setattr(flow, "temp_rclone_config",
                        lambda *a, **k: contextlib.nullcontext(rc))
    monkeypatch.chdir(bucket_root)

    # use the REAL concurrency slot (production behaviour), not the test bypass
    monkeypatch.delenv("PREFECT_TEST_MODE", raising=False)
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness():
        from launch import _ensure_concurrency_limit

        _ensure_concurrency_limit()          # production startup code, limit=1
        yield {
            "cfg": cfg, "cfg_path": cfg_path, "sandbox": sandbox, "src": src, "nas": nas,
            "buckets": bucket_root, "alerts": alerts, "rc": rc, "flow": flow,
            "fy": get_fy_prefix(),
        }
    ls.build_robocopy_command = orig_build
    for (mod, attr), o in saved.items():
        setattr(mod, attr, o)


def _db_rows(cfg_path: Path):
    from core.manifest import ManifestDB

    from models.config import load_config
    c = load_config(str(cfg_path))
    m = ManifestDB(c.paths.database_path)
    rows = m.get_recent_runs(10)
    lan_rows = m.file_count("lan_status")
    cloud_rows = m.file_count("cloud_status")
    m.close()
    return rows, lan_rows, cloud_rows


def _dead_port() -> int:
    """A TCP port that is bound-and-closed: connect() fails immediately."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _import_ui(monkeypatch):
    """Import the dashboard app with CWD at the repo root.

    ui.py mounts StaticFiles(directory="static") / Jinja2Templates at import
    time; the rig has already chdir'd into the lab sandbox, so the app can only
    be imported from the repository root. Lab seam: CWD only.
    """
    repo = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(repo)
    import ui

    return ui


# ── W8.1 — the production deployments register on a real server ───────────────
def test_w8_production_deployments_registered(pf_rig, monkeypatch):
    """serve.deployments() really registers all 6 deployments with cron + tz."""
    import serve
    from models.config import load_config

    # serve._deployments() resolves CONFIG_PATH relative to CWD, and the rig has
    # chdir'd into the lab sandbox -> run the registration from the repo root.
    repo = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(repo)

    real_cfg = load_config(str(repo / "config.yaml"))   # the config serve.py reads
    deps = serve.deployments()

    async def _apply():
        for d in deps:
            await d.aapply()

    asyncio.run(_apply())

    from prefect.client.orchestration import get_client

    async def _read():
        async with get_client() as client:
            registered = []
            for d in await client.read_deployments():
                scheds = [(s.schedule.cron, s.schedule.timezone) for s in (d.schedules or [])]
                registered.append((d.name, d.paused, scheds, sorted(d.tags or [])))
            return registered

    registered = asyncio.run(_read())
    got = {name: (paused, scheds, tags) for name, paused, scheds, tags in registered}

    expected_sched = {
        "backup-cloud": real_cfg.schedule.cloud_cron,
        "backup-lan": real_cfg.schedule.lan_cron,
        "weekly-report": real_cfg.schedule.weekly_cron,
        "monthly-report": real_cfg.schedule.monthly_cron,
        "rollover-check": real_cfg.schedule.rollover_cron,
        "integrity-audit": real_cfg.schedule.audit_cron,
    }
    problems = []
    for name, cron in expected_sched.items():
        if name not in got:
            problems.append(f"{name} NOT registered")
            continue
        paused, scheds, tags = got[name]
        if scheds != [(cron, real_cfg.schedule.timezone)]:
            problems.append(f"{name} schedule={scheds} config={cron}/{real_cfg.schedule.timezone}")
        if paused:
            problems.append(f"{name} registered PAUSED")

    L.record("W8.deployments",
             "the production deployment set registers on a real Prefect server with the "
             "config's cron + timezone",
             f"serve.deployments() -> {[d.name for d in deps]} | registered on server: "
             f"{[(n, s) for n, _p, s, _t in registered]} | problems={problems}",
             "OK — all 6 deployments registered, schedules match config.yaml"
             if not problems else f"ISSUE — {problems}")
    print(f"[W8.1] registered={sorted(got)} problems={problems}")
    assert not problems, problems


# ── W8.2 — the dashboard's trigger call, and its duplicate-click guard ────────
def test_w8_dashboard_trigger_and_duplicate_guard(pf_rig, monkeypatch):
    """arun_deployment("aam-backup/backup-lan") is ui.py's exact call: it creates
    a run, and a second click is REFUSED (400) instead of queueing a backup."""
    import flow
    from prefect.deployments import arun_deployment
    from prefect.schedules import Cron

    # register the lab 'backup-lan' deployment exactly as serve.py does, but with
    # lab parameters (the production rig's config.yaml stays untouched)
    dep = flow.backup.to_deployment(
        name="backup-lan",
        parameters={"config_path": str(pf_rig["cfg_path"]), "mode": "lan"},
        schedules=[Cron(pf_rig["cfg"].schedule.lan_cron, pf_rig["cfg"].schedule.timezone)],
        tags=["production", "lan"],
        description="LAB mirror of the production backup-lan deployment",
    )
    asyncio.run(dep.aapply())

    async def _trigger():
        return await arun_deployment(name="aam-backup/backup-lan", timeout=0)

    fr = asyncio.run(_trigger())

    from prefect.client.orchestration import get_client

    async def _state():
        async with get_client() as client:
            r = await client.read_flow_run(fr.id)
            return r

    run = asyncio.run(_state())

    # the dashboard's own duplicate-run guard, fed by this run
    ui = _import_ui(monkeypatch)

    is_running = asyncio.run(ui._is_running("lan"))

    # …and the endpoint itself must REFUSE the second click (400 already_running)
    monkeypatch.setattr(ui, "_config", pf_rig["cfg"])
    monkeypatch.setattr(ui, "_config_loaded_at", time.time())
    import httpx

    async def _post_twice():
        transport = httpx.ASGITransport(app=ui.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://aam-lab") as c:
            r2 = await c.post("/trigger/lan")
            body = r2.json() if r2.headers.get("content-type", "").startswith("application/json") else {}
            return r2.status_code, body

    status2, body2 = asyncio.run(_post_twice())

    L.record("W8.dashboard_trigger",
             "a run created through the deployment API is seen as active, so the "
             "dashboard refuses a duplicate trigger instead of queueing a second backup",
             f"arun_deployment('aam-backup/backup-lan') -> run {fr.id} state="
             f"{run.state.type.value if run.state else '?'} mode={run.parameters.get('mode')} | "
             f"ui._is_running('lan')={is_running} | "
             f"second POST /trigger/lan -> {status2} {body2}",
             "OK — queued run visible to the guard; duplicate click refused with 400 "
             "already_running (no second backup queued)"
             if run.state and run.state.type.value in ("PENDING", "SCHEDULED")
             and is_running is True
             and status2 == 400 and body2.get("status") == "already_running"
             else f"ISSUE — state={run.state.type.value if run.state else '?'} "
                  f"is_running={is_running} second_trigger={status2} {body2}")
    print(f"[W8.2] run_state={run.state.type.value} is_running={is_running} "
          f"second={status2} {body2}")
    assert run.state.type.value in ("PENDING", "SCHEDULED")
    assert is_running is True
    assert status2 == 400 and body2.get("status") == "already_running"

    # keep the run around for a moment: it is queued behind the limit, exactly
    # like a PENDING run waiting for the agent in production


# ── W8.3 — the same endpoint fails CLOSED when Prefect is unreachable ─────────
def test_w8_dashboard_trigger_fails_closed_without_prefect(pf_rig, monkeypatch):
    """H3: Prefect API down -> the trigger endpoint must REFUSE (503), and /status
    must degrade to run_state=unknown instead of claiming idle."""
    ui = _import_ui(monkeypatch)
    from prefect.settings import PREFECT_API_URL, temporary_settings

    monkeypatch.setattr(ui, "_config", pf_rig["cfg"])
    monkeypatch.setattr(ui, "_config_loaded_at", time.time())

    dead = f"http://127.0.0.1:{_dead_port()}/api"
    import httpx

    async def _call():
        transport = httpx.ASGITransport(app=ui.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://aam-lab") as c:
            with temporary_settings(updates={PREFECT_API_URL: dead}):
                trig = await c.post("/trigger/lan")
                trig_cloud = await c.post("/trigger/cloud")
                status = await c.get("/status")
            return trig, trig_cloud, status

    trig, trig_cloud, status = asyncio.run(_call())

    def _json(r):
        try:
            return r.json()
        except Exception:
            return {"_raw": r.text[:200]}

    tb, cb, sb = _json(trig), _json(trig_cloud), _json(status)
    L.record("W8.trigger_fail_closed",
             "with the Prefect API unreachable the dashboard refuses to trigger (503) "
             "and /status degrades to run_state=unknown — it never claims idle",
             f"PREFECT_API_URL={dead} | POST /trigger/lan -> {trig.status_code} {tb} | "
             f"POST /trigger/cloud -> {trig_cloud.status_code} {cb} | "
             f"GET /status -> {status.status_code} lan={sb.get('lan', {}).get('run_state')} "
             f"cloud={sb.get('cloud', {}).get('run_state')} running={sb.get('lan', {}).get('running')}",
             "OK — both triggers refused with 503 (fail-closed), /status degrades to "
             "'unknown' with running=False (fail-open display only)"
             if trig.status_code == 503 and trig_cloud.status_code == 503
             and sb.get("lan", {}).get("run_state") == "unknown"
             and sb.get("lan", {}).get("running") is False
             else f"ISSUE — trigger={trig.status_code} status_lan={sb.get('lan')}")
    print(f"[W8.3] trigger={trig.status_code} cloud={trig_cloud.status_code} "
          f"status={status.status_code} lan={sb.get('lan', {}).get('run_state')}")
    assert trig.status_code == 503 and trig_cloud.status_code == 503
    assert sb["lan"]["run_state"] == "unknown" and sb["lan"]["running"] is False


# ── W8.4 — crash recovery: orphaned runs of a dead agent ─────────────────────
def test_w8_orphaned_runs_cancelled_at_startup(pf_rig, monkeypatch):
    """launch._cancel_orphaned_runs() reaps what a crashed agent left behind,
    and respects a live backup lock when deciding about RUNNING runs."""
    import launch
    import flow
    from core.process import read_lock_alive, write_lock
    from prefect.schedules import Cron

    dep = flow.backup.to_deployment(
        name="backup-lan",
        parameters={"config_path": str(pf_rig["cfg_path"]), "mode": "lan"},
        schedules=[Cron(pf_rig["cfg"].schedule.lan_cron, pf_rig["cfg"].schedule.timezone)],
        tags=["production", "lan"],
    )
    asyncio.run(dep.aapply())

    from prefect.client.orchestration import get_client

    async def _plant_all():
        from prefect.states import Pending, Running

        async with get_client() as client:
            d = await client.read_deployment_by_name("aam-backup/backup-lan")
            runs = []
            for _ in range(2):
                fr = await client.create_flow_run_from_deployment(d.id)
                runs.append(fr.id)
            # mimic a crashed agent: one run made it to RUNNING (slot held, lock
            # live), one is queued but never started
            await client.set_flow_run_state(runs[0], state=Running())
            await client.set_flow_run_state(runs[1], state=Pending())
            return runs[0], runs[1]

    running_id, pending_id = asyncio.run(_plant_all())

    # lock path is derived from CONFIG_PATH inside the production function ->
    # point it at the lab config so nothing outside LAB_ROOT is touched
    monkeypatch.setattr("models.config.CONFIG_PATH", str(pf_rig["cfg_path"]))
    lock = pf_rig["cfg"].paths.backup_lock_path

    async def _state(fid):
        async with get_client() as client:
            r = await client.read_flow_run(fid)
            return r.state.type.value if r.state else "?"

    # case 1: a live backup lock -> the RUNNING run is LEFT ALONE, PENDING reaped
    write_lock(lock)
    launch._cancel_orphaned_runs()
    running_state_1, pending_state_1 = asyncio.run(_state(running_id)), asyncio.run(_state(pending_id))

    # case 2: the crashed process is gone (stale lock) -> everything is reaped
    lock.unlink(missing_ok=True)
    launch._cancel_orphaned_runs()
    running_state_2, pending_state_2 = asyncio.run(_state(running_id)), asyncio.run(_state(pending_id))
    alive_after, pid_after = read_lock_alive(lock)

    L.record("W8.orphan_cancel",
             "the startup cleanup cancels the orphaned runs of a crashed agent, but "
             "never cancels a RUNNING backup whose lock is held by a live process",
             f"planted RUNNING={running_id} PENDING={pending_id} | "
             f"with a LIVE lock: running->{running_state_1} pending->{pending_state_1} | "
             f"with the lock released: running->{running_state_2} pending->{pending_state_2} | "
             f"lock after cleanup: exists={lock.exists()} alive={alive_after}",
             "OK — PENDING always reaped; RUNNING preserved while its lock is live, "
             "cancelled once the lock is gone"
             if pending_state_1 == "CANCELLED" and running_state_1 == "RUNNING"
             and running_state_2 == "CANCELLED" and pending_state_2 == "CANCELLED"
             else f"ISSUE — live-lock: r={running_state_1} p={pending_state_1}; "
                  f"released-lock: r={running_state_2} p={pending_state_2}")
    print(f"[W8.4] live-lock r={running_state_1} p={pending_state_1} | "
          f"released r={running_state_2} p={pending_state_2}")
    assert pending_state_1 == "CANCELLED" and running_state_1 == "RUNNING"
    assert running_state_2 == "CANCELLED" and pending_state_2 == "CANCELLED"


# ── W8.5 — two simultaneous runs serialise on the real concurrency limit ─────
def test_w8_concurrent_triggers_serialise(pf_rig, monkeypatch):
    """Cron fires while the operator also clicks Run: two LAN backups started at
    the same time must serialise (limit=1) — the mirrors never interleave."""
    repo = Path(__file__).resolve().parent.parent
    from prefect.settings import PREFECT_API_URL

    # launch.py hard-sets os.environ["PREFECT_API_URL"]="http://127.0.0.1:4200/api"
    # at import time (that is what the production service needs), and the rig
    # imports launch. That env var then shadows the harness URL for any
    # SUBPROCESS we spawn, while the harness pins the setting with higher
    # precedence in this process - so resolve from the setting, never from
    # os.environ, and pass that to the children.
    api_url = PREFECT_API_URL.value()

    sidecars = [pf_rig["sandbox"] / f"child_out_{i}.json" for i in (1, 2)]
    children = []
    for idx, out_path in enumerate(sidecars, start=1):
        child_code = textwrap.dedent(f"""
            import json, sys, time, pathlib
            sys.path.insert(0, {str(repo)!r})
            import core.lan_sync as ls
            from audit_lab.labconf import strip_zb
            _o = ls.build_robocopy_command
            ls.build_robocopy_command = lambda s, d, c: strip_zb(_o(s, d, c))
            _sync = ls.run_lan_sync
            ev = []
            def probe(*a, **k):
                ev.append(["enter", time.time()])
                try:
                    return _sync(*a, **k)
                finally:
                    ev.append(["exit", time.time()])
            ls.run_lan_sync = probe
            import flow
            flow.run_lan_sync = probe        # the task imports the fn directly
            t0 = time.time()
            outcome = {{}}
            try:
                flow.backup(config_path={str(pf_rig["cfg_path"])!r}, mode="lan")
                outcome["result"] = "OK"
            except Exception as e:
                outcome["result"] = f"{{type(e).__name__}}: {{e}}"
            pathlib.Path({str(out_path)!r}).write_text(
                json.dumps({{"child": {idx}, "t0": t0, "outcome": outcome, "events": ev}}),
                encoding="utf-8")
        """)
        env = dict(os.environ)
        env["PREFECT_API_URL"] = api_url
        env.pop("PREFECT_TEST_MODE", None)   # production behaviour: real slot
        children.append((idx, out_path,
                         subprocess.Popen([sys.executable, "-c", child_code],
                                          cwd=str(repo), env=env)))

    codes = [p.wait(timeout=900) for _i, _p, p in children]
    rows, lan_rows, _cloud = _db_rows(pf_rig["cfg_path"])
    on_nas = len([p for p in pf_rig["nas"].rglob("*.txt")])

    per_child = {}
    for idx, out_path, _p in children:
        if out_path.exists():
            per_child[idx] = json.loads(out_path.read_text(encoding="utf-8"))
        else:
            per_child[idx] = {"child": idx, "t0": None, "outcome": {"result": "NO SIDECAR"},
                              "events": []}
    intervals = []
    for idx in sorted(per_child):
        ev = per_child[idx]["events"]
        for i in range(0, len(ev) - 1, 2):
            intervals.append((idx, ev[i][1], ev[i + 1][1]))
    ordered = sorted(intervals, key=lambda t: t[1])          # by start time
    overlapped = any(ordered[i][2] > ordered[i + 1][1] for i in range(len(ordered) - 1))
    # the child whose mirror starts later must have WAITED for the global slot:
    # (mirror start) - (its own flow start) is the queueing time
    waits = {idx: round(min(e[1] for e in intervals if e[0] == idx)
                        - per_child[idx]["t0"], 2)
             for idx in per_child if any(e[0] == idx for e in intervals)
             and per_child[idx]["t0"] is not None}
    outcomes = {idx: per_child[idx]["outcome"]["result"] for idx in per_child}

    L.record("W8.concurrent_serialise",
             "two LAN backups started at the same moment serialise on the global "
             "'aam-backup' concurrency limit (limit=1) instead of interleaving",
             f"child exit codes={codes} | child outcomes={outcomes} | "
             f"mirror intervals (child, enter, exit)="
             f"{[(c, round(a, 3), round(b, 3)) for c, a, b in ordered]} | "
             f"queue wait per child (mirror start - flow start, s)={waits} | "
             f"intervals overlap={overlapped} | files on NAS={on_nas}/800 | "
             f"run_history={[r['status'] for r in rows]} | manifest lan rows={lan_rows}",
             "OK — the two mirrors ran back-to-back (no overlap), every file copied "
             "exactly once, both runs recorded"
             if codes == [0, 0] and all(v == "OK" for v in outcomes.values())
             and len(rows) == 2 and on_nas == 800 and not overlapped
             else f"ISSUE — codes={codes} outcomes={outcomes} "
                  f"rows={[r['status'] for r in rows]} on_nas={on_nas} overlap={overlapped}")
    print(f"[W8.5] codes={codes} outcomes={outcomes} rows={[r['status'] for r in rows]} "
          f"on_nas={on_nas} intervals={ordered} waits={waits}")
    assert codes == [0, 0]
    assert all(v == "OK" for v in outcomes.values()), outcomes
    assert on_nas == 800 and len(rows) == 2 and not overlapped


# ── W8.6 — the report deployments run as real flow runs (SMTP outage safe) ────
def test_w8_report_flows_contain_smtp_outage(pf_rig):
    """weekly-report / monthly-report are real deployments. M3 3.4 (plan WP8):
    an undelivered report must FAIL the flow run (it used to COMPLETE while
    the email silently failed - Aug 3, Sep 1) and the attempt is recorded."""
    import flow
    import pendulum

    from core.manifest import ManifestDB
    from core.time_utils import IST

    # notifications ON with an SMTP server that refuses connections immediately
    pf_rig["cfg"].notifications.weekly_enabled = True
    pf_rig["cfg"].notifications.monthly_enabled = True
    pf_rig["cfg"].notifications.smtp_host = "127.0.0.1"
    pf_rig["cfg"].notifications.smtp_port = _dead_port()
    pf_rig["cfg"].notifications.smtp_username = "u"
    pf_rig["cfg"].notifications.smtp_password = "p"
    pf_rig["cfg"].notifications.sender = "lab@example.invalid"
    pf_rig["cfg"].notifications.recipients = ["ops@example.invalid"]
    pf_rig["cfg"].notifications.retry_base_seconds = 0     # no real back-off in the lab
    write_lab_config(pf_rig["sandbox"], pf_rig["cfg"])
    # one recent run, so the report has content (not the STALE case)
    db = ManifestDB(str(pf_rig["cfg"].paths.database_path))
    now = pendulum.now(IST).isoformat()
    db.insert_run({"run_id": "w8-seed", "mode": "lan", "started_at": now, "ended_at": now,
                   "status": "LAN_COMPLETE"})
    # the monthly report covers the previous calendar month -> seed a run there too
    prev = pendulum.now(IST).start_of("month").subtract(months=1).add(days=14).isoformat()
    db.insert_run({"run_id": "w8-seed-prev", "mode": "lan", "started_at": prev, "ended_at": prev,
                   "status": "LAN_COMPLETE"})
    db.close()

    results = {}
    for name in ("weekly", "monthly"):
        exc = None
        try:
            if name == "weekly":
                flow.weekly_report_flow(config_path=str(pf_rig["cfg_path"]))
            else:
                flow.monthly_report_flow(config_path=str(pf_rig["cfg_path"]))
        except Exception as e:  # noqa: BLE001
            exc = f"{type(e).__name__}: {e}"
        results[name] = exc

    db = ManifestDB(str(pf_rig["cfg"].paths.database_path))
    try:
        recorded = [db.last_notification(f"report:{n}") for n in ("weekly", "monthly")]
    finally:
        db.close()
    L.record("W8.report_flows",
             "an undelivered report FAILS its real flow run and the attempt is recorded",
             f"smtp=127.0.0.1:{pf_rig['cfg'].notifications.smtp_port} (refusing) | "
             f"weekly={results['weekly']} | monthly={results['monthly']} | "
             f"recorded={[r and r['status'] for r in recorded]}",
             "OK — both report flows FAILED truthfully and the delivery failure is on record"
             if all(r and "not delivered" in r for r in results.values())
             else f"ISSUE — {results}")
    print(f"[W8.6] {results}")
    assert all(r and "not delivered" in r for r in results.values()), results
    assert [r and r["status"] for r in recorded] == ["FAILED", "FAILED"]

def test_w8_backup_flow_leg_scoped_modes(pf_rig):
    """backup(mode='cloud') and backup(mode='lan') — what the two daily
    deployments actually pass — record only their own leg."""
    flow = pf_rig["flow"]
    cfg_path = str(pf_rig["cfg_path"])

    excs = {}
    for mode in ("cloud", "lan"):
        try:
            flow.backup(config_path=cfg_path, mode=mode)
            excs[mode] = None
        except Exception as e:  # noqa: BLE001
            excs[mode] = f"{type(e).__name__}: {str(e)[:200]}"

    rows, lan_rows, cloud_rows = _db_rows(pf_rig["cfg_path"])
    modes = sorted({r["mode"] for r in rows})
    statuses = [f"{r['mode']}:{r['status']}" for r in rows]
    L.record("W8.leg_scoped_modes",
             "each leg's deployment runs only its own pipeline (mode parameter) and "
             "records exactly one run_history row",
             f"mode=cloud raised={excs['cloud']} | mode=lan raised={excs['lan']} | "
             f"run_history={statuses} | modes present={modes} | "
             f"manifest lan_rows={lan_rows} cloud_rows={cloud_rows}",
             "OK — one cloud row and one LAN row, no cross-leg writes"
             if len(rows) == 2 and modes == ["cloud", "lan"]
             and all(s.endswith(("COMPLETE", "NO_CHANGES_COMPLETE")) for s in statuses)
             else f"ISSUE — rows={statuses} raised={excs}")
    print(f"[W8.7] rows={statuses} lan={lan_rows} cloud={cloud_rows} excs={excs}")
    assert len(rows) == 2 and set(modes) == {"cloud", "lan"}
    assert excs["cloud"] is None and excs["lan"] is None
