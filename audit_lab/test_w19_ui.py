"""WAVE 19 — coverage-driven tests for ui.py (the dashboard).

The gap map after waves 17-18 ranked ui.py the worst-covered module (64%):
every trigger endpoint, the verify tri-state, login/logout/session handling,
the config-refresh path-change branch, the email-report endpoints and the
health helpers. This wave closes them.

Seams (lab-only, no production edits): models.config.CONFIG_PATH / load_config
and the module attributes ui._cfg, ui.get_db, ui._is_running,
ui._is_verify_running, ui.arun_deployment, ui._check_rate_limit,
ui._get_health are pointed at lab doubles; the real DB is a lab ManifestDB.
"""
from __future__ import annotations

import time
import types
from pathlib import Path

import pytest
from prefect.client.schemas.objects import StateType

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(300)


def _write_cfg(base: Path, cfg) -> Path:
    import json

    import yaml

    p = base / "config.yaml"
    yaml.safe_dump(json.loads(cfg.model_dump_json()), p.open("w", encoding="utf-8"),
                   sort_keys=False, allow_unicode=True)
    return p


def _fake_run(state_type, final=False, name="RUNNING", run_id="fr-abc123",
              tags=(), parameters=None, nxt=None):
    state = types.SimpleNamespace(type=state_type, is_final=lambda: final, name=name)
    return types.SimpleNamespace(id=run_id, state=state, state_type=state_type,
                                 tags=list(tags), parameters=parameters or {},
                                 name="aam-backup", next_scheduled_start_time=nxt)


