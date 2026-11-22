"""Adversarial scenarios against the dashboard HTTP surface (ui.py).

Each test attacks one thing: an unsigned caller, a forged / expired / superseded session, a
cross-site POST, a wrong verb, a hostile body or parameter, a double click, a run already in
progress, a brute-force sign-in. The invariant: a hostile request gets a 4xx (or the normal answer
for a valid one), never a 500 that leaks a traceback or a secret, and no state changes without the
right sign-in.

Prefect, the mail server and the real backup are faked. The database is a temporary one and the
settings writes go to a copy of config.yaml, never to the sandbox file itself.
"""

import json
import re
import shutil
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pendulum
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import ui
from core import admin_auth, app_restart, approvals as ap, config_edit
from core.manifest import ManifestDB
from models.config import CONFIG_PATH, load_config
from test_support import ui_login

SIGNED = {"session": ui_login.SESSION_TOKEN}
ADMIN = {**SIGNED, "admin_session": ui_login.ADMIN_TOKEN}
JSON_ACCEPT = {"Accept": "application/json"}
HTML_ACCEPT = {"Accept": "text/html"}
JSON_BODY = {"Content-Type": "application/json"}
GUARDS = [{"guard": "G3_MASS_DELETE", "reason": "This backup would delete 600 of 1,000",
           "metrics": {"planned_deletes": 600, "delete_percent": 60.0}}]
# routes that are open on purpose (sign-in page, monitoring, sign-out); everything else needs a session
OPEN_ROUTES = {"/login", "/health", "/logout", "/api/admin/logout"}


def _client(cookies: dict | None = None) -> TestClient:
    # the non-empty default header stops tests/conftest.py from signing this client in by itself
    return TestClient(ui.app, raise_server_exceptions=False, follow_redirects=False,
                       headers=JSON_ACCEPT, cookies=dict(cookies or {}))


def _fresh() -> None:
    """One rate-limit budget per attack step: a loop of hostile calls must not trip the limiter
    by accident, the limiter itself is tested on its own below."""
    ui._RATE_LIMITS.clear()


def _open(db, leg: str = "lan", fy: str = "FY26-27") -> str:
    return db.open_approval_request(leg=leg, blocked_run_id="run-b", fy=fy, guards=GUARDS,
                                    phrase=ap.request_phrase(GUARDS),
                                    created_at=pendulum.now("UTC").isoformat())


def _status(db, aid: str) -> str:
    return db.get_approval(aid)["status"]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    _fresh()
    db = ManifestDB(str(tmp_path / "manifest.db"))
    cfg_file = tmp_path / "config.yaml"
    shutil.copy(CONFIG_PATH, cfg_file)                 # every settings write lands in this copy
    cfg = load_config(str(cfg_file))
    cfg.paths.database_path = str(tmp_path / "manifest.db")
    cfg.cloud.enabled = True
    cfg.lan.enabled = True

    calls: list[dict] = []

    async def fake_arun(name, parameters=None, timeout=None):
        calls.append({"name": name, "parameters": parameters, "timeout": timeout})
        return SimpleNamespace(id="flow-run-1", state=None)

    get_db = MagicMock(return_value=db)
    is_running = AsyncMock(return_value=False)
    verify_running = AsyncMock(return_value=False)
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "_settings_config_path", lambda: str(cfg_file))
    monkeypatch.setattr(ui, "get_db", get_db)
    monkeypatch.setattr(ui, "_is_running", is_running)
    monkeypatch.setattr(ui, "_is_verify_running", verify_running)
    monkeypatch.setattr(ui, "arun_deployment", fake_arun)
    monkeypatch.setattr(ui, "_active_fy_list", lambda: ["FY26-27"])
    monkeypatch.setattr(ui, "_configured_fy", lambda: "FY26-27")
    rig = SimpleNamespace(db=db, cfg=cfg, cfg_file=cfg_file, calls=calls, get_db=get_db,
                          is_running=is_running, verify_running=verify_running)
    yield rig
    db.close()
    _fresh()


