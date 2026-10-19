"""Regression tests for the independent code review of 2026-10-19 (groups C, D, E, F, S)."""
import copy
import pickle
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import ui
from core import config_edit as ce
from core import gcs_key, secrets as sec
from core.attention import RED, compute_attention
from core.manifest import ManifestDB
from models.config import AppConfig, NotificationConfig, load_config
from test_support import ui_login
from test_support.sandbox_config import _config_dict

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def test_an_upgraded_config_with_the_removed_dashboard_keys_still_loads(tmp_path, caplog):
    """#1: api_key / auth_enabled of an older release are dropped with a warning - the services must
    not crash-loop after an upgrade (fail closed comes from the empty password hashes)."""
    d = _config_dict(tmp_path)
    d["dashboard"] = {"auth_enabled": True, "api_key": "old-key", "bind_address": "127.0.0.1"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(d), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.dashboard.login_password_hash == "" and cfg.dashboard.admin_password_hash == ""
    assert not hasattr(cfg.dashboard, "api_key")


def test_logout_also_locks_advanced():
    """#3: on a shared PC the next person must not inherit an unlocked Advanced."""
    ui_login.register(ui)
    client = TestClient(ui.app, follow_redirects=False,
                        cookies={"session": ui_login.SESSION_TOKEN, "admin_session": ui_login.ADMIN_TOKEN})
    assert ui._is_admin.__name__  # sanity: helper exists
    r = client.get("/logout")
    assert r.status_code == 303
    assert ui_login.ADMIN_TOKEN not in ui._admin_sessions and ui_login.SESSION_TOKEN not in ui._sessions
    assert "admin_session" in r.headers.get("set-cookie", "")


def test_the_typed_mail_password_is_never_written_plain_to_the_check_copy(tmp_path, monkeypatch):
    """#5: the validate/save check copy in %TEMP% holds the encrypted form, not the typed text."""
    d = _config_dict(tmp_path)
    d["notifications"] = {"smtp_host": "s.example", "sender": "b@f.example", "recipients": ["o@f.example"]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(d), encoding="utf-8")
    seen = []
    real_load = __import__("models.config", fromlist=["load_config"]).load_config

    def spy(p):
        seen.append(Path(p).read_text(encoding="utf-8"))
        return real_load(p)

    monkeypatch.setattr("models.config.load_config", spy)
    monkeypatch.setattr(sec, "available", lambda: True)
    monkeypatch.setattr(sec, "protect", lambda text: "dpapi:" + text[::-1].encode().hex())
    monkeypatch.setattr(sec, "unprotect", lambda v: bytes.fromhex(v[len("dpapi:"):]).decode()[::-1])
    ce._load_candidate(str(path), {"notifications.smtp_password": "Typed-Secret-123"})
    assert seen and "Typed-Secret-123" not in seen[0] and "dpapi:" in seen[0]


def test_an_undecryptable_mail_password_is_a_red_dashboard_item(tmp_path, monkeypatch):
    """#6: it must not stay invisible until the first email fails."""
    monkeypatch.setattr(sec, "unprotect", lambda v: (_ for _ in ()).throw(sec.SecretsError("other machine")))
    n = NotificationConfig(smtp_password="dpapi:AAAA")
    assert n.smtp_password == "" and n.smtp_password.undecryptable is True
    for clone in (copy.deepcopy(n.smtp_password), pickle.loads(pickle.dumps(n.smtp_password))):
        assert clone.undecryptable is True and clone.protected is True
    d = _config_dict(tmp_path)
    d["notifications"] = {"smtp_password": "dpapi:AAAA"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(d), encoding="utf-8")
    db = ManifestDB(tmp_path / "m.db")
    try:
        items = [i for i in compute_attention(db, load_config(str(path)))
                 if i["code"] == "smtp_password_undecryptable"]
        assert len(items) == 1 and items[0]["level"] == RED
    finally:
        db.close()


def test_the_fy_archive_step_refuses_a_protected_key_instead_of_failing_every_morning(tmp_path):
    """#2 (latent until S4): gcloud needs the key as a file."""
    from core import fy_rollover
    key = tmp_path / "gcs_key.json.dpapi"
    key.write_text("dpapi:AAAA", encoding="utf-8")
    assert fy_rollover.run_archive_transition("bucket", "FY25-26", str(key)) is False


def test_reset_script_regrants_the_agent_account_on_the_new_config():
    """#4: the atomic config rewrite loses the explicit grant from the installer."""
    ps1 = (DEPLOY / "11_reset_dashboard_passwords.ps1").read_text(encoding="utf-8", errors="replace")
    assert 'icacls $ConfigFile /grant "${acl}:M"' in ps1 and "AamBackupAgent" in ps1


def test_keep_service_account_refuses_builtin_accounts():
    """#10."""
    ps1 = (DEPLOY / "06_install_services.ps1").read_text(encoding="utf-8", errors="replace")
    assert "NT AUTHORITY" in ps1 and "LocalSystem" in ps1


def test_password_hashing_runs_off_the_event_loop():
    """#7: PBKDF2 (600k iterations) and the notify call must not block the dashboard."""
    src = Path(ui.__file__).read_text(encoding="utf-8")
    assert "run_in_threadpool(admin_auth.hash_password" in src
    assert "run_in_threadpool(admin_auth.verify_login_password" in src
    assert "run_in_threadpool(_announce_password_change" in src
    assert "import hmac" not in src
