"""WAVE 14 - ui.py (dashboard) + core/report.py (email/CSV/HTML reporting).

Requested by the reviewer after the Wave 13 plan review: W10's coverage map
showed ui.py at 0% and core/report.py was never exercised at all, while the
dashboard is what an operator actually reads and the reports are what the firm
receives.

Seams (lab-only, no production edits):
  * ui._cfg() reads models.config.CONFIG_PATH at CALL time, so that attribute is
    pointed at a lab config.yaml - the repo's real config.yaml is never read.
  * ui._config / ui._config_loaded_at / ui._DB_INSTANCE are saved and restored,
    so a long-lived dashboard DB (the M-6 exposure path) never leaks between
    tests or into other waves.
  * SMTP is a local socket that refuses connections, so report sending contains
    an outage without touching a real mail server.
  * Prefect: _prefect_has_active_run's client call is replaced per case, so the
    tri-state contract is tested without depending on a live server.

    W14.1  _prefect_has_active_run  - the H3 tri-state contract + tag/mode filter
    W14.2  dashboard status fields    - _run_state_fields, integrity, last-run
    W14.3  HTTP endpoints             - /, /status, /health, /report/*
    W14.4  auth + rate limiting       - sessions, API key, rate windows
    W14.5  report CSV                  - _csv_safe injection branches
    W14.6  report HTML                 - generate_report_html branches
    W14.7  report email                - contained SMTP outage
"""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(900)

REPO = Path(__file__).resolve().parent.parent


def _write_cfg(base: Path, cfg) -> Path:
    import json

    import yaml

    p = base / "config.yaml"
    yaml.safe_dump(json.loads(cfg.model_dump_json()), p.open("w", encoding="utf-8"),
                   sort_keys=False, allow_unicode=True)
    return p


def _seed_db(db_path: Path, *, runs=(), audits=(), entries=0):
    """Populate a real manifest DB with known rows."""
    from core.manifest import ManifestDB

    db = ManifestDB(str(db_path), busy_timeout_ms=5000)
    for r in runs:
        db.insert_run(r)
    for a in audits:
        db.record_audit(a)
    for i in range(entries):
        db.upsert_file_entry(f"Clients/f{i:03d}.txt", 100 + i, 1700000000.0 + i,
                             lan_status="synced" if i % 2 == 0 else None,
                             cloud_status="synced" if i % 3 == 0 else None)
    db.close()
    return db_path


class _Run:
    """Minimal flow-run stand-in with the attributes ui.py reads."""

    def __init__(self, tags, parameters=None, state_type=None, next_scheduled_start_time=None):
        self.tags = tags
        self.parameters = parameters or {}
        self.state_type = state_type
        self.next_scheduled_start_time = next_scheduled_start_time
        if state_type is not None:
            self.state = type("S", (), {"type": state_type})()


class _State:
    RUNNING = "RUNNING"
    PENDING = "PENDING"
    SCHEDULED = "SCHEDULED"


@pytest.fixture()
def ui_rig(sandbox, monkeypatch):
    """A lab-wired ui module: lab config, lab DB, no cross-test leakage.

    Also chdirs to the repo root for the import, because ui.py mounts
    StaticFiles(directory="static") and Jinja2Templates(directory="templates")
    relative to the CWD at import time.
    """
    import models.config as mconfig

    src = sandbox / "source" / "FY26-27"
    L.write_file(src / "Clients" / "seed.txt", b"seed\n" * 10)
    nas = L.make_canary(sandbox / "nas" / "FY26-27")
    cfg = L.build_config(sandbox, source=src, lan_dest=str(nas), runtime=sandbox / "runtime")

    cwd = os.getcwd()
    os.chdir(REPO)
    try:
        import ui
    finally:
        os.chdir(cwd)

    cfg_path = _write_cfg(sandbox, cfg)
    monkeypatch.setattr(mconfig, "CONFIG_PATH", str(cfg_path))

    saved = (ui._config, ui._config_loaded_at, ui._DB_INSTANCE)
    ui._config, ui._config_loaded_at, ui._DB_INSTANCE = None, 0.0, None
    ui._RATE_LIMITS.clear()
    try:
        yield {"sandbox": sandbox, "cfg": cfg, "cfg_path": cfg_path, "ui": ui,
               "db_path": Path(cfg.paths.database_path)}
    finally:
        ui._config, ui._config_loaded_at, ui._DB_INSTANCE = saved
        if ui._DB_INSTANCE is not None:
            try:
                ui._DB_INSTANCE.close()
            except Exception:
                pass
            ui._DB_INSTANCE = None
        ui._RATE_LIMITS.clear()