# ── 1-2. Nothing is open to a stranger ───────────────────────────────────────

def _route_cases() -> list[tuple[str, str]]:
    cases = []
    for route in ui.app.routes:
        if not isinstance(route, APIRoute) or route.path in OPEN_ROUTES:
            continue
        for method in sorted(route.methods - {"HEAD"}):
            cases.append((method, re.sub(r"\{[^}]+\}", "probe", route.path)))
    return cases


_ROUTE_CASES = _route_cases()


@pytest.mark.parametrize("method,path", _ROUTE_CASES, ids=[f"{m} {p}" for m, p in _ROUTE_CASES])
def test_every_route_refuses_a_caller_without_a_session(rig, method, path):
    client = _client()
    for accept in ("application/json", "text/html"):
        body = None if method == "GET" else b"{}"
        r = client.request(method, path, headers={"Accept": accept}, content=body)
        assert r.status_code in (401, 303), (method, path, accept, r.status_code)
        if r.status_code == 303:
            assert r.headers["location"] == "/login"
        assert "Traceback" not in r.text
    assert rig.get_db.call_count == 0, "a stranger reached the database"
    assert rig.calls == [], "a stranger started a run"


def test_only_sign_in_health_and_sign_out_are_open_and_leak_no_secrets(rig):
    client = _client()
    page = client.get("/login")
    assert page.status_code == 200
    health = client.get("/health")
    assert health.status_code in (200, 503)
    for body in (page.text, health.text):
        assert ui_login.LOGIN_HASH not in body and ui_login.ADMIN_HASH not in body
        assert "pbkdf2_sha256" not in body
    assert client.get("/logout").status_code == 303
    assert client.post("/api/admin/logout").status_code == 200
    # a stranger's sign-out and sign-out of Advanced end nothing that belongs to someone else
    assert ui_login.SESSION_TOKEN in ui._sessions


# ── 3-6. Sessions: garbage, expired, superseded, Advanced cookie alone ───────

GARBAGE_SESSIONS = ["", "null", "undefined", "../../../etc/passwd", "%00", "%C3%A9",
                    "t" * 63, "t" * 65, ui_login.SESSION_TOKEN[:-1] + "u",
                    ui_login.ADMIN_TOKEN, "x" * 100_000]


@pytest.mark.parametrize("value", GARBAGE_SESSIONS, ids=[v[:20] or "empty" for v in GARBAGE_SESSIONS])
def test_garbage_or_forged_session_cookie_opens_no_write(rig, value):
    aid = _open(rig.db)
    client = _client()
    headers = {**JSON_ACCEPT, "Cookie": f"session={value}"}
    for method, path, kw in [
        ("POST", "/trigger/cloud?wait=false", {}),
        ("POST", f"/approvals/{aid}/approve?wait=false", {"json": {"phrase": "APPROVE"}}),
        ("POST", "/api/settings", {"json": {"changes": {"firm_name": "Hacked"}}}),
        ("GET", "/status", {}),
    ]:
        _fresh()
        r = client.request(method, path, headers=headers, **kw)
        assert r.status_code == 401, (value[:20], path, r.status_code)
    assert rig.calls == [] and _status(rig.db, aid) == ap.PENDING


def test_expired_or_superseded_session_is_refused_and_forgotten(rig):
    client = _client(SIGNED)
    aid = _open(rig.db)
    ui._sessions[ui_login.SESSION_TOKEN]["created_at"] = time.time() - 25 * 3600     # past the 24 h TTL
    assert client.post("/trigger/cloud?wait=false").status_code == 401
    assert ui_login.SESSION_TOKEN not in ui._sessions
    # a session opened before the dashboard password was changed is dead too (plan E2)
    ui._sessions[ui_login.SESSION_TOKEN] = {"created_at": time.time(),
                                            "fp": admin_auth.fingerprint(ui_login.LOGIN_HASH)}
    rig.cfg.dashboard.login_password_hash = ui_login.ADMIN_HASH
    _fresh()
    assert client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"}).status_code == 401
    assert _status(rig.db, aid) == ap.PENDING and rig.calls == []


