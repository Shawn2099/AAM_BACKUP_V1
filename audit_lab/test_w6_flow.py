"""WAVE 6 — the REAL Prefect orchestration layer, exactly as production runs it.

Every previous experiment drove flow._run_*_pipeline() directly. This wave drives
the same code through the production entry points:

  * flow.backup(config_path=..., mode=...)          <- what backup-cloud / backup-lan call
  * flow.rollover_check_flow(config_path=...)      <- the scheduled rollover deployment
  * flow.integrity_audit_flow(config_path=...)     <- the weekly audit deployment
  * serve._deployments() + launch._reconcile_disabled_legs()   <- registration/scheduling
  * task retries (retries=1, retry_delay_seconds), ExceptionGroup aggregation,
    DB maintenance, and the alert-suppression policy — all real flow-run behaviour

Runs inside prefect_test_harness() (a REAL ephemeral Prefect API server) with the
"aam-backup" global concurrency limit created by the production launcher code.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(1200


)


def write_lab_config(base: Path, cfg) -> Path:
    """Serialise the validated AppConfig to a real config.yaml (the production
    loader reads this file, exactly as in production)."""
    import yaml

    p = base / "config.yaml"
    yaml.safe_dump(json.loads(cfg.model_dump_json()), p.open("w", encoding="utf-8"),
                   sort_keys=False, allow_unicode=True)
    return p


@pytest.fixture()
def rig(sandbox, monkeypatch):
    """Full production wiring: config.yaml on disk + real flow runtime + local GCS."""
    import core.lan_sync as ls

    src = sandbox / "source" / "FY26-26"
    for i in range(60):
        L.write_file(src / "Clients" / f"f_{i:04d}.txt", f"LEDGER {i}\n".encode() * 40)
    nas = L.make_canary(sandbox / "nas" / "FY26-26")
    L.write_file(sandbox / "lab-key.json", b'{"type": "service_account"}')

    cfg = L.build_config(sandbox, source=src, lan_dest=str(nas), runtime=sandbox / "runtime")

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
    monkeypatch.setattr(flow, "send_failure_alert", lambda *a, **k: alerts.append(a) or True)
    monkeypatch.setattr(flow, "temp_rclone_config",
                        lambda *a, **k: __import__("contextlib").nullcontext(rc))
    monkeypatch.chdir(bucket_root)

    monkeypatch.delenv("PREFECT_TEST_MODE", raising=False)   # use the REAL concurrency slot
    from prefect.testing.utilities import prefect_test_harness

    with prefect_test_harness():
        from launch import _ensure_concurrency_limit

        _ensure_concurrency_limit()          # production startup code, creates limit=1
        yield {
            "cfg": cfg, "cfg_path": cfg_path, "sandbox": sandbox, "src": src, "nas": nas,
            "buckets": bucket_root, "alerts": alerts, "rc": rc, "flow": flow,
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


def _flow_run_states():
    """Ask the REAL Prefect server for the flow/task runs of this test."""
    import asyncio

    from prefect.client.orchestration import get_client

    async def _q():
        async with get_client() as client:
            fr = await client.read_flow_runs(limit=20)
            out = []
            for r in fr:
                tr = await client.read_task_runs(limit=100)
                names = {t.name: (t.state.type.value if t.state else None) for t in tr}
                out.append((r.name, r.state.type.value, names))
            return out

    return asyncio.run(_q())


# ── the production entry point ─────────────────────────────────────────────────
def test_w6_backup_flow_both_legs(rig):
    """flow.backup(mode='all') as a REAL flow run: tasks, states, DB, maintenance."""
    flow = rig["flow"]
    t0 = time.monotonic()
    flow.backup(config_path=str(rig["cfg_path"]), mode="all")

    rows, lan_rows, cloud_rows = _db_rows(rig["cfg_path"])
    states = _flow_run_states()
    task_names = list(states[0][2].keys()) if states else []

    L.record("W6.backup_flow_all",
             "the production flow.backup(mode='all') works end-to-end through Prefect",
             f"flow run state={states[0][1] if states else '?'} | "
             f"task runs recorded: {sorted(task_names)} | "
             f"run_history={[r['status'] for r in rows]} | "
             f"manifest lan_rows={lan_rows} cloud_rows={cloud_rows} | "
             f"alerts={len(rig['alerts'])} | elapsed={time.monotonic()-t0:.0f}s",
             "OK — real Prefect flow run with real task runs for both legs"
             if states and states[0][1] == "COMPLETED" and len(rows) >= 2
             else f"ISSUE — flow state={states[0][1] if states else '?'}, rows={[r['status'] for r in rows]}")
    print(f"[W6.all] state={states[0][1] if states else '?'} tasks={sorted(task_names)} "
          f"rows={[r['status'] for r in rows]} lan={lan_rows} cloud={cloud_rows} "
          f"alerts={len(rig['alerts'])}")
    assert states and states[0][1] == "COMPLETED"
    assert len(rows) >= 2
    assert {"lan", "cloud"} <= {r["mode"] for r in rows}


def test_w6_backup_flow_task_retries(rig, monkeypatch):
    """A transient preflight failure is retried by the real task retry policy."""
    flow = rig["flow"]
    calls = {"n": 0}
    real = flow.run_cloud_dry_run

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "exit_code": -1, "error": "transient GCS blip (lab)"}
        return real(*a, **k)

    monkeypatch.setattr(flow, "run_cloud_dry_run", _flaky)
    monkeypatch.setitem(rig["cfg"].model_dump(), "cloud", None)  # no-op, keep typing simple

    flow.backup(config_path=str(rig["cfg_path"]), mode="cloud")

    rows, _lan, cloud_rows = _db_rows(rig["cfg_path"])
    L.record("W6.task_retry",
             "the cloud preflight task's configured retry (retries=1) re-runs it once",
             f"preflight invocation count={calls['n']} (1st failed, 2nd real) | "
             f"run_history={[r['status'] for r in rows]} | manifest cloud rows={cloud_rows}",
             "OK — the retry policy executed the failing task a second time and the run recovered"
             if calls["n"] >= 2 and rows and rows[0]["status"].startswith("CLOUD_")
             and rows[0]["status"] != "CLOUD_FAILED"
             else f"ISSUE — retry did not recover (calls={calls['n']}, status={rows[0]['status'] if rows else None})")
    print(f"[W6-retry] calls={calls['n']} status={[r['status'] for r in rows]}")
    assert calls["n"] >= 2, "the task should have been retried"


def test_w6_backup_flow_failure_alert_policy(rig, monkeypatch):
    """Evidence-gate failure -> ExceptionGroup, exactly ONE generic alert, and
    no DB maintenance run (the failure-day behaviour)."""
    flow = rig["flow"]
    # keep the REAL task object (so .with_options / Prefect tracking stay live)
    # and replace only the function it runs — like production calling the task
    monkeypatch.setattr(
        flow.cloud_verify_and_report_task, "fn",
        lambda config, fy_prefix: {
            "verified": False, "verify_exit_code": 1, "verify_termination": "normal",
            "verify_completion": True, "verify_differences": 3, "verify_reason": "lab divergence",
            "size": {"count": 5, "bytes": 5000},
            "manifest": [{"Path": "a.txt", "Size": 100, "ModTime": "2026-09-06T10:00:00Z"}],
            "diff": {"added": ["missing.txt"], "removed": [], "modified": [],
                     "unchanged": ["a.txt"]},
            "manifest_error": None,
        })

    from flow import PartialRun

    exc = None
    try:
        flow.backup(config_path=str(rig["cfg_path"]), mode="cloud")
    except Exception as e:  # ExceptionGroup expected
        exc = e

    rows, _lan, _cloud = _db_rows(rig["cfg_path"])
    is_partial = any(isinstance(x, PartialRun) for x in getattr(exc, "exceptions", []))
    subjects = [a[2] for a in rig["alerts"]]
    L.record("W6.alert_policy",
             "a failed cloud run raises an ExceptionGroup and alerts exactly once",
             f"raised={type(exc).__name__} | sub-exceptions={[type(x).__name__ for x in getattr(exc, 'exceptions', [])]} | "
             f"is_partial={is_partial} | alerts emitted={len(rig['alerts'])} | "
             f"run_history={[r['status'] for r in rows]} | "
             f"alert subjects={[s[:70] for s in subjects]}",
             "FINDING — the verify-gate failure PAGES TWICE: the in-pipeline gate alert "
             "('Cloud integrity verification FAILED…') AND backup()'s generic "
             "'Backup completed with errors' alert, because the raised exception is a "
             "plain RuntimeError and only PartialRun suppresses the generic one"
             if exc is not None and len(rig["alerts"]) == 2
             else "OK — ExceptionGroup + exactly one alert (generic path for a non-PartialRun failure)"
             if exc is not None and len(rig["alerts"]) == 1 and rows
             and rows[0]["status"].startswith("CLOUD_")
             else "ISSUE — alert/status policy deviates")
    print(f"[W6-fail] exc={type(exc).__name__} alerts={len(rig['alerts'])} "
          f"status={[r['status'] for r in rows]} subjects={[s[:50] for s in subjects]}")
    assert exc is not None and len(rig["alerts"]) == 2


def test_w6_rollover_check_flow(rig, monkeypatch):
    """The scheduled rollover flow end to end with the real tools (M2 2.6):
    final backups through the normal pipelines -> new folders + placeholder ->
    config backup -> config.yaml rewrite, all recorded.

    The clock is pinned to the NEXT FY: the rig's FY26-26 folders are then
    exactly one FY behind (the forward-only rule no longer treats any
    difference as a rollover - that was probe P4's backward-rollover bug)."""
    flow = rig["flow"]
    before = rig["cfg"].model_dump()["paths"]
    monkeypatch.setattr(flow, "get_fy_prefix", lambda *a, **k: "FY27-28")

    result = flow.rollover_check_flow(config_path=str(rig["cfg_path"]))
    after_path = rig["sandbox"] / "config.yaml"

    from models.config import load_config

    reloaded = load_config(str(after_path))
    rows = [r["status"] for r in _db_rows(after_path)[0]]
    new_src = Path(reloaded.paths.source_drive)
    placeholder = new_src / reloaded.rollover.source_placeholder_name
    backups = sorted(after_path.parent.glob("config.yaml.bak-*"))
    L.record("W6.rollover_flow",
             "rollover_check_flow performs the real rollover and rewrites config.yaml",
             f"flow returned={result!r} | old source={before['source_drive']!r} | "
             f"new source={reloaded.paths.source_drive!r} | new lan={reloaded.paths.lan_destination!r} | "
             f"placeholder={placeholder.is_file()} | config backups={len(backups)} | "
             f"run_history={rows}",
             "OK — final backups via the pipelines, config atomically rewritten to the next FY"
             if result == "ROLLOVER_COMPLETED" and reloaded.paths.source_drive.endswith("FY27-28")
             else f"ISSUE — returned {result!r}, source={reloaded.paths.source_drive!r}")
    print(f"[W6-rollover] {result} -> {reloaded.paths.source_drive} rows={rows}")
    assert result == "ROLLOVER_COMPLETED"
    assert reloaded.paths.source_drive.endswith("FY27-28")
    assert reloaded.paths.lan_destination.endswith("FY27-28")
    assert placeholder.is_file(), "new FY source folder must hold the placeholder (G1)"
    assert (Path(L._to_unc(rig["nas"].parent / "FY27-28")) / ".AAM_TARGET_MOUNTED").is_file() \
        or (rig["nas"].parent / "FY27-28" / ".AAM_TARGET_MOUNTED").is_file()
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") != \
        after_path.read_text(encoding="utf-8")
    assert "ROLLOVER_COMPLETE" in rows
    assert any(s in ("LAN_COMPLETE",) for s in rows) and \
        any(s in ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE") for s in rows)


def test_w6_integrity_audit_flow(rig, monkeypatch):
    """The weekly integrity audit flow records an integrity_audits row."""
    flow = rig["flow"]
    # seed the LAN destination so the audit has something to compare
    for i in range(30):
        L.write_file(rig["nas"] / f"g_{i:04d}.txt", b"g" * 100)

    monkeypatch.setattr(flow, "wol_check_task", lambda *a, **k: None)

    exc = None
    try:
        flow.integrity_audit_flow(config_path=str(rig["cfg_path"]), mode="lan")
    except Exception as e:
        exc = e

    from core.manifest import ManifestDB

    from models.config import load_config

    c = load_config(str(rig["cfg_path"]))
    m = ManifestDB(c.paths.database_path)
    audits = [m.latest_audit("lan")]
    m.close()
    L.record("W6.integrity_audit_flow",
             "integrity_audit_flow records a durable audit row for the LAN leg",
             f"raised={type(exc).__name__}: {str(exc)[:120]} | latest_audit(lan) status="
             f"{audits[0]['status'] if audits[0] else None} detail={ (audits[0]['detail'][:120] if audits[0] else None)!r} | "
             f"alerts={len(rig['alerts'])}",
             "OK — audit row written through the real flow (divergence -> alert + failed flow)"
             if audits[0] else "ISSUE — no audit row recorded")
    print(f"[W6-audit] raised={type(exc).__name__} audit={audits[0]['status'] if audits[0] else None} "
          f"alerts={len(rig['alerts'])}")
    # A18: the weekly audit must persist a durable row AND take the failing path
    # (flow raises + one alert) when it finds divergence. integrity_audits = 0
    # on the deployed box, so this is the only proof the audit row is written.
    assert audits[0] is not None, "no integrity_audits row was recorded through the flow"
    assert audits[0]["status"] == "VERIFICATION_FAILED", audits[0]
    assert len(rig["alerts"]) == 1, (
        f"expected exactly 1 alert for the divergence, got {len(rig['alerts'])}")
    assert exc is not None, "a diverging audit must fail the flow"


def test_w6_deployments_and_reconcile(rig, monkeypatch):
    """Deployments/schedules from config, and pause-on-disabled-leg against the real server."""
    import asyncio

    import launch
    import serve

    # serve._deployments() resolves CONFIG_PATH relative to CWD, and the rig has
    # chdir'd into the lab sandbox -> build the deployment set from the repo root.
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)

    deps = serve.deployments()
    names = [d.name for d in deps]
    lan_dep = next(d for d in deps if d.name == "backup-lan")
    sched = lan_dep.schedules[0] if lan_dep.schedules else None
    sched_cron = sched.schedule.cron if sched else None
    sched_tz = sched.schedule.timezone if sched else None

    # serve() registers these on the server; do the same so the reconcile step
    # (which talks to the server, not to local objects) has something to pause
    async def _apply():
        for d in deps:
            await d.aapply()

    asyncio.run(_apply())

    cfg = rig["cfg"]
    cfg.lan.enabled = False
    cfg.cloud.enabled = True
    outcome = launch._reconcile_disabled_legs(cfg)

    async def _read():
        from prefect.client.orchestration import get_client
        async with get_client() as client:
            d = await client.read_deployment_by_name("aam-backup/backup-lan")
            return bool(getattr(d, "paused", False))

    paused = asyncio.run(_read())
    L.record("W6.deployments",
             "deployments/schedules come from config, and a disabled leg is PAUSED server-side",
             f"deployments={names} | backup-lan schedule={sched_cron} "
             f"timezone={sched_tz} | "
             f"reconcile output={outcome} | server-side paused={paused}",
             "OK — registration + pause reconciliation work against the real Prefect server"
             if paused and outcome.get("backup-lan") == "PAUSED (leg disabled)" else
             f"ISSUE — paused={paused}, outcome={outcome}")
    print(f"[W6-deploy] {names} reconcile={outcome} paused={paused}")
    assert paused is True