@pytest.fixture()
def ui_rig(sandbox, monkeypatch):
    """Dashboard rig: lab config on disk, globals snapshotted and restored."""
    import ui
    import models.config as mconfig

    saved = (ui._config, ui._config_loaded_at, ui._DB_INSTANCE, dict(ui._sessions))
    cfg = L.build_config(sandbox, source=sandbox / "src", lan_dest=str(sandbox / "nas"),
                         runtime=sandbox / "runtime")
    cfg_path = _write_cfg(sandbox, cfg)
    monkeypatch.setattr(mconfig, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(mconfig, "load_config", lambda p=None: cfg)
    # prime the REAL _cfg() cache (do not patch the function itself - the
    # config-refresh test needs the genuine implementation)
    ui._config, ui._config_loaded_at = cfg, time.time()
    monkeypatch.setattr(ui, "_DB_INSTANCE", None)
    ui._sessions.clear()
    ui._RATE_LIMITS.clear()
    ui._config, ui._config_loaded_at = None, 0.0
    try:
        yield {"ui": ui, "cfg": cfg, "cfg_path": cfg_path, "sandbox": sandbox,
               "monkeypatch": monkeypatch}
    finally:
        ui._config, ui._config_loaded_at, ui._DB_INSTANCE, ui._sessions = (
            saved[0], saved[1], saved[2], saved[3])
        ui._RATE_LIMITS.clear()


def _seed_runs(db_path, runs):
    from core.manifest import ManifestDB

    db = ManifestDB(str(db_path), busy_timeout_ms=5000)
    for r in runs:
        db.insert_run(r)
    db.close()
    return db_path


# ── sessions / auth ───────────────────────────────────────────────────────────

def test_w19_session_lifecycle(ui_rig, monkeypatch):
    ui = ui_rig["ui"]
    token = ui._create_session()
    assert token in ui._sessions
    assert ui._validate_session(token) is True
    assert ui._validate_session(None) is False          # no cookie
    assert ui._validate_session("bogus") is False       # unknown token

    # expired session: rejected and evicted
    ui._sessions[token]["created_at"] = time.time() - ui._session_ttl().total_seconds() - 1
    assert ui._validate_session(token) is False
    assert token not in ui._sessions

    # _cleanup_expired_sessions runs on every new session
    dead = "dead-token"
    ui._sessions[dead] = {"created_at": time.time() - 90_000}
    ui._create_session()
    assert dead not in ui._sessions
    L.record("W19.session_lifecycle",
             "sessions expire by TTL and are evicted on creation; validation "
             "never accepts missing/unknown/expired tokens",
             "valid->True | None->False | bogus->False | expired->False+evicted",
             "OK - the session store behaves")


def test_w19_cfg_database_path_change_closes_db(ui_rig, monkeypatch):
    """The FY-rollover path: a changed database_path must close the cached DB."""
    ui = ui_rig["ui"]
    import models.config as mconfig

    closed = []
    fake_db = types.SimpleNamespace(close=lambda: closed.append(1))
    old = ui_rig["cfg"].model_copy(deep=True)
    old.paths.database_path = str(ui_rig["sandbox"] / "old" / "manifest.db")

    new_cfg = ui_rig["cfg"].model_copy(deep=True)
    new_cfg.paths.database_path = str(ui_rig["sandbox"] / "new" / "manifest.db")
    (ui_rig["sandbox"] / "newdir").mkdir(parents=True, exist_ok=True)
    new_path = _write_cfg(ui_rig["sandbox"] / "newdir", new_cfg)
    monkeypatch.setattr(mconfig, "CONFIG_PATH", str(new_path))
    monkeypatch.setattr(mconfig, "load_config", lambda p=None: new_cfg)

    ui._config, ui._config_loaded_at, ui._DB_INSTANCE = old, 0.0, fake_db
    got = ui._cfg()                      # the REAL _cfg (not the rig double)
    assert got.paths.database_path == new_cfg.paths.database_path
    assert closed == [1] and ui._DB_INSTANCE is None, "the stale DB must be closed"
    L.record("W19.cfg_path_change",
             "a config refresh with a changed database_path closes the cached "
             "DB connection (no thread queries a stale handle)",
             f"old={old.paths.database_path} new={new_cfg.paths.database_path} "
             "closed=True",
             "OK - the F13 critical section holds")


def test_w19_login_page(ui_rig):
    from fastapi.testclient import TestClient

    with TestClient(ui_rig["ui"].app) as client:
        r = client.get("/login")
        assert r.status_code == 200 and "Sign In" in r.text
        r = client.get("/login", params={"error": "<script>x</script>"})
        assert "&lt;script&gt;" in r.text      # escaped, never raw HTML


def test_w19_login_disabled_auth(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    ui_rig["cfg"].dashboard.auth_enabled = False
    with TestClient(ui.app) as client:
        r = client.post("/login", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "session" in r.cookies
        assert ui._validate_session(r.cookies["session"]) is True


def test_w19_login_enabled_auth(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    ui_rig["cfg"].dashboard.auth_enabled = True
    ui_rig["cfg"].dashboard.api_key = "s3cret"
    with TestClient(ui.app, base_url="https://labtest") as client:      # https -> secure flag
        r = client.post("/login", data={"api_key": "s3cret"}, follow_redirects=False)
        assert r.status_code == 303 and "session" in r.cookies
        assert "Secure" in r.headers.get("set-cookie", "")
        r = client.post("/login", data={"api_key": "wrong"}, follow_redirects=False)
        assert r.status_code == 303
        assert "Invalid+API+key" in r.headers["location"]


def test_w19_login_rate_limited(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    ui_rig["monkeypatch"].setattr(ui, "_check_rate_limit", lambda *a, **k: False)
    with TestClient(ui.app) as client:
        r = client.post("/login", data={"api_key": "x"})
        assert r.status_code == 429


def test_w19_logout(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    token = ui._create_session()
    with TestClient(ui.app) as client:
        client.cookies.set("session", token)
        r = client.get("/logout", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert token not in ui._sessions

def test_w19_require_auth_branches(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    ui_rig["cfg"].dashboard.auth_enabled = True
    ui_rig["cfg"].dashboard.api_key = "k3y"
    with TestClient(ui.app) as client:
        # browser request -> redirect to login
        r = client.get("/status", headers={"Accept": "text/html"},
                       follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        # API request -> 401 JSON
        r = client.get("/status")
        assert r.status_code == 401
        # valid API key header passes
        r = client.get("/status", headers={"X-API-Key": "k3y"})
        assert r.status_code != 401
        # valid session cookie passes
        token = ui._create_session()
        client.cookies.set("session", token)
        r = client.get("/status", headers={"X-API-Key": ""})
        assert r.status_code != 401


# ── dashboard / status ────────────────────────────────────────────────────────

def test_w19_dashboard_flash_and_typeresponse_fallback(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    ui_rig["cfg"].dashboard.auth_enabled = False
    with TestClient(ui.app) as client:
        # the old ?status= flash banner was dead code and is gone (4f): the page just renders
        r = client.get("/", params={"status": "triggered_lan"})
        assert r.status_code == 200

    # Starlette < 0.28 fallback: keyword call raises TypeError -> positional call
    calls = []

    def fake_tr(*a, **k):
        calls.append((a, k))
        if "request" in k:
            raise TypeError("old starlette")
        return "OLD-STARLETTE-RESPONSE"

    ui_rig["monkeypatch"].setattr(ui.templates, "TemplateResponse", fake_tr)
    with TestClient(ui.app) as client:
        r = client.get("/")
        assert r.text == "OLD-STARLETTE-RESPONSE" and len(calls) == 2


def _status_run(error_len=0):
    return {"run_id": "r1", "mode": "cloud", "status": "CLOUD_COMPLETE",
            "started_at": "2026-10-13T10:00:00", "ended_at": "2026-10-13T10:01:00",
            "files_copied": 5, "files_failed": 0, "duration_seconds": 61.0,
            "bytes_copied": 2048, "exit_code": 0, "extended_metrics": "{}",
            "error_message": "x" * error_len}


def test_w19_status_error_truncation(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    ui_rig["cfg"].dashboard.auth_enabled = False
    db_path = ui_rig["sandbox"] / "runtime" / "manifest.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _seed_runs(db_path, [_status_run(error_len=5000)])
    import models.config as mconfig
    cfg = ui_rig["cfg"]
    cfg.paths.database_path = str(db_path)
    with TestClient(ui.app) as client:
        r = client.get("/status")
        assert r.status_code == 200
        body = r.json()
        err = body["recent_runs"][0]["error"]
        assert len(err) == 2003 and err.endswith("...")   # 2000 chars + "..."


def test_w19_status_db_unavailable(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    ui_rig["cfg"].dashboard.auth_enabled = False

    class BadDB:
        def get_recent_runs(self, n):
            raise FileNotFoundError("no db")

    ui_rig["monkeypatch"].setattr(ui, "get_db", lambda: BadDB())
    with TestClient(ui.app) as client:
        r = client.get("/status")
        assert r.status_code == 503 and r.json()["error"] == "ManifestDB not found"


def test_w19_trigger_result_branches(ui_rig):
    ui = ui_rig["ui"]
    import json

    r = ui._trigger_result("Cloud", _fake_run(StateType.COMPLETED, final=True,
                                              name="COMPLETED"))
    assert r.status_code == 200 and json.loads(r.body)["status"] == "completed"

    r = ui._trigger_result("LAN", _fake_run(StateType.CANCELLED, final=True,
                                            name="CANCELLED"))
    assert r.status_code == 409 and json.loads(r.body)["status"] == "cancelled"

    r = ui._trigger_result("Cloud", _fake_run(StateType.FAILED, final=True,
                                              name="FAILED"))
    assert r.status_code == 500 and json.loads(r.body)["status"] == "failed"

    r = ui._trigger_result("Full", _fake_run(StateType.RUNNING, final=False,
                                             name="RUNNING"), noun="verification")
    assert r.status_code == 202 and json.loads(r.body)["flow_run_id"] == "fr-abc123"
    assert "verification" in json.loads(r.body)["detail"]


def test_w19_trigger_timeout(ui_rig):
    ui = ui_rig["ui"]
    req = types.SimpleNamespace(query_params={})
    assert ui._trigger_timeout(req) == 180
    for falsy in ("false", "0", "no", "off", "FALSE"):
        req = types.SimpleNamespace(query_params={"wait": falsy})
        assert ui._trigger_timeout(req) == 0
    req = types.SimpleNamespace(query_params={"wait": "true"})
    assert ui._trigger_timeout(req) == 180


# ── trigger endpoints ─────────────────────────────────────────────────────────

def _prep_triggers(ui_rig, *, leg_enabled=True, running=False, verify=None,
                   run=None, raise_exc=None):
    ui = ui_rig["ui"]
    ui_rig["cfg"].cloud.enabled = leg_enabled
    ui_rig["cfg"].lan.enabled = leg_enabled
    ui_rig["cfg"].dashboard.auth_enabled = False
    # these tests make many posts per minute from one IP: neutralise the
    # rate limiter here and exercise the 429 branch separately (per test)
    ui_rig["monkeypatch"].setattr(ui, "_check_rate_limit", lambda *a, **k: True)
    ui_rig["monkeypatch"].setattr(
        ui, "_is_running", lambda leg: _async(running))
    ui_rig["monkeypatch"].setattr(
        ui, "_is_verify_running", lambda: _async(verify))

    seen = {}

    async def fake_arun(name, timeout=None, parameters=None):
        seen["name"], seen["timeout"], seen["parameters"] = name, timeout, parameters
        if raise_exc is not None:
            raise raise_exc
        return run or _fake_run(StateType.RUNNING, final=False)

    ui_rig["monkeypatch"].setattr(ui, "arun_deployment", fake_arun)
    return seen


def _async(value):
    async def _coro():
        return value

    return _coro()


def test_w19_trigger_cloud_branches(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]

    # leg disabled
    _prep_triggers(ui_rig, leg_enabled=False)
    with TestClient(ui.app) as client:
        r = client.post("/trigger/cloud")
        assert r.status_code == 409 and r.json()["status"] == "disabled"

    # Prefect unreachable -> fail closed 503
    seen = _prep_triggers(ui_rig, running=None)
    with TestClient(ui.app) as client:
        r = client.post("/trigger/cloud")
        assert r.status_code == 503 and "cannot verify active runs" in r.json()["detail"]

    # already running -> 400
    _prep_triggers(ui_rig, running=True)
    with TestClient(ui.app) as client:
        r = client.post("/trigger/cloud")
        assert r.status_code == 400 and r.json()["status"] == "already_running"

    # success (202, running) + wait=false passes timeout=0
    seen = _prep_triggers(ui_rig)
    with TestClient(ui.app) as client:
        r = client.post("/trigger/cloud", params={"wait": "false"})
        assert r.status_code == 202 and seen["name"] == "aam-backup/backup-cloud"
        assert seen["timeout"] == 0

    # arun_deployment raises -> 500 (G15: no silent success)
    seen = _prep_triggers(ui_rig, raise_exc=RuntimeError("deployment missing"))
    with TestClient(ui.app) as client:
        r = client.post("/trigger/cloud")
        assert r.status_code == 500 and r.json()["status"] == "trigger_failed"

    # rate limit
    _prep_triggers(ui_rig)
    ui_rig["monkeypatch"].setattr(ui, "_check_rate_limit", lambda *a, **k: False)
    with TestClient(ui.app) as client:
        r = client.post("/trigger/cloud")
        assert r.status_code == 429


def test_w19_trigger_lan_branches(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]

    _prep_triggers(ui_rig, leg_enabled=False)
    with TestClient(ui.app) as client:
        r = client.post("/trigger/lan")
        assert r.status_code == 409

    _prep_triggers(ui_rig, running=None)
    with TestClient(ui.app) as client:
        assert client.post("/trigger/lan").status_code == 503

    _prep_triggers(ui_rig, running=True)
    with TestClient(ui.app) as client:
        assert client.post("/trigger/lan").status_code == 400

    seen = _prep_triggers(ui_rig)
    with TestClient(ui.app) as client:
        r = client.post("/trigger/lan")
        assert r.status_code == 202 and seen["name"] == "aam-backup/backup-lan"
        assert seen["timeout"] == 180

    seen = _prep_triggers(ui_rig, raise_exc=RuntimeError("boom"))
    with TestClient(ui.app) as client:
        assert client.post("/trigger/lan").status_code == 500


def test_w19_trigger_verify_branches(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    seen = _prep_triggers(ui_rig, verify=False)
    with TestClient(ui.app) as client:
        # invalid mode
        r = client.post("/trigger/verify/banana")
        assert r.status_code == 400 and "Invalid mode" in r.json()["detail"]

        # lan disabled
        ui_rig["cfg"].lan.enabled = False
        ui_rig["cfg"].cloud.enabled = True
        assert client.post("/trigger/verify/lan").status_code == 409
        # cloud disabled
        ui_rig["cfg"].lan.enabled = True
        ui_rig["cfg"].cloud.enabled = False
        assert client.post("/trigger/verify/cloud").status_code == 409
        # both disabled
        ui_rig["cfg"].lan.enabled = False
        ui_rig["cfg"].cloud.enabled = False
        assert client.post("/trigger/verify/all").status_code == 409

    # backup in progress -> refused (lock contention)
    seen = _prep_triggers(ui_rig, running=True, verify=False)
    with TestClient(ui.app) as client:
        r = client.post("/trigger/verify/all")
        assert r.status_code == 400 and "backup is already in progress" in r.json()["detail"]

    # an audit already running -> refused
    seen = _prep_triggers(ui_rig, running=False, verify=True)
    with TestClient(ui.app) as client:
        r = client.post("/trigger/verify/all")
        assert r.status_code == 400 and "verification audit is already" in r.json()["detail"]

    # Prefect unreachable at the verify check -> 503
    seen = _prep_triggers(ui_rig, running=False, verify=None)
    with TestClient(ui.app) as client:
        assert client.post("/trigger/verify/all").status_code == 503

    # success: parameters carry the mode, upper-case label
    seen = _prep_triggers(ui_rig, running=False, verify=False)
    with TestClient(ui.app) as client:
        r = client.post("/trigger/verify/cloud")
        assert r.status_code == 202
        assert seen["name"] == "integrity-audit/integrity-audit"
        assert seen["parameters"] == {"mode": "cloud"}

    # launch failure -> 500
    seen = _prep_triggers(ui_rig, running=False, verify=False,
                          raise_exc=RuntimeError("audit deployment missing"))
    with TestClient(ui.app) as client:
        r = client.post("/trigger/verify/lan")
        assert r.status_code == 500 and r.json()["status"] == "trigger_failed"

    # rate limit
    _prep_triggers(ui_rig, verify=False)
    ui_rig["monkeypatch"].setattr(ui, "_check_rate_limit", lambda *a, **k: False)
    with TestClient(ui.app) as client:
        assert client.post("/trigger/verify/all").status_code == 429


# ── the two Prefect-query helpers (direct, full branch coverage) ──────────────

class _FakePC:
    def __init__(self, runs):
        self.runs = runs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read_flow_runs(self, flow_run_filter=None, limit=200):
        return self.runs


def test_w19_is_verify_running_branches(ui_rig, monkeypatch):
    ui = ui_rig["ui"]
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run(StateType.RUNNING, tags=["integrity"])]))
    import asyncio
    assert asyncio.run(ui._is_verify_running()) is True

    # no integrity tag -> False
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run(StateType.RUNNING, tags=["aam-backup"], parameters={"mode": "cloud"})]))
    assert asyncio.run(ui._is_verify_running()) is False

    # SCHEDULED, no next time -> counts as active
    import datetime as _dt
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run(StateType.SCHEDULED, tags=["integrity"], nxt=None)]))
    assert asyncio.run(ui._is_verify_running()) is True

    # SCHEDULED far in the future -> not active
    future = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=5)
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run(StateType.SCHEDULED, tags=["integrity"], nxt=future)]))
    assert asyncio.run(ui._is_verify_running()) is False

    # SCHEDULED due within 60s -> active
    soon = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=30)
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run(StateType.SCHEDULED, tags=["integrity"], nxt=soon)]))
    assert asyncio.run(ui._is_verify_running()) is True

    # naive next time (no tzinfo) is treated as UTC
    naive = _dt.datetime.utcnow() + _dt.timedelta(seconds=10)
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run(StateType.SCHEDULED, tags=["integrity"], nxt=naive)]))
    assert asyncio.run(ui._is_verify_running()) is True

    # unparseable next time -> fail closed (True)
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run(StateType.SCHEDULED, tags=["integrity"], nxt="not-a-date")]))
    assert asyncio.run(ui._is_verify_running()) is True

    # unrecognised state -> fail closed (True)
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run("WEIRD", tags=["integrity"])]))
    assert asyncio.run(ui._is_verify_running()) is True

    # API unreachable -> None (tri-state; callers fail closed)
    def boom():
        raise RuntimeError("prefect down")

    monkeypatch.setattr(ui, "get_client", boom)
    assert asyncio.run(ui._is_verify_running()) is None