def test_advanced_cookie_alone_and_dashboard_sign_in_alone_cannot_reach_advanced(rig, monkeypatch):
    setter = MagicMock()
    monkeypatch.setattr(config_edit, "set_password_hash", setter)
    body = {"current": ui_login.ADMIN_PASSWORD, "new": "Brand-new-pass-1", "confirm": "Brand-new-pass-1"}
    # the Advanced cookie without the dashboard sign-in: nothing opens
    only_admin = _client({"admin_session": ui_login.ADMIN_TOKEN})
    assert only_admin.get("/api/admin/status").status_code == 401
    assert only_admin.get("/api/settings/browse?path=C:\\").status_code == 401
    assert only_admin.post("/api/admin/password", json=body).status_code == 401
    # the dashboard sign-in without the Advanced one: refused with 403
    signed = _client(SIGNED)
    assert signed.get("/api/settings/browse?path=C:\\").status_code == 403
    assert signed.post("/api/settings/check-source", json={"parent": "C:\\"}).status_code == 403
    _fresh()
    assert signed.post("/api/admin/password", json=body).status_code == 403
    _fresh()
    assert signed.post("/api/admin/dashboard-password", json=body).status_code == 403
    setter.assert_not_called()


# ── 7-9. Brute force: sign-in and Advanced sign-in lock out ──────────────────

def test_sign_in_locks_out_an_address_and_forged_forwarding_headers_do_not_reset_it(rig, monkeypatch):
    monkeypatch.setattr(admin_auth, "verify_login_password",
                        lambda pw, d: pw == ui_login.LOGIN_PASSWORD)
    client = _client()
    for i in range(10):
        r = client.post("/login", data={"password": f"guess-{i}"},
                        headers={"X-Forwarded-For": f"10.0.0.{i}", "X-Real-IP": f"10.1.0.{i}"})
        assert r.status_code == 303 and r.headers["location"].startswith("/login?error=")
    # the 11th try is refused even with the right password, and forged addresses change nothing
    locked = client.post("/login", data={"password": ui_login.LOGIN_PASSWORD},
                         headers={"X-Forwarded-For": "203.0.113.9"})
    assert locked.status_code == 429 and "session" not in locked.cookies
    assert "Traceback" not in locked.text


def test_advanced_sign_in_never_opens_anonymously_and_locks_out_when_signed_in(rig, monkeypatch):
    monkeypatch.setattr(admin_auth, "verify_admin_password",
                        lambda pw, d: pw == ui_login.ADMIN_PASSWORD)
    anon = _client()
    before = set(ui._admin_sessions)          # the conftest's own Advanced token is already there
    for _ in range(30):                       # no dashboard sign-in: refused before any password check
        assert anon.post("/api/admin/login", json={"password": ui_login.ADMIN_PASSWORD}).status_code == 401
    assert set(ui._admin_sessions) == before
    signed = _client(SIGNED)
    codes = [signed.post("/api/admin/login", json={"password": f"guess-{i}"}).status_code
             for i in range(12)]
    assert 200 not in codes and codes[-1] == 429
    assert set(ui._admin_sessions) == before


# ── 10-12. Triggers: refuse while busy, never queue, one at a time, rate-limited ─

@pytest.mark.parametrize("path,running", [
    ("/trigger/cloud?wait=false", {"cloud"}),
    ("/trigger/lan?wait=false", {"lan"}),
    ("/trigger/cloud?wait=false", {"rollover"}),          # the FY rollover moves the folders
    ("/trigger/lan?wait=false&anyway=1", {"cloud"}),      # another backup holds the one slot
])
def test_trigger_refuses_while_a_backup_or_rollover_runs_and_never_queues(rig, path, running):
    rig.is_running.side_effect = lambda leg: leg in running
    r = _client(SIGNED).post(path)
    assert r.status_code == 400 and r.json()["status"] == "already_running"
    assert rig.calls == []