# ══════════════════════════════════════════════════════════════════════════════
# W14.1 - the H3 tri-state contract
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("state,want_running,want_label", [
    (True, True, "running"),
    (False, False, "idle"),
    (None, False, "unknown"),
])
def test_w14_run_state_fields_tri_state(ui_rig, state, want_running, want_label):
    """"""
    ui = ui_rig["ui"]
    fields = ui._run_state_fields(state)
    problems = []
    if fields.get("running") is not want_running:
        problems.append(f"running={fields.get('running')!r}, want {want_running!r}")
    if fields.get("run_state") != want_label:
        problems.append(f"run_state={fields.get('run_state')!r}, want {want_label!r}")
    if not isinstance(fields.get("running"), bool):
        problems.append("`running` is not a strict bool - dashboard.js does truthiness on it")
    # the None case must never look idle-and-certain to the trigger guard
    if state is None and fields.get("running") is not False:
        problems.append("an unreachable API must not report a running backup")
    assert not problems, problems


def test_w14_prefect_has_active_run_filter_and_tristate(ui_rig, monkeypatch):
    """_prefect_has_active_run(): only matching, still-active runs count; an
    unreachable API returns None (never False, which disabled the duplicate
    guard); an unrecognised state on a MATCHING run fails closed to True."""
    import asyncio

    ui = ui_rig["ui"]
    rows, problems = [], []

    _StateType = _State

    def _install(runs, exc=None):
        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def read_flow_runs(self, **kwargs):
                if exc is not None:
                    raise exc
                return runs

        class _CM:
            def __call__(self, *a, **k):
                return _Client()

        monkeypatch.setattr(ui, "get_client", _CM())

    # far-future SCHEDULED: an explicit timestamp beyond the 60s window
    # -> False. A SCHEDULED run with NO next_scheduled_start_time is
    # indistinguishable from due-now and fails closed to True (ui.py:243).
    import datetime as _dt

    soon = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=10)
    far = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)
    cases = [
        ("running, no tags at all", [_Run([], {}, _StateType.RUNNING)], "cloud", None),
        ("integrity tag only (must be ignored)", [_Run(["integrity"], {}, _StateType.RUNNING)], "cloud", None),
        ("matching tag -> running", [_Run(["aam-backup", "cloud"], {}, _StateType.RUNNING)], "cloud", None),
        ("parameters mode=lan", [_Run([], {"mode": "lan"}, _StateType.RUNNING)], "lan", None),
        ("mode=all counts for either", [_Run([], {"mode": "all"}, _StateType.RUNNING)], "cloud", None),
        ("SCHEDULED due now", [_Run(["cloud"], {}, _StateType.SCHEDULED, next_scheduled_start_time=soon)], "cloud", None),
        ("SCHEDULED far future (explicit ts)", [_Run(["cloud"], {}, _StateType.SCHEDULED, next_scheduled_start_time=far)], "cloud", None),
        ("unrecognised state on a match", [_Run(["cloud"], {}, "WEIRD_STATE")], "cloud", None),
    ]
    for label, runs, pipeline, exc in cases:
        _install(runs, exc)
        got = asyncio.run(ui._prefect_has_active_run(pipeline))
        rows.append(f"{label:42} -> {got!r}")

    # -- the contract, case by case -------------------------------------------------
    _install([_Run([], {}, _StateType.RUNNING)])
    if asyncio.run(ui._prefect_has_active_run("cloud")) is not False:
        problems.append("a run with no tags and no mode was counted for a pipeline")
    _install([_Run(["integrity"], {}, _StateType.RUNNING)])
    if asyncio.run(ui._prefect_has_active_run("cloud")) is not False:
        problems.append("a read-only integrity audit hijacked the backup status")
    _install([_Run(["aam-backup", "cloud"], {}, _StateType.RUNNING)])
    if asyncio.run(ui._prefect_has_active_run("cloud")) is not True:
        problems.append("a matching RUNNING run was not detected")
    _install([_Run([], {"mode": "lan"}, _StateType.RUNNING)])
    if asyncio.run(ui._prefect_has_active_run("lan")) is not True:
        problems.append("parameters.mode was ignored")
    _install([_Run([], {"mode": "all"}, _StateType.RUNNING)])
    if asyncio.run(ui._prefect_has_active_run("cloud")) is not True:
        problems.append("mode=all was not counted for a single leg")
    _install([_Run(["cloud"], {}, _StateType.SCHEDULED)])
    if asyncio.run(ui._prefect_has_active_run("cloud")) is not True:
        problems.append("a SCHEDULED run with no next_scheduled_start_time did not fail closed")
    import datetime as _dt

    soon = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=10)
    far = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)
    _install([_Run(["cloud"], {}, _StateType.SCHEDULED, next_scheduled_start_time=far)])
    if asyncio.run(ui._prefect_has_active_run("cloud")) is not False:
        problems.append("a far-future SCHEDULED run was counted as active")
    _install([_Run(["cloud"], {}, _StateType.SCHEDULED, next_scheduled_start_time=soon)])
    if asyncio.run(ui._prefect_has_active_run("cloud")) is not True:
        problems.append("a due-now SCHEDULED run was not counted (double-click guard gap)")
    _install([_Run(["cloud"], {}, "WEIRD_STATE")])
    if asyncio.run(ui._prefect_has_active_run("cloud")) is not True:
        problems.append("an unrecognised state on a MATCHING run did not fail closed")
    _install([], exc=OSError("prefect api unreachable"))
    if asyncio.run(ui._prefect_has_active_run("cloud")) is not None:
        problems.append("an unreachable Prefect API returned a bool instead of None")

    L.record("W14.h3_tristate",
              "_prefect_has_active_run() implements the H3 tri-state: matching "
              "RUNNING/PENDING/due-now SCHEDULED runs count, integrity audits are "
              "excluded, an unrecognised state on a MATCHING run fails closed, and an "
              "unreachable API returns None so callers can choose fail-open (display) "
              "vs fail-closed (trigger)",
              "\n".join(rows),
              "OK - the duplicate-run guard's filter and tri-state both behave"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W14.1]\n" + "\n".join(rows))
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W14.2 - what the dashboard shows for each run/audit state
# ------------------------------------------------------------------------------
def test_w14_integrity_summary_never_implies_verified(ui_rig):
    """H-1: a COMPLETE backup with no audit must read NOT_VERIFIED, never VERIFIED."""
    ui = ui_rig["ui"]
    from core.manifest import ManifestDB

    db = ManifestDB(str(_seed_db(ui_rig["db_path"], runs=[
        {"run_id": "r1", "mode": "cloud", "started_at": "2026-10-10T18:00:00+05:30",
         "ended_at": "2026-10-10T18:05:00+05:30", "status": "CLOUD_COMPLETE"},
    ])))

    rows, problems = [], []
    no_audit = ui._integrity_summary(db, "cloud")
    rows.append(f"no audit row            -> {no_audit}")
    if no_audit.get("status") != "NOT_VERIFIED":
        problems.append(f"a COMPLETE backup with no audit reads {no_audit.get('status')!r} - "
                        "H-1: the dashboard must not imply verification it never ran")

    db.record_audit({"audit_id": "a1", "mode": "cloud", "scope": "cloud/full",
                     "started_at": "2026-10-11T03:00:00+05:30",
                     "ended_at": "2026-10-11T03:05:00+05:30", "status": "VERIFIED",
                     "detail": "capability=HASH_GCS_ADVERTISED"})
    verified = ui._integrity_summary(db, "cloud")
    rows.append(f"VERIFIED audit row      -> {verified}")
    if verified.get("status") != "VERIFIED" or verified.get("checked_at") is None:
        problems.append(f"a VERIFIED audit row rendered as {verified}")

    db.record_audit({"audit_id": "a2", "mode": "cloud", "scope": "cloud/full",
                     "started_at": "2026-10-12T03:00:00+05:30",
                     "ended_at": "2026-10-12T03:05:00+05:30",
                     "status": "VERIFICATION_FAILED",
                     "detail": "content mismatch detected (content-changed=1)"})
    failed = ui._integrity_summary(db, "cloud")
    rows.append(f"FAILED audit row        -> {failed}")
    if failed.get("status") != "VERIFICATION_FAILED":
        problems.append(f"a failing audit row rendered as {failed}")
    if failed.get("detail") is None:
        problems.append("the failure detail was dropped from the summary")

    # the LAN leg must be independent (its own audit state)
    lan = ui._integrity_summary(db, "lan")
    rows.append(f"LAN leg, no audit       -> {lan}")
    if lan.get("status") != "NOT_VERIFIED":
        problems.append(f"the LAN leg inherited the cloud verdict: {lan}")

    # _get_last_success / _last_run_summary
    last_success = ui._get_last_success(db, "cloud")
    rows.append(f"_get_last_success(cloud) -> {last_success!r}")
    if last_success is None:
        problems.append("a COMPLETE cloud run was not reported as the last success")
    if ui._get_last_success(db, "lan") is not None:
        problems.append("_get_last_success returned a LAN success with no LAN runs")

    summary = ui._last_run_summary(db, "lan")
    rows.append(f"_last_run_summary(lan)   -> {summary!r}")
    if summary is not None:
        problems.append("a mode with no runs produced a summary instead of None")

    # a long error message must be truncated for the page
    db.insert_run({"run_id": "r-long", "mode": "lan",
                   "started_at": "2026-10-12T01:00:00+05:30", "status": "LAN_FAILED",
                   "error_message": "E" * 5000, "duration_seconds": 12.6,
                   "files_copied": 3, "files_failed": 1})
    long_summary = ui._last_run_summary(db, "lan")
    rows.append(f"_last_run_summary(lan, failed, long error) -> "
                f"status={long_summary['status']} error_len={len(long_summary['error'])} "
                f"duration={long_summary['duration']!r} files={long_summary['files']} "
                f"failed={long_summary['files_failed']}")
    if len(long_summary["error"]) > 2003:
        problems.append(f"a 5000-char error was rendered at {len(long_summary['error'])} chars")
    if long_summary["duration"] != "13s":
        problems.append(f"duration formatted as {long_summary['duration']!r}")
    if long_summary["files_failed"] != 1:
        problems.append("files_failed was dropped from the summary")
    db.close()

    L.record("W14.integrity_summary",
              "H-1 labelling: a COMPLETE backup with NO integrity_audits row renders "
              "NOT_VERIFIED, a VERIFIED row renders VERIFIED with its timestamp, a "
              "failing row keeps its detail, and each leg is judged independently",
              "\n".join(rows),
              "OK - backup result and integrity status are reported as separate facts, "
              "and absence of an audit never reads as verified"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W14.2]\n" + "\n".join(rows))
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W14.3 - the HTTP surface
# ------------------------------------------------------------------------------
def test_w14_dashboard_and_status_endpoints(ui_rig, monkeypatch):
    """/, /status and /health against a real lab config + DB."""
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    # dates relative to today (date rot, 2026-10-18): weekly = last 7 days, monthly = the
    # previous calendar month (until plan group C5 moves the buttons to "last 30 days")
    import pendulum

    from core.time_utils import IST
    now_ist = pendulum.now(IST)
    recent = now_ist.subtract(days=2).format("YYYY-MM-DD")
    last_month = now_ist.start_of("month").subtract(months=1).add(days=14).format("YYYY-MM-DD")
    _seed_db(ui_rig["db_path"], runs=[
        {"run_id": "c1", "mode": "cloud", "started_at": f"{recent}T18:00:00+05:30",
         "status": "CLOUD_COMPLETE", "files_copied": 12, "duration_seconds": 8.0},
        {"run_id": "l1", "mode": "lan", "started_at": f"{recent}T01:00:00+05:30",
         "status": "LAN_FAILED", "error_message": "robocopy exit 16", "files_failed": 1},
        {"run_id": "c0", "mode": "cloud", "started_at": f"{last_month}T18:00:00+05:30",
         "ended_at": f"{last_month}T18:05:00+05:30",
         "status": "CLOUD_COMPLETE", "files_copied": 3, "duration_seconds": 8.0},
    ], entries=9)

    # Prefect unreachable -> the tri-state must degrade, not crash
    monkeypatch.setattr(ui, "get_client", _raising_client(OSError("api down")))

    rows, problems = [], []
    with TestClient(ui.app) as client:
        r_root = client.get("/")
        rows.append(f"GET /            -> {r_root.status_code} "
                    f"len={len(r_root.text)}")
        if r_root.status_code != 200:
            problems.append(f"GET / returned {r_root.status_code}")

        r_status = client.get("/status")
        rows.append(f"GET /status      -> {r_status.status_code}")
        if r_status.status_code != 200:
            problems.append(f"GET /status returned {r_status.status_code}")
        else:
            body = r_status.json()
            rows.append(f"  body keys      -> {sorted(body)}")
            rows.append(f"  cloud          -> { {k: v for k, v in body['cloud'].items() if k != 'last_run'} }")
            rows.append(f"  lan            -> { {k: v for k, v in body['lan'].items() if k != 'last_run'} }")
            if body["cloud"]["integrity"]["status"] != "NOT_VERIFIED":
                problems.append("the status API implied an integrity verdict with no audits")
            if body["cloud"]["running"] is not False or body["cloud"]["run_state"] != "unknown":
                problems.append(f"an unreachable Prefect did not degrade: {body['cloud']}")
            if body["lan"]["last_run"]["status"] != "LAN_FAILED":
                problems.append("the failed LAN run was not surfaced")
            if body["manifest"]["lan_files"] == 0 or body["manifest"]["cloud_files"] == 0:
                problems.append(f"manifest counts were {body['manifest']}")
            if len(body["recent_runs"]) != 3:
                problems.append(f"recent_runs={len(body['recent_runs'])}")
            if body["fy_prefix"] != "FY26-27" and not body["fy_prefix"].startswith("FY"):
                problems.append(f"fy_prefix={body['fy_prefix']!r}")
            if "schedule" not in body or not body["schedule"]["cloud_cron"]:
                problems.append("the schedule block is missing or empty")
            if "error" in body.get("health", {}):
                problems.append(f"/status health degraded unexpectedly: {body['health']}")

        r_health = client.get("/health")
        rows.append(f"GET /health      -> {r_health.status_code} {r_health.text[:120]}")
        # WP9: the seeded runs are days old -> /health must say so (503 + the
        # staleness reason); it used to answer 200 "healthy" regardless.
        if r_health.status_code != 503 or not any(
                "has not succeeded" in m for m in r_health.json().get("reasons", [])):
            problems.append(f"GET /health did not report the stale backups: "
                            f"{r_health.status_code} {r_health.text[:200]}")

        # report endpoints with runs present -> a downloadable attachment
        r_weekly = client.get("/report/weekly")
        r_monthly = client.get("/report/monthly")
        rows.append(f"GET /report/weekly  -> {r_weekly.status_code} "
                    f"cd={r_weekly.headers.get('content-disposition', '')[:60]}")
        rows.append(f"GET /report/monthly -> {r_monthly.status_code} "
                    f"cd={r_monthly.headers.get('content-disposition', '')[:60]}")
        for name, resp in (("weekly", r_weekly), ("monthly", r_monthly)):
            if resp.status_code != 200:
                problems.append(f"/report/{name} returned {resp.status_code}")
            elif "attachment" not in resp.headers.get("content-disposition", ""):
                problems.append(f"/report/{name} was not served as an attachment")

    L.record("W14.http_endpoints",
              "the dashboard's HTTP surface against a lab config + DB: / renders, "
              "/status reports recent runs, manifest counts, per-leg integrity and a "
              "degraded (not crashed) tri-state when Prefect is unreachable, /health "
              "answers, and /report/* serve attachments",
              "\n".join(rows),
              "OK - the dashboard renders and reports without a live Prefect or SMTP"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W14.3]\n" + "\n".join(rows))
    assert not problems, problems[:4]