def test_w19_prefect_has_active_run_branches(ui_rig, monkeypatch):
    ui = ui_rig["ui"]
    import asyncio

    # a due SCHEDULED backup counts as active (double-click protection)
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run(StateType.SCHEDULED, tags=["aam-backup", "cloud"], nxt=None)]))
    assert asyncio.run(ui._prefect_has_active_run("cloud")) is True

    # matching by parameters.mode
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run(StateType.RUNNING, tags=["x"], parameters={"mode": "lan"})]))
    assert asyncio.run(ui._prefect_has_active_run("lan")) is True

    # integrity audits never hijack backup status
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run(StateType.RUNNING, tags=["integrity"])]))
    assert asyncio.run(ui._prefect_has_active_run("cloud")) is False

    # unparseable next_scheduled_start_time -> fail closed (True)
    monkeypatch.setattr(ui, "get_client", lambda: _FakePC(
        [_fake_run(StateType.SCHEDULED, tags=["aam-backup", "cloud"], nxt="junk")]))
    assert asyncio.run(ui._prefect_has_active_run("cloud")) is True

    # API unreachable -> None
    def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(ui, "get_client", boom)
    assert asyncio.run(ui._prefect_has_active_run("cloud")) is None


# ── health ────────────────────────────────────────────────────────────────────