def test_verify_refuses_while_a_safety_check_or_backup_runs_even_with_anyway(rig):
    client = _client(SIGNED)
    rig.verify_running.return_value = True
    assert client.post("/trigger/verify/all?anyway=1").status_code == 400
    rig.verify_running.return_value = False
    rig.is_running.side_effect = lambda leg: leg == "lan"
    assert client.post("/trigger/verify/all?anyway=1").status_code == 400
    assert rig.calls == []


@pytest.mark.parametrize("path", ["/trigger/cloud?wait=false", "/trigger/lan?wait=false",
                                  "/trigger/verify/all"])
def test_trigger_answers_503_and_starts_nothing_while_prefect_is_silent(rig, path):
    rig.is_running.return_value = None          # the scheduler did not answer: fail closed
    rig.verify_running.return_value = None
    r = _client(SIGNED).post(path)
    assert r.status_code == 503 and rig.calls == []


def test_double_click_starts_one_backup_and_the_sixth_start_is_rate_limited(rig):
    client = _client(SIGNED)
    assert client.post("/trigger/cloud?wait=false").status_code == 202
    rig.is_running.return_value = True                     # the first run is queued in Prefect now
    assert client.post("/trigger/cloud?wait=false").status_code == 400
    assert len(rig.calls) == 1
    # a fresh budget: the trigger limit (5 per window) stops a flood of starts
    rig.is_running.return_value = False
    ui._RATE_LIMITS.clear()
    codes = [client.post("/trigger/lan?wait=false").status_code for _ in range(6)]
    assert codes == [202] * 5 + [429]
    assert len(rig.calls) == 6


# ── 13-15. Query and path parameters ─────────────────────────────────────────

@pytest.mark.parametrize("fy", ["../../etc", "FY99-00' OR '1'='1", "%00", "FY26-27%0d%0aX-Evil:1",
                                "A" * 5000])
def test_fy_parameter_must_name_an_active_folder_and_is_never_passed_through(rig, fy):
    r = _client(SIGNED).post(f"/trigger/cloud?wait=false&fy={fy}")
    assert r.status_code == 409 and rig.calls == []


def test_a_valid_fy_is_canonicalised_before_it_reaches_the_deployment(rig):
    r = _client(SIGNED).post("/trigger/lan?wait=false&fy=fy26-27")
    assert r.status_code == 202
    assert rig.calls == [{"name": "aam-backup/backup-lan", "parameters": {"fy": "FY26-27"},
                          "timeout": 0}]


@pytest.mark.parametrize("wait,expected", [("-1", None), ("99999999999999999999", None),
                                           ("nan", None), ("%00", None), ("true;DROP", None),
                                           ("0", 0), ("false", 0)])
def test_garbage_wait_parameter_gets_the_configured_timeout_not_the_raw_value(rig, wait, expected):
    r = _client(SIGNED).post(f"/trigger/cloud?wait={wait}")
    assert r.status_code == 202
    timeout = rig.calls[0]["timeout"]
    assert timeout == (expected if expected is not None else rig.cfg.dashboard.trigger_wait_seconds)


@pytest.mark.parametrize("mode", ["..%2F..%2Fx", "lan;drop", "%00", "all%20", "x" * 5000, "%E2%98%83"])
def test_verify_mode_path_parameter_accepts_only_three_words(rig, mode):
    r = _client(SIGNED).post(f"/trigger/verify/{mode}")
    assert r.status_code in (400, 404) and rig.calls == []


# ── 16. The FY rollover run ──────────────────────────────────────────────────