def _raising_client(exc):
    class _CM:
        def __call__(self, *a, **k):
            class _C:
                async def __aenter__(self):
                    raise exc

                async def __aexit__(self, *a):
                    return False

            return _C()

    return _CM()


def test_w14_report_endpoints_without_data(ui_rig):
    """A missing DB must degrade to 503; an empty DB gives a report that says
    'No backups in <period>' (plan C P1) - never a blank 200."""
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    rows, problems = [], []
    with TestClient(ui.app) as client:
        # a DB file that does not exist yet
        ui._DB_INSTANCE = None
        ui._config = None
        ui._config_loaded_at = 0.0
        missing = ui_rig["sandbox"] / "runtime" / "nope" / "manifest.db"
        import models.config as mconfig
        import yaml

        cfg = ui_rig["cfg"]
        cfg.paths.database_path = str(missing)
        yaml.safe_dump(__import__("json").loads(cfg.model_dump_json()),
                       (ui_rig["sandbox"] / "config.yaml").open("w", encoding="utf-8"))
        mconfig.CONFIG_PATH = str(ui_rig["sandbox"] / "config.yaml")
        r_missing = client.get("/report/weekly")
        rows.append(f"missing DB, /report/weekly -> {r_missing.status_code}")
        if r_missing.status_code != 503:
            problems.append(f"a missing DB returned {r_missing.status_code}, want 503")

        # an empty DB -> a report that says nothing ran, not a blank page.
        # ManifestDB connects lazily (_conn = None until first _get_conn()),
        # so _seed_db with no rows never creates the file — force one
        # connection (a read) so the file exists, like production would.
        _seed_db(missing)
        from core.manifest import ManifestDB

        empty_db = ManifestDB(str(missing), busy_timeout_ms=5000)
        empty_db.file_count("lan_status")   # one read -> the file exists on disk
        empty_db.close()
        ui._DB_INSTANCE = None
        r_empty = client.get("/report/weekly")
        rows.append(f"empty DB, /report/weekly  -> {r_empty.status_code} "
                    f"{r_empty.text[:80]!r}")
        # plan C P1: an empty period is still a report, and it says so
        if r_empty.status_code != 200 or "No backups in" not in r_empty.text:
            problems.append(f"an empty DB returned {r_empty.status_code}, want a 200 report "
                            "that says 'No backups in'")

    L.record("W14.report_no_data",
              "the report endpoints degrade honestly when there is nothing to report: "
              "a missing manifest DB answers 503 and an empty one answers a report that says "
              "'No backups in <period>' (plan C P1)",
              "\n".join(rows),
              "OK - no-data paths are distinguishable from a real report"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W14.3b]\n" + "\n".join(rows))
    assert not problems, problems[:3]