def test_w19_get_health_branches(ui_rig):
    ui = ui_rig["ui"]
    import asyncio

    ui_rig["cfg"].paths.source_drive = str(ui_rig["sandbox"])
    h = asyncio.run(ui._get_health())
    assert "source_free_gb" in h and h["source_exists"] is True

    ui_rig["cfg"].paths.source_drive = str(ui_rig["sandbox"] / "does-not-exist")
    h = asyncio.run(ui._get_health())
    assert h == {"error": "unavailable"}


def test_w19_health_endpoint(ui_rig):
    """M3 3.5 (plan WP9): /health is truthful. It used to answer 200/healthy
    unconditionally - even when its own check crashed - so monitoring could
    never see an outage (11 days, Aug 22 - Sep 1)."""
    from fastapi.testclient import TestClient

    import pendulum

    from core.manifest import ManifestDB
    from core.time_utils import IST

    ui = ui_rig["ui"]
    ui_rig["cfg"].dashboard.auth_enabled = False
    ui_rig["monkeypatch"].setattr(ui, "_is_running", lambda leg: _async(False))
    # 1. nothing has ever succeeded -> 503 with the reasons, no paths/secrets
    with TestClient(ui.app) as client:
        r = client.get("/health")
        assert r.status_code == 503 and r.json()["status"] == "attention"
        assert any("no successful backup" in m for m in r.json()["reasons"])
        assert "source_drive" not in r.json()

    # 2. fresh successful runs on every enabled leg -> 200 healthy
    now = pendulum.now(IST).isoformat()
    db = ManifestDB(str(ui_rig["cfg"].paths.database_path))
    for leg, status in (("cloud", "CLOUD_COMPLETE"), ("lan", "LAN_COMPLETE")):
        db.insert_run({"run_id": f"h-{leg}", "mode": leg, "started_at": now, "ended_at": now,
                       "status": status})
    db.close()
    with TestClient(ui.app) as client:
        r = client.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "healthy"

    # 3. health that cannot be evaluated is NOT healthy
    def _boom():
        raise RuntimeError("cfg exploded")

    ui_rig["monkeypatch"].setattr(ui, "_cfg", _boom)
    with TestClient(ui.app) as client:
        r = client.get("/health")
        assert r.status_code == 503 and r.json()["status"] == "unknown"