def test_rollover_run_refuses_while_a_rollover_runs_or_prefect_is_silent(rig, monkeypatch):
    monkeypatch.setattr(ui.fy_rollover, "configured_fy", lambda *a: "FY26-27")
    monkeypatch.setattr(ui.fy_rollover, "manual_run_state", lambda *a: {"can_run": True, "message": ""})
    monkeypatch.setattr(ui, "_finished_journal", lambda *a: False)
    monkeypatch.setattr(ui, "_open_rollover_journal", lambda: None)
    client = _client(SIGNED)
    rig.is_running.side_effect = lambda leg: leg == "rollover"
    assert client.post("/api/rollover/run").status_code == 409
    rig.is_running.side_effect = lambda leg: None
    assert client.post("/api/rollover/run").status_code == 503
    assert rig.calls == []
    # the same mocks let an idle system through, so the refusals above are the guard at work
    rig.is_running.side_effect = None
    rig.is_running.return_value = False
    assert client.post("/api/rollover/run").status_code == 202
    assert len(rig.calls) == 1


# ── 17-22. Approvals: unknown ids, single use, double clicks, bad phrases ───

@pytest.mark.parametrize("aid", ["nope", "%00", "x' OR '1'='1", "..%2F..%2Fsecret", "A" * 5000,
                                 "%E2%98%83"])
def test_unknown_approval_ids_are_404_for_every_action_and_change_nothing(rig, aid):
    real = _open(rig.db)
    client = _client(SIGNED)
    for action, kw in [("approve", {"json": {"phrase": "APPROVE"}}), ("run", {}), ("dismiss", {})]:
        _fresh()
        r = client.post(f"/approvals/{aid}/{action}?wait=false", **kw)
        assert r.status_code == 404, (action, r.status_code)
    assert _status(rig.db, real) == ap.PENDING and rig.calls == []


def test_an_approval_runs_once_a_double_approve_is_refused_and_a_used_one_never_runs(rig):
    aid = _open(rig.db)
    client = _client(SIGNED)
    _fresh()
    assert client.post(f"/approvals/{aid}/run?wait=false").status_code == 409   # not approved yet
    _fresh()
    assert client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"}).status_code == 202
    assert len(rig.calls) == 1
    rig.is_running.return_value = True                                        # the approved run is queued
    _fresh()
    assert client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"}).status_code == 409
    _fresh()
    assert client.post(f"/approvals/{aid}/run?wait=false").status_code == 409
    rig.is_running.return_value = False
    assert rig.db.mark_approval_used(aid, run_id="run-x", used_at=pendulum.now("UTC").isoformat())
    _fresh()
    assert client.post(f"/approvals/{aid}/run?wait=false").status_code == 409  # consumed: never again
    assert len(rig.calls) == 1


def test_two_simultaneous_approve_clicks_start_exactly_one_run(rig):
    aid = _open(rig.db)
    barrier = threading.Barrier(2)
    codes, errors = [], []

    def click():
        try:
            client = _client(SIGNED)
            barrier.wait(timeout=10)
            codes.append(client.post(f"/approvals/{aid}/approve?wait=false",
                                     json={"phrase": "APPROVE"}).status_code)
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=click) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    assert sorted(codes) == [202, 409]
    assert len(rig.calls) == 1


@pytest.mark.parametrize("content", [
    b"", b"{", b"null", b"[]", b'{"phrase": "approve approve"}', b'{"phrase": ""}',
    b'{"phrase": "APPROVE\\u0000"}', b'{"phrase": "\\ud800"}', b"\xff\xfe\x00",
    b"[" * 100_000, b'{"phrase":' + b"[" * 100_000,
    json.dumps({"phrase": "A" * 3_000_000}).encode(),
], ids=["empty", "truncated", "null", "array", "extra-word", "blank", "nul", "lone-surrogate",
        "not-utf8", "deep-array", "deep-nested", "three-megabytes"])