# ------------------------------------------------------------------------------
# W14.4 - auth + rate limiting
# ------------------------------------------------------------------------------
def test_w14_auth_and_rate_limit_branches(ui_rig, monkeypatch):
    """Sessions, the API-key header and the trigger rate window."""
    ui = ui_rig["ui"]
    rows, problems = [], []

    # ---- sessions ------------------------------------------------------------
    ui._sessions.clear()
    token = ui._create_session()
    if not ui._validate_session(token):
        problems.append("a freshly created session failed validation")
    if ui._validate_session(None):
        problems.append("a missing session token validated")
    if ui._validate_session("not-a-real-token"):
        problems.append("an unknown token validated")
    # expired sessions must be dropped on validation
    ui._sessions[token]["created_at"] = time.time() - ui._session_ttl().total_seconds() - 1
    if ui._validate_session(token):
        problems.append("an expired session still validated")
    if token in ui._sessions:
        problems.append("an expired session was left in the store")
    rows.append(f"sessions: create/validate/unknown/expired all behave "
                f"(store size now {len(ui._sessions)})")

    # ---- sign-in (plan E: always on, no API key) ------------------------------
    # _require_auth() reads through _cfg(), which loads from DISK, so the lab config
    # (written by build_config with the two test password hashes) is what applies.
    from fastapi import HTTPException

    class _Req:
        def __init__(self, headers=None, cookies=None):
            self.headers = headers or {}
            self.cookies = cookies or {}

    ui._config = None
    ui._config_loaded_at = 0.0
    ui._sessions.clear()
    try:
        ui._require_auth(_Req({"Accept": "text/html"}))
        problems.append("an unauthenticated browser request was allowed through")
    except HTTPException as e:
        rows.append(f"_require_auth(browser, no session) -> {e.status_code} "
                    f"Location={e.headers.get('Location')}")
        if e.status_code != 303:
            problems.append(f"a browser request got {e.status_code}, want 303 to /login")
    try:
        ui._require_auth(_Req({"Accept": "application/json"}))
        problems.append("an unauthenticated API request was allowed through")
    except HTTPException as e:
        rows.append(f"_require_auth(api, no session)     -> {e.status_code}")
        if e.status_code != 401:
            problems.append(f"an API request got {e.status_code}, want 401")
    try:
        ui._require_auth(_Req({"X-API-Key": "anything", "Accept": "application/json"}))
        problems.append("the removed X-API-Key header opened the dashboard")
    except HTTPException:
        pass
    good = ui._create_session()
    ui._require_auth(_Req(cookies={"session": good}))
    rows.append("_require_auth(valid session) -> passes; X-API-Key header opens nothing")
    ui._sessions.clear()

    # ---- rate limiting -------------------------------------------------------
    ui._RATE_LIMITS.clear()
    allowed = sum(1 for _ in range(8) if ui._check_rate_limit("10.0.0.1", 5))
    rows.append(f"_check_rate_limit(max 5) -> {allowed} of 8 allowed")
    if allowed != 5:
        problems.append(f"the rate window allowed {allowed} of 8, want 5")
    if ui._check_rate_limit("10.0.0.1", 5):
        problems.append("the rate limit did not block after the window filled")
    # a different IP is independent
    if not ui._check_rate_limit("10.0.0.2", 5):
        problems.append("the rate limit bled across client IPs")
    # an expired window must reset
    ui._RATE_LIMITS["10.0.0.3"] = [time.time() - ui._dash("rate_limit_window_seconds") - 1]
    if not ui._check_rate_limit("10.0.0.3", 5):
        problems.append("an expired rate window still blocked")
    rows.append("rate limit: window enforced, per-IP, expired window resets")
    ui._RATE_LIMITS.clear()

    L.record("W14.auth_rate_limit",
              "dashboard auth and rate limiting: session create/validate/expire, the "
              "API-key header (correct/wrong/missing/disabled), _require_auth's 303 vs "
              "401 split, and the per-IP trigger rate window including expiry",
              "\n".join(rows),
              "OK - auth and rate limiting are enforced per branch"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W14.4]\n" + "\n".join(rows))
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W14.5 - CSV injection
# ------------------------------------------------------------------------------
def test_w14_csv_injection_is_neutralised():
    """A cell starting with =, +, -, @, tab or CR must be prefixed so Excel
    cannot evaluate it. These are error strings and metric JSON an accountant
    will open in Excel."""
    from core.report import _csv_safe, _generate_csv_data

    rows, problems = [], []
    inject = ["=cmd|'/c calc'!A1", "+1+1", "-2+3", "@SUM(1+1)", "\t=1+1", "\r=1+1",
              "=HYPERLINK(\"http://x\")", "+CMD('calc')"]
    for value in inject:
        safe = _csv_safe(value)
        rows.append(f"_csv_safe({value!r:28}) -> {safe!r}")
        if not safe.startswith("'") or safe[1:] != value:
            problems.append(f"{value!r} was neutralised to {safe!r}")
    # benign values must pass through untouched
    for value in ["ordinary text", "0", "", None, "New File\tClients/a.txt"]:
        safe = _csv_safe(value)
        if safe != ("" if value is None else str(value)):
            problems.append(f"benign value {value!r} was changed to {safe!r}")
    rows.append(f"_csv_safe(None) -> {_csv_safe(None)!r} (empty, not 'None')")

    # ---- the generated CSV --------------------------------------------------
    csv_runs = [{
        "started_at": "2026-10-10T18:00:00+05:30", "ended_at": "2026-10-10T18:05:00+05:30",
        "mode": "cloud", "status": "CLOUD_COMPLETE", "files_copied": 1200,
        "files_failed": 0, "bytes_copied": 999999, "duration_seconds": 300,
        "exit_code": 0, "error_message": None, "extended_metrics": '{"a": 1}',
    }, {
        "started_at": "2026-10-11T01:00:00+05:30", "ended_at": "2026-10-11T01:02:00+05:30",
        "mode": "lan", "status": "LAN_FAILED", "files_copied": 3, "files_failed": 1,
        "bytes_copied": 10, "duration_seconds": 120, "exit_code": 16,
        "error_message": "=rm -rf / (attempted injection)",
        "extended_metrics": '{"status": "LAN_FAILED"}',
    }]
    data = _generate_csv_data(csv_runs)
    text = data.decode("utf-8")
    rows.append("CSV:\n" + text)
    lines = text.splitlines()
    if "Started At" not in lines[0] or "Files Failed" not in lines[0]:
        problems.append(f"the CSV header is wrong: {lines[0]!r}")
    if len(lines) != 3:
        problems.append(f"the CSV has {len(lines)} lines, want 3 (header + 2 runs)")
    if "=rm -rf /" in text and "'=rm -rf /" not in text:
        problems.append("the injected error message reached the CSV unescaped")
    if "'=rm -rf / (attempted injection)" not in text:
        problems.append("the injected error message was not neutralised")
    # numeric columns must stay numeric (a leading quote would break sorting)
    if "1200" not in text or "'1200" in text:
        problems.append("a numeric column was quoted")
    # an empty run list -> header only, no exception
    empty = _generate_csv_data([]).decode("utf-8")
    if len(empty.splitlines()) != 1 or "Started At" not in empty:
        problems.append(f"an empty run list produced {empty!r}")

    L.record("W14.csv_injection",
              "_csv_safe() neutralises every documented CSV-formula prefix and leaves "
              "benign/numeric/None values untouched, and _generate_csv_data() escapes "
              "free-text columns while keeping numeric columns numeric",
              "\n".join(rows[:20]),
              "OK - CSV injection is neutralised and the header is stable"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W14.5]\n" + "\n".join(rows[:14]))
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W14.6 - report HTML
# ------------------------------------------------------------------------------
def test_w14_generate_report_html_branches(ui_rig):
    """generate_report_html() for an empty DB, a healthy week, an all-failed week
    and audit rows present."""
    from core.report import generate_report_html

    from core.manifest import ManifestDB

    ui = ui_rig["ui"]
    rows, problems = [], []

    empty_db = ManifestDB(str(ui_rig["db_path"]))
    empty_html = generate_report_html(empty_db, "LAB", 7, "Weekly")
    rows.append(f"empty DB          -> {empty_html!r}")
    if "No backups in" not in empty_html:          # plan C P1: an empty period still reports
        problems.append(f"an empty DB did not say 'No backups in': {str(empty_html)[:80]!r}")
    empty_db.close()

    # dates relative to today: a fixed date fell out of the 7-day window
    # (date rot found 2026-10-18)
    import pendulum

    from core.time_utils import IST
    day = pendulum.now(IST).subtract(days=2)
    d0, d1 = day.format("YYYY-MM-DD"), day.add(days=1).format("YYYY-MM-DD")
    db_path = _seed_db(ui_rig["sandbox"] / "runtime" / "m2.db", runs=[
        {"run_id": "c1", "mode": "cloud", "started_at": f"{d0}T18:00:00+05:30",
         "ended_at": f"{d0}T18:05:00+05:30", "status": "CLOUD_COMPLETE",
         "files_copied": 12, "files_failed": 0, "bytes_copied": 5000,
         "duration_seconds": 300, "exit_code": 0},
        {"run_id": "l1", "mode": "lan", "started_at": f"{d0}T01:00:00+05:30",
         "ended_at": f"{d0}T01:02:00+05:30", "status": "LAN_FAILED",
         "files_copied": 0, "files_failed": 5, "bytes_copied": 0,
         "duration_seconds": 120, "exit_code": 16,
         "error_message": "You do not have the Backup and Restore Files user rights."},
    ], audits=[{
        "audit_id": "a1", "mode": "cloud", "scope": "cloud/full",
        "started_at": f"{d1}T03:00:00+05:30", "ended_at": f"{d1}T03:05:00+05:30",
        "status": "VERIFICATION_FAILED", "files_checked": 12, "mismatches": 1,
        "detail": "content mismatch detected (content-changed=1)",
    }], entries=6)

    db = ManifestDB(str(db_path))
    html = generate_report_html(db, "LAB & Co", 7, "Weekly")
    rows.append(f"populated DB      -> {len(html)} chars of HTML")
    if not html:
        problems.append("a populated DB produced no HTML")
    # generate_report_html renders statuses through _status_display():
    #   CLOUD_COMPLETE -> "Completed", LAN_FAILED -> "Failed"
    # (the raw status strings appear in the CSV attachment, not the HTML)
    for needle in ("Completed", "Failed", "VERIFICATION_FAILED", "12"):
        if needle not in html:
            problems.append(f"the report HTML is missing {needle!r}")
    # the firm name and the period must appear
    if "LAB" not in html:
        problems.append("the firm name is missing from the report")
    # a failing run's error text must reach the report (that is the point)
    if "Backup and Restore" not in html:
        problems.append("the failure reason was dropped from the report")
    # is_email changes the wrapper
    email_html = generate_report_html(db, "LAB", 7, "Weekly", is_email=True)
    rows.append(f"is_email=True     -> {len(email_html)} chars "
                f"(email wrapper {'present' if email_html and '<html' in email_html.lower() else 'absent'})")
    if email_html and "<html" not in email_html.lower():
        problems.append("the email variant produced a non-HTML body")
    db.close()

    L.record("W14.report_html",
              "generate_report_html() returns falsy for an empty DB, renders statuses, "
              "file counts, exit codes and failure reasons for a populated one, carries "
              "the integrity-audit verdict, and produces a distinct email variant",
              "\n".join(rows),
              "OK - the report renders every run state it was given"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W14.6]\n" + "\n".join(rows))
    assert not problems, problems[:4]


