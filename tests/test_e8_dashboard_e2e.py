"""Plan E8: sign-in and both password changes on a REAL uvicorn server (cookies over HTTP)."""
import socket
import threading
import time

import httpx
import pytest
import uvicorn
import yaml

import ui
from core.manifest import ManifestDB
from test_support import ui_login
from test_support.sandbox_config import _config_dict

pytestmark = pytest.mark.anonymous


@pytest.fixture
def server(tmp_path, monkeypatch):
    d = _config_dict(tmp_path)
    d["dashboard"] = dict(ui_login.DASHBOARD)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(d), encoding="utf-8")
    import models.config as mconfig
    monkeypatch.setattr(mconfig, "CONFIG_PATH", str(cfg_path))
    db = ManifestDB(tmp_path / "m.db")
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_settings_config_path", lambda: str(cfg_path))
    monkeypatch.setattr(ui, "_settings_lock_busy", lambda: None)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_announce_password_change", lambda *a, **k: None)
    ui._config, ui._config_loaded_at = None, 0.0
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(ui.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)
    db.close()
    ui._config, ui._config_loaded_at = None, 0.0


def _browser(base):
    return httpx.Client(base_url=base, follow_redirects=False,
                        headers={"Accept": "application/json"})


def _sign_in(base, password=ui_login.LOGIN_PASSWORD):
    c = _browser(base)
    r = c.post("/login", data={"password": password})
    assert r.status_code == 303, r.text
    return c


def test_sign_in_change_both_passwords_and_old_sessions_end(server):
    anon = _browser(server)
    assert anon.get("/status").status_code == 401

    alice = _sign_in(server)               # two people signed in with the office password
    bob = _sign_in(server)
    assert alice.get("/status").status_code != 401 and bob.get("/status").status_code != 401

    # Alice opens Advanced and changes the dashboard password
    r = alice.post("/api/admin/login", json={"password": ui_login.ADMIN_PASSWORD})
    assert r.status_code == 200
    r = alice.post("/api/admin/dashboard-password", json={
        "current": ui_login.ADMIN_PASSWORD, "new": "second-office-pass", "confirm": "second-office-pass"})
    assert r.status_code == 200, r.text
    assert alice.get("/status").status_code != 401            # the changer stays signed in
    assert bob.get("/status").status_code == 401              # everyone else is signed out
    assert _browser(server).post("/login", data={"password": ui_login.LOGIN_PASSWORD}).headers[
        "location"].startswith("/login?error")                # the old password no longer works
    carol = _sign_in(server, "second-office-pass")
    assert carol.get("/status").status_code != 401

    # now the Advanced password: Alice's Advanced session survives, a second Advanced session ends
    dave = _sign_in(server, "second-office-pass")
    assert dave.post("/api/admin/login", json={"password": ui_login.ADMIN_PASSWORD}).status_code == 200
    r = alice.post("/api/admin/password", json={
        "current": ui_login.ADMIN_PASSWORD, "new": "second-owner-pass", "confirm": "second-owner-pass"})
    assert r.status_code == 200, r.text
    assert alice.get("/api/settings").json()["admin"] is True
    assert dave.get("/api/settings").json()["admin"] is False
    assert dave.post("/api/admin/login", json={"password": ui_login.ADMIN_PASSWORD}).status_code == 401
    assert dave.post("/api/admin/login", json={"password": "second-owner-pass"}).status_code == 200