def test_a_wrong_or_malformed_confirmation_is_refused_and_changes_nothing(rig, content):
    aid = _open(rig.db)
    _fresh()
    r = _client(SIGNED).post(f"/approvals/{aid}/approve?wait=false", content=content, headers=JSON_BODY)
    assert r.status_code == 400, r.status_code
    assert _status(rig.db, aid) == ap.PENDING and rig.calls == []


@pytest.mark.xfail(strict=False, reason="SUSPECTED BUG: ui.py:923 passes a non-text phrase on; "
                   "core/approvals.py:99 calls .split() on it, so a number, boolean, list or object "
                   "raises AttributeError and answers HTTP 500")
def test_a_non_text_confirmation_is_refused_with_a_4xx(rig):
    aid = _open(rig.db)
    client = _client(SIGNED)
    for phrase in (5, True, ["APPROVE"], {"phrase": "APPROVE"}):
        _fresh()
        r = client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": phrase})
        assert r.status_code == 400, (phrase, r.status_code)
    assert _status(rig.db, aid) == ap.PENDING and rig.calls == []


def test_approval_waits_while_a_run_is_active_or_prefect_is_silent(rig):
    aid = _open(rig.db)
    client = _client(SIGNED)
    rig.is_running.side_effect = lambda leg: leg == "lan"
    assert client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"}).status_code == 409
    assert _status(rig.db, aid) == ap.PENDING
    _fresh()
    rig.is_running.side_effect = lambda leg: None
    assert client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"}).status_code == 503
    assert _status(rig.db, aid) == ap.PENDING and rig.calls == []


# ── 23-27. Settings: hostile bodies, advanced keys, passwords, restart ──────

@pytest.mark.parametrize("content", [
    b"{", b"\xff\xfe", b"[" * 100_000, b"", b'{"changes": {}}', b'{"phrase": 5}',
    b'{"changes": {"firm_name": {"x": 1}}}', b'{"changes": {"no.such.key": 1}}',
], ids=["truncated", "not-utf8", "deep", "empty", "empty-changes", "phrase-number", "object-value",
        "unknown-key"])
def test_settings_save_answers_hostile_bodies_without_a_server_error_or_a_write(rig, content):
    before = rig.cfg_file.read_bytes()
    r = _client(SIGNED).post("/api/settings", content=content, headers=JSON_BODY)
    assert r.status_code < 500, r.status_code
    assert "Traceback" not in r.text
    assert rig.cfg_file.read_bytes() == before


@pytest.mark.xfail(strict=False, reason="SUSPECTED BUG: ui.py:1183 (save_settings), "
                   "ui.py:1288 (validate_settings) and ui.py:1080 (password change) call "
                   ".get() on a JSON null, array or number, which raises AttributeError -> HTTP 500")
def test_settings_endpoints_refuse_non_object_json_with_a_4xx(rig, monkeypatch):
    monkeypatch.setattr(admin_auth, "verify_admin_password", lambda pw, d: False)
    for cookies, path in [(SIGNED, "/api/settings"), (SIGNED, "/api/settings/validate"),
                          (ADMIN, "/api/admin/password")]:
        for body in (b"null", b"[]", b"42", b'"text"'):
            _fresh()
            r = _client(cookies).post(path, content=body, headers=JSON_BODY)
            assert r.status_code < 500, (path, body, r.status_code)


def test_a_dashboard_only_user_cannot_save_or_validate_an_advanced_key(rig):
    key = "paths.source_drive"
    assert key not in config_edit.BASIC_KEYS            # the premise: this key is Advanced-only
    client = _client(SIGNED)
    body = {"changes": {key: "D:\\Elsewhere"}}
    verdict = client.post("/api/settings/validate", json=body)
    assert verdict.status_code == 200 and verdict.json()["ok"] is False
    _fresh()
    before = rig.cfg_file.read_bytes()
    assert client.post("/api/settings", json=body).status_code >= 400
    assert rig.cfg_file.read_bytes() == before