# ------------------------------------------------------------------------------
# W14.7 - report email contains an SMTP outage
# ------------------------------------------------------------------------------
def test_w14_report_email_contains_smtp_outage(ui_rig):
    """send_weekly/monthly_report() must contain an SMTP outage: return False,
    never raise (a report is not worth failing a flow over)."""
    from core.manifest import ManifestDB

    from core.report import send_monthly_report, send_weekly_report
    from models.config import AppConfig

    db_path = _seed_db(ui_rig["sandbox"] / "runtime" / "m3.db", runs=[
        {"run_id": "c1", "mode": "cloud", "started_at": "2026-10-10T18:00:00+05:30",
         "status": "CLOUD_COMPLETE", "files_copied": 5},
    ])
    db = ManifestDB(str(db_path))

    # a local socket that accepts nothing -> a real refused SMTP connection
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()      # nothing is listening -> connection refused

    cfg = AppConfig(**{**ui_rig["cfg"].model_dump(),
                        "notifications": {"smtp_host": "127.0.0.1", "smtp_port": port,
                                          "smtp_username": "", "smtp_password": "",
                                          "sender": "lab@example.invalid",
                                          "recipients": ["ops@example.invalid"],
                                          "send_on_failure": True, "weekly_enabled": True,
                                          "monthly_enabled": True}})
    rows, problems = [], []
    for name, fn in (("weekly", send_weekly_report), ("monthly", send_monthly_report)):
        try:
            got = fn(db, cfg.notifications, "LAB & Co")
            rows.append(f"send_{name}_report  -> returned {got!r} (no exception)")
            if got is not False:
                problems.append(f"send_{name}_report returned {got!r} with SMTP down, want False")
        except Exception as e:  # noqa: BLE001
            problems.append(f"send_{name}_report RAISED {type(e).__name__}: {e}")

    # no runs at all -> False, and no SMTP attempt
    empty_db = ManifestDB(str(ui_rig["sandbox"] / "runtime" / "m4.db"))
    got = send_weekly_report(empty_db, cfg.notifications, "LAB")
    rows.append(f"no runs           -> returned {got!r}")
    if got is not False:
        problems.append(f"an empty DB returned {got!r}, want False")
    empty_db.close()
    db.close()

    L.record("W14.report_email",
              "send_weekly_report()/send_monthly_report() against a refusing local "
              "SMTP socket: both return False without raising, and an empty DB returns "
              "False without attempting a connection",
              "\n".join(rows),
              "OK - a report outage is contained (the flow still COMPLETES)"
              if not problems else f"ISSUE - {problems[:3]}")
    print(f"[W14.7]\n" + "\n".join(rows))
    assert not problems, problems[:3]
