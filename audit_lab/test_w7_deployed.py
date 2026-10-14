"""WAVE 7 — tests against the DEPLOYED version (real services, real everything).

Everything up to Wave 6 tested the pipeline in-process. This file tests the
installed system, exactly as production runs:

  * the three NSSM services (AamPrefectServer / AamBackupAgent / AamWatchdog)
  * the real Prefect API on 127.0.0.1:4200 and the deployments registered by
    serve.py / launch.py (real cron + timezone from config.yaml)
  * backups triggered through the DEPLOYMENT API (the same call the dashboard's
    "Run Backup" button and the Prefect UI "Quick run" make)
  * the real runtime dir C:\\BackupAgent (real manifest.db, real logs, real lock)
  * the real LAN destination and the real GCS bucket from config.yaml
  * the dashboard endpoints and the watchdog's recovery behaviour

Run it only AFTER the services are installed:
    .\\.venv\\Scripts\\python.exe -m pytest audit_lab/test_w7_deployed.py -q -s --timeout=1800
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from audit_lab import labconf as L

# The deployed services run against the real Prefect API on 127.0.0.1:4200.
# Without this, Prefect 3 spins up its own EPHEMERAL server (random port) which has
# no deployments registered - so every client call 404s. launch.py sets the same
# variable for the agent process; we must set it for the test process too.
os.environ.setdefault("PREFECT_API_URL", "http://127.0.0.1:4200/api")
os.environ.setdefault("PREFECT_HOME", r"C:\BackupAgent\.prefect")

pytestmark = pytest.mark.timeout(1200)

# ── deployed-system facts ───────────────────────────────────────────────────────
API_URL = "http://127.0.0.1:4200/api"
DASH = "http://127.0.0.1:8080"
SERVICES = ("AamPrefectServer", "AamBackupAgent", "AamWatchdog")


def _require_services() -> None:
    missing = [s for s in SERVICES
               if subprocess.run(["sc", "query", s], capture_output=True, text=True).returncode != 0]
    if missing:
        pytest.fail(f"services not installed: {missing} - run audit_lab\\bootstrap_deployed.ps1 "
                    "from an ADMINISTRATOR PowerShell first")


def _cfg():
    """The REAL config.yaml the deployed services use (read-only)."""
    from models.config import AppConfig

    with open(Path(__file__).resolve().parent.parent / "config.yaml", encoding="utf-8") as f:
        import yaml
        return AppConfig(**yaml.safe_load(f))


def _client():
    from prefect.client.orchestration import get_client
    return get_client()


def _trigger_and_wait(deployment: str, params: dict | None = None, timeout_s: int = 900):
    """Trigger a run the way the dashboard does, then wait for a terminal state."""
    import asyncio

    from prefect.deployments import arun_deployment

    async def _run():
        fr = await arun_deployment(name=deployment, parameters=params or {}, timeout=0)
        flow_id = fr.id
        async with _client() as client:
            deadline = time.time() + timeout_s
            last = None
            while time.time() < deadline:
                r = await client.read_flow_run(flow_id)
                last = r
                name = r.state.type.value if r.state else "UNKNOWN"
                if name not in ("PENDING", "RUNNING", "SCHEDULED", "PAUSED"):
                    return r
                await asyncio.sleep(3)
            raise TimeoutError(f"flow run {flow_id} still {last.state.type.value if last else '?'} "
                               f"after {timeout_s}s")

    return asyncio.run(_run())


def _db_rows(mode: str | None = None, limit: int = 10) -> list[dict]:
    cfg = _cfg()
    from core.manifest import ManifestDB
    m = ManifestDB(cfg.paths.database_path)
    rows = m.get_recent_runs(limit)
    m.close()
    return [r for r in rows if mode is None or r["mode"] == mode]


# ── d1/d2: the deployment itself ───────────────────────────────────────────────
def test_d1_services_running_with_expected_logons():
    _require_services()
    out = {}
    for s in SERVICES:
        r = subprocess.run(["sc", "query", s], capture_output=True, text=True)
        state = next((ln.split(":", 1)[1].split()[1] for ln in r.stdout.splitlines()
                      if ln.strip().startswith("STATE") and len(ln.split(":", 1)[1].split()) > 1),
                     "UNKNOWN")
        acct = subprocess.run(["sc", "qc", s], capture_output=True, text=True).stdout
        start = next((ln.split(":", 1)[1].strip() for ln in acct.splitlines()
                      if "SERVICE_START_NAME" in ln), "?")
        out[s] = (state, start)
    for s, (state, acct) in out.items():
        print(f"[d1] {s}: {state}  logon={acct}")
    L.record("DEPLOYED.services",
             "the three services are installed and running",
             "\n".join(f"  {k}: state={v[0]} logon={v[1]}" for k, v in out.items()),
             "OK - all three running" if all(v[0] == "RUNNING" for v in out.values())
             else "ISSUE - at least one service is not RUNNING")
    assert all(v[0] == "RUNNING" for v in out.values()), out


def test_d2_prefect_api_and_deployments():
    _require_services()
    import asyncio

    cfg = _cfg()

    async def _q():
        async with _client() as c:
            deps = await c.read_deployments()
            return [(d.name, [f"{s.schedule.cron}/{s.schedule.timezone}" for s in (d.schedules or [])]
                     if d.schedules else []) for d in deps]

    deps = asyncio.run(_q())
    expected = {
        "backup-cloud": (cfg.schedule.cloud_cron, cfg.schedule.timezone),
        "backup-lan":   (cfg.schedule.lan_cron, cfg.schedule.timezone),
    }
    got = {n: s for n, s in deps}
    problems = []
    for name, (cron, tz) in expected.items():
        if name not in got:
            problems.append(f"{name} NOT registered")
        elif cron not in (got[name][0] if got[name] else ""):
            problems.append(f"{name} schedule mismatch: config={cron}/{tz} registered={got[name]}")
    L.record("DEPLOYED.deployments",
             "the deployments are registered with the config's cron + timezone",
             f"registered: {deps}\nconfig expects: {expected}",
             "OK - schedules match config.yaml" if not problems else f"ISSUE - {problems}")
    print(f"[d2] {deps}")
    assert not problems, problems


def test_d3_dashboard_endpoints():
    _require_services()
    import httpx

    with httpx.Client(base_url=DASH, timeout=20) as c:
        h = c.get("/health")
        st = c.get("/status")
        root = c.get("/")
    body = {}
    try:
        body = st.json()
    except Exception as e:  # non-JSON /status is itself a finding
        body = {"_parse_error": f"{type(e).__name__}: {e}"}

    def _last_run(leg: str) -> str:
        # NOTE: the API returns explicit null for last_run/last_success when there are
        # no runs yet, so .get(key, default) does NOT protect - the key exists.
        run = (body.get(leg) or {}).get("last_run") or {}
        return run.get("status") or "no-runs-yet"

    L.record("DEPLOYED.dashboard",
             "the dashboard is serving status from the real manifest.db",
             f"/health={h.status_code} /status={st.status_code} /={root.status_code} | "
             f"cloud={_last_run('cloud')} lan={_last_run('lan')} | "
             f"manifest lan_files={body.get('manifest', {}).get('lan_files')} "
             f"cloud_files={body.get('manifest', {}).get('cloud_files')} | "
             f"source free={body.get('health', {}).get('source_free_gb')}GB",
             "OK - dashboard reachable and reporting (no runs yet is expected)"
             if st.status_code == 200 and root.status_code == 200
             else f"ISSUE - status={st.status_code} root={root.status_code}")
    print(f"[d3] health={h.status_code} status={st.status_code} root={root.status_code}")
    assert st.status_code == 200 and root.status_code == 200


# ── d4/d5: real backups through the deployment API ────────────────────────────
def test_d4_real_lan_backup_via_deployment():
    _require_services()
    cfg = _cfg()
    before = len([p for p in Path(cfg.paths.lan_destination).rglob("*") if p.is_file()])
    r = _trigger_and_wait("aam-backup/backup-lan")
    state = r.state.type.value if r.state else "?"
    after = [p for p in Path(cfg.paths.lan_destination).rglob("*") if p.is_file()]
    canary = Path(cfg.paths.lan_destination, ".AAM_TARGET_MOUNTED").exists()
    rows = _db_rows("lan", 3)

    src_files = [p for p in Path(cfg.paths.source_drive).rglob("*") if p.is_file()]
    copied = {p.name for p in after}
    expected = {p.name for p in src_files}
    missing = sorted(expected - copied)

    L.record("DEPLOYED.lan_backup",
             "a LAN backup triggered through the deployment API really copies the files",
             f"flow run state={state} | source files={len(src_files)} | "
             f"files at destination before={before} after={len(after)} (canary kept={canary}) | "
             f"MISSING at destination={missing} | run_history={[r['status'] for r in rows]} | "
             f"exit_code={rows[0]['exit_code'] if rows else None} files_failed={rows[0]['files_failed'] if rows else None}",
             "OK - LAN leg copied every file, canary preserved, run recorded"
             if state == "COMPLETED" and not missing and canary else
             f"ISSUE - state={state}, missing={missing}, canary={canary}")
    print(f"[d4] state={state} src={len(src_files)} dest={len(after)} missing={missing} "
          f"canary={canary} rows={[r['status'] for r in rows]}")
    assert state == "COMPLETED" and not missing and canary


def test_d5_real_cloud_backup_via_deployment():
    _require_services()
    cfg = _cfg()
    from core.time_utils import get_fy_prefix

    fy = get_fy_prefix()
    r = _trigger_and_wait("aam-backup/backup-cloud")
    state = r.state.type.value if r.state else "?"
    rows = _db_rows("cloud", 3)

    # verify with the same rclone the service uses (real GCS, real key)
    from core.rclone_config import temp_rclone_config

    with temp_rclone_config(cfg.paths.gcs_key_path, cfg.cloud.location,
                            cfg.cloud.project_number, cfg.cloud.storage_class) as rc:
        out = subprocess.run(
            [str(Path(__file__).resolve().parent.parent / "deploy" / "bin" / "rclone.exe"),
             "lsjson", f"aam_gcs:{cfg.cloud.bucket}/{fy}", "-R", "--config", rc],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        try:
            objects = [o["Path"] for o in json.loads(out.stdout or "[]")]
        except Exception:
            objects = [f"<parse failed: {out.stderr[:200]}>"]

    src_names = {p.name for p in Path(cfg.paths.source_drive).rglob("*") if p.is_file()}
    cloud_names = {os.path.basename(o) for o in objects if not o.startswith("<")}
    missing = sorted(src_names - cloud_names)

    L.record("DEPLOYED.cloud_backup",
             "a cloud backup triggered through the deployment API really uploads to GCS",
             f"flow run state={state} | FY prefix={fy} | bucket={cfg.cloud.bucket} | "
             f"objects in GCS={len(objects)} -> {objects[:8]} | "
             f"source files={len(src_names)} MISSING from GCS={missing} | "
             f"run_history={[r['status'] for r in rows]}",
             "OK - every source file is in the real bucket, run recorded"
             if state == "COMPLETED" and not missing else
             f"ISSUE - state={state}, missing={missing}")
    print(f"[d5] state={state} fy={fy} objects={len(objects)} missing={missing} "
          f"rows={[r['status'] for r in rows]}")
    assert state == "COMPLETED" and not missing


# ── d6: the /ZB + service-account interaction, on the deployed box ─────────────
def test_d6_lan_zb_and_service_account():
    """Document the deployed Log On account and what /ZB does for it."""
    _require_services()
    from audit_lab.labconf import run as _run

    acct = next((ln.split(":", 1)[1].strip() for ln in
                 subprocess.run(["sc", "qc", "AamBackupAgent"], capture_output=True, text=True
                                ).stdout.splitlines() if "SERVICE_START_NAME" in ln), "?")

    # does that account hold SeBackupPrivilege?
    priv = _run(["whoami", "/priv"], timeout=30).stdout
    has_zb = "SeBackupPrivilege" in priv
    rows = _db_rows("lan", 3)
    exit_codes = [(r["status"], r["exit_code"]) for r in rows]

    L.record("DEPLOYED.zb_service_account",
             "the LAN leg's /ZB behaviour matches the service Log On account",
             f"AamBackupAgent logs on as: {acct} | "
             f"whoami /priv for THIS session: SeBackupPrivilege={has_zb} | "
             f"recent LAN runs (status, exit_code)={exit_codes} | "
             f"exit 16 = /ZB hard failure, exit 1/3 = copy happened",
             "OK - the LAN leg is copying (no 16s)" if not any(c == 16 for _s, c in exit_codes)
             else "ISSUE - a LAN run exited 16: the service account lacks SeBackupPrivilege "
                  "(run audit_lab\\setup_service_account.ps1 -Apply)")
    print(f"[d6] logon={acct} SeBackup={has_zb} recent={exit_codes}")
    # informational only: the account may legitimately be LocalSystem (which has it)


# ── d7: watchdog recovery ──────────────────────────────────────────────────────
@pytest.mark.timeout(900)
def test_d7_watchdog_restarts_hung_prefect_server():
    _require_services()
    subprocess.run(["sc", "stop", "AamPrefectServer"], capture_output=True, text=True)
    time.sleep(5)
    revived, waited = False, 0
    for _ in range(int(700 / 15)):
        r = subprocess.run(["sc", "query", "AamPrefectServer"], capture_output=True, text=True)
        if "RUNNING" in r.stdout:
            revived = True
            break
        time.sleep(15)
        waited += 15
    L.record("DEPLOYED.watchdog_recovery",
             "the watchdog restarts a stopped Prefect server (no manual action)",
             f"stopped AamPrefectServer -> back to RUNNING={revived} after ~{waited}s "
             f"(detection is 5 x 60s + restart)",
             "OK - self-healing works" if revived else "ISSUE - the watchdog did not revive the service")
    print(f"[d7] revived={revived} after {waited}s")
    assert revived is True


# ── d8: crash + stale-lock recovery on the deployed runtime dir ───────────────
def test_d8_crash_then_next_run_recovers():
    _require_services()
    cfg = _cfg()
    lock = cfg.paths.backup_lock_path
    # simulate a crashed run's stale lock: a PID that cannot exist
    Path(lock).write_text("999999:999999.0", encoding="utf-8")
    r = _trigger_and_wait("aam-backup/backup-lan")
    state = r.state.type.value if r.state else "?"
    lock_after = Path(lock).exists()
    rows = _db_rows("lan", 2)
    L.record("DEPLOYED.stale_lock_recovery",
             "a stale lock from a crashed run does not block the next backup",
             f"planted stale lock {lock} -> flow state={state} | lock still present after run="
             f"{lock_after} | run_history={[r['status'] for r in rows]}",
             "OK - stale lock collected, backup ran" if state == "COMPLETED" and not lock_after
             else f"ISSUE - state={state}, lock_present={lock_after}")
    print(f"[d8] state={state} lock_after={lock_after}")
    assert state == "COMPLETED" and not lock_after