def test_password_change_needs_advanced_and_the_current_advanced_password(rig, monkeypatch):
    setter = MagicMock()
    monkeypatch.setattr(config_edit, "set_password_hash", setter)
    monkeypatch.setattr(admin_auth, "verify_admin_password",
                        lambda pw, d: pw == ui_login.ADMIN_PASSWORD)
    body = {"current": ui_login.ADMIN_PASSWORD, "new": "Brand-new-pass-1", "confirm": "Brand-new-pass-1"}
    assert _client(SIGNED).post("/api/admin/password", json=body).status_code == 403
    assert _client(SIGNED).post("/api/admin/dashboard-password", json=body).status_code == 403
    assert _client(ADMIN).post("/api/admin/password", json={**body, "current": "wrong"}).status_code == 400
    setter.assert_not_called()


def test_restart_is_refused_while_a_run_is_active_or_the_scheduler_is_silent(rig, monkeypatch):
    restart = MagicMock()
    monkeypatch.setattr(app_restart, "request_restart", restart)
    monkeypatch.setattr(app_restart, "restart_pending", lambda: False)
    monkeypatch.setattr(ui, "_settings_lock_busy", lambda: None)
    client = _client(SIGNED)
    # the scheduler cannot be asked (fail closed): nothing restarts
    monkeypatch.setattr(ui, "get_client", MagicMock(side_effect=RuntimeError("no API")))
    assert client.post("/api/settings/apply", json={"force": True}).status_code == 409
    # a backup holds the lock: nothing restarts
    monkeypatch.setattr(ui, "_settings_lock_busy", lambda: "a backup is running (backup lock held by PID 4242)")
    _fresh()
    assert client.post("/api/settings/apply", json={"force": True}).status_code == 409
    restart.assert_not_called()


# ── 28-30. Path traversal and unusual paths ──────────────────────────────────

@pytest.mark.parametrize("path", [
    "..\\..\\Windows", "%2e%2e%5c%2e%2e%5cWindows", "..%2F..%2Fetc", "\\\\server\\share",
    "//server/share", "%5c%5cserver%5cshare%5cx", "D:relative", "C:%00", "C:\\%00\\..\\x",
    "file:///C:/Windows", "' OR 1=1 --", "http://evil.example/",
])
def test_folder_browser_refuses_relative_network_nul_and_traversal_paths(rig, path):
    r = _client(ADMIN).get(f"/api/settings/browse?path={path}")
    assert r.status_code == 400, (path, r.status_code)
    assert '"folders":' not in r.text and "Traceback" not in r.text   # the listing's key, not the word


@pytest.mark.parametrize("parent", ["\\\\evil.example\\share", "..\\..\\..\\Windows", "\x00",
                                    "C:\\" + "x" * 5000, "' OR 1=1 --", None, 5])
def test_source_check_refuses_network_and_traversal_parents_and_writes_nothing(rig, parent):
    before = rig.cfg_file.read_bytes()
    r = _client(ADMIN).post("/api/settings/check-source", json={"parent": parent})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert rig.cfg_file.read_bytes() == before


@pytest.mark.parametrize("path", ["/static/%2e%2e/ui.py", "/static/..%5cui.py",
                                  "/static/%2e%2e%2f%2e%2e%2fconfig.yaml", "/static/%252e%252e/ui.py",
                                  "/static/%00", "/static/css/%2e%2e/%2e%2e/%2e%2e/config.yaml"])
def test_static_mount_never_serves_a_file_outside_the_static_folder(path):
    r = _client().get(path)
    assert r.status_code != 500 and r.status_code != 200
    assert "dashboard:" not in r.text and "FastAPI(" not in r.text


# ── 31-32. Cross-site posts, wrong verbs ─────────────────────────────────────