# ── email report endpoints ────────────────────────────────────────────────────

def test_w19_email_report_endpoints(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    ui_rig["cfg"].dashboard.auth_enabled = False
    ui_rig["cfg"].notifications.recipients = ["ops@example.com"]
    from core import report as rep

    for path, sender, period, days in (
            ("/trigger/report/weekly/email", "send_weekly_report", "Weekly", 7),
            ("/trigger/report/monthly/email", "send_monthly_report", "Monthly", 30)):
        gen_args, send_args = [], []
        outcome = {"mode": "ok"}

        def fake_gen(db, firm, d, p, is_email=False, legs=None):
            gen_args.append((d, p))
            return "" if outcome["mode"] == "no_data" else "<html>report</html>"

        def fake_send(db, cfg_, firm, body_html=None, legs=None):
            if outcome["mode"] == "raise":
                raise OSError("smtp unreachable")
            return outcome["mode"] == "ok"

        ui_rig["monkeypatch"].setattr(rep, "generate_report_html", fake_gen)
        ui_rig["monkeypatch"].setattr(rep, sender, fake_send)
        ui_rig["monkeypatch"].setattr(ui, "_check_rate_limit", lambda *a, **k: True)

        with TestClient(ui.app) as client:
            # success -> 200 with the recipient list
            r = client.post(path)
            assert r.status_code == 200 and r.json()["status"] == "sent"
            assert "ops@example.com" in r.json()["detail"]
            assert gen_args == [(days, period)]

            # send returns False -> 500 (config/SMTP problem, not "sent")
            outcome["mode"] = "false"
            r = client.post(path)
            assert r.status_code == 500 and r.json()["status"] == "failed"

            # send raises -> 500 with the error surfaced
            outcome["mode"] = "raise"
            r = client.post(path)
            assert r.status_code == 500 and "smtp unreachable" in r.json()["detail"]

            # no data -> 404 (never an empty 200 that looks like a report)
            outcome["mode"] = "no_data"
            r = client.post(path)
            assert r.status_code == 404 and r.json()["status"] == "no_data"

            # rate limited
            outcome["mode"] = "ok"
            ui_rig["monkeypatch"].setattr(ui, "_check_rate_limit", lambda *a, **k: False)
            r = client.post(path)
            assert r.status_code == 429
            # (the next iteration re-patches the limiter to True; the rig's
            #  teardown restores everything else)


def test_w19_report_download_rate_limit(ui_rig):
    from fastapi.testclient import TestClient

    ui = ui_rig["ui"]
    ui_rig["cfg"].dashboard.auth_enabled = False
    ui_rig["monkeypatch"].setattr(ui, "_check_rate_limit", lambda *a, **k: False)
    with TestClient(ui.app) as client:
        assert client.get("/report/weekly").status_code == 429
        assert client.get("/report/monthly").status_code == 429


# ── helpers ───────────────────────────────────────────────────────────────────

def test_w19_integrity_summary_branches(ui_rig):
    ui = ui_rig["ui"]

    class RaisingDB:
        def latest_audit(self, mode):
            raise RuntimeError("db gone")

    assert ui._integrity_summary(RaisingDB(), "cloud") == {
        "status": "NOT_VERIFIED", "checked_at": None, "detail": None}

    class WeirdDB:
        def latest_audit(self, mode):
            return "not-a-dict"          # defensive: non-dict audit

    assert ui._integrity_summary(WeirdDB(), "lan")["status"] == "NOT_VERIFIED"

    class OkDB:
        def latest_audit(self, mode):
            return {"status": "VERIFICATION_FAILED", "ended_at": "2026-10-13T09:00:00",
                    "detail": "content mismatch (content-changed=1)"}

    s = ui._integrity_summary(OkDB(), "cloud")
    assert s["status"] == "VERIFICATION_FAILED" and s["checked_at"].endswith("09:00:00")
    assert s["detail"] == "content mismatch (content-changed=1)"
    L.record("W19.integrity_summary",
             "_integrity_summary never implies VERIFIED: a raising/garbage audit "
             "reads NOT_VERIFIED, a real audit is reported verbatim",
             "raising->NOT_VERIFIED | non-dict->NOT_VERIFIED | real->verbatim",
             "OK - H-1's honesty boundary holds")