@pytest.mark.xfail(strict=False, reason="SUSPECTED BUG: ui.py:770 (trigger_cloud) and ui.py:920 (approve_blocked_run) accept a "
                   "state-changing POST with a valid session cookie whatever the Origin or "
                   "Sec-Fetch-Site; the only CSRF defence is the SameSite=lax session cookie "
                   "(ui.py:448)")
def test_cross_site_post_with_a_valid_session_is_refused(rig):
    aid = _open(rig.db)
    client = _client(SIGNED)
    hostile = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    r = client.post("/trigger/cloud?wait=false", headers=hostile)
    assert r.status_code in (400, 403) and rig.calls == []
    _fresh()
    r = client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"}, headers=hostile)
    assert r.status_code in (400, 403) and _status(rig.db, aid) == ap.PENDING


@pytest.mark.parametrize("method,path", [
    ("GET", "/trigger/cloud"), ("GET", "/trigger/lan"), ("PUT", "/api/settings"),
    ("DELETE", "/api/settings"), ("GET", "/api/settings/apply"), ("GET", "/approvals/x/approve"),
    ("GET", "/approvals/x/run"), ("GET", "/approvals/x/dismiss"), ("POST", "/status"),
    ("POST", "/api/settings/history"), ("DELETE", "/trigger/verify/all"), ("PUT", "/api/rollover/run"),
    ("POST", "/report/weekly"),
])
def test_wrong_verb_on_a_signed_in_route_is_405_and_starts_nothing(rig, method, path):
    r = _client(ADMIN).request(method, path)
    assert r.status_code == 405, r.status_code
    assert rig.calls == []


# ── 33-35. SQL-looking strings, giant limits, error bodies ───────────────────

SQL_LOOKING = ["' OR '1'='1", "1; DROP TABLE approvals; --", "\" OR \"\"=\"",
               "x'); DELETE FROM approvals; --", "%27%20OR%201%3D1--"]


@pytest.mark.parametrize("sqli", SQL_LOOKING)
def test_sql_looking_strings_in_path_and_query_change_nothing(rig, sqli):
    aid = _open(rig.db)
    client = _client(ADMIN)
    quoted = quote(sqli, safe="")
    for method, url, kw in [
        ("POST", f"/approvals/{quoted}/approve", {"json": {"phrase": "APPROVE"}}),
        ("POST", f"/approvals/{quoted}/dismiss", {}),
        ("POST", f"/trigger/cloud?fy={quoted}", {}),
        ("GET", f"/api/settings/browse?path={quoted}", {}),
    ]:
        _fresh()
        r = client.request(method, url, **kw)
        assert r.status_code in (400, 404, 409), (url, r.status_code)
    assert _status(rig.db, aid) == ap.PENDING and len(rig.db.open_approvals()) == 1
    assert rig.calls == []


def test_giant_limits_and_negative_offsets_are_answered_and_capped(rig):
    _open(rig.db)
    client = _client(ADMIN)
    for qs in ["?limit=999999999999&offset=-5", "?limit=-1", "?page=0&size=1000000000", "?limit=abc"]:
        _fresh()
        r = client.get("/approvals" + qs)
        assert r.status_code == 200 and len(r.json()["approvals"]) <= 10
        _fresh()
        r = client.get("/api/settings/history" + qs)
        assert r.status_code == 200 and len(r.json()["changes"]) <= 200


def test_failures_answer_with_plain_detail_never_a_traceback(rig, monkeypatch):
    async def boom(name, parameters=None, timeout=None):
        raise RuntimeError("the scheduler refused the request")

    monkeypatch.setattr(ui, "arun_deployment", boom)
    client = _client(SIGNED)
    r = client.post("/trigger/cloud?wait=false")
    assert r.status_code == 500 and "Traceback" not in r.text and 'File "' not in r.text
    aid = _open(rig.db)
    _fresh()
    r = client.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"})
    assert r.status_code == 500 and "Traceback" not in r.text and 'File "' not in r.text
