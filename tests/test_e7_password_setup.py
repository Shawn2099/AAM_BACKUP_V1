"""Plan E4/E7: set / reset the two passwords from the server console (core.password_setup)."""
import yaml
import pytest

from core import admin_auth, password_setup as ps
from models.config import load_config
from test_support.sandbox_config import _config_dict


def _cfg(tmp_path, dashboard=None):
    d = _config_dict(tmp_path)
    d["dashboard"] = dashboard or {}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(d), encoding="utf-8")
    return str(path)


def _typed(*answers):
    it = iter(answers)
    return lambda prompt: next(it)


def test_first_install_sets_both_and_stores_only_hashes(tmp_path):
    path = _cfg(tmp_path)
    assert ps.missing(load_config(path).dashboard) == ["login", "admin"]
    done = ps.set_passwords(path, ["login", "admin"], _typed("office-pass-123", "office-pass-123",
                                                             "owner-pass-4567", "owner-pass-4567"))
    assert done == ["Dashboard", "Advanced"]
    text = open(path, encoding="utf-8").read()
    assert "office-pass-123" not in text and "owner-pass-4567" not in text
    d = load_config(path).dashboard
    assert admin_auth.verify_login_password("office-pass-123", d)
    assert admin_auth.verify_admin_password("owner-pass-4567", d)
    assert ps.missing(d) == [] and admin_auth.credentials_set(d)


def test_only_missing_asks_for_what_is_not_set(tmp_path):
    path = _cfg(tmp_path, {"login_password_hash": admin_auth.hash_password("office-pass-123")})
    assert ps.missing(load_config(path).dashboard) == ["admin"]


@pytest.mark.parametrize("answers,message", [
    (("short", "short"), "at least"),
    (("long-enough-1", "long-enough-2"), "not the same"),
])
def test_rules_refuse_and_nothing_is_written(tmp_path, answers, message):
    path = _cfg(tmp_path)
    before = open(path, "rb").read()
    with pytest.raises(ps.PasswordSetupError, match=message):
        ps.set_passwords(path, ["login"], _typed(*answers))
    assert open(path, "rb").read() == before


def test_the_two_passwords_must_differ(tmp_path):
    path = _cfg(tmp_path)
    before = open(path, "rb").read()
    with pytest.raises(ps.PasswordSetupError, match="different"):
        ps.set_passwords(path, ["login", "admin"], _typed("same-password-1", "same-password-1",
                                                          "same-password-1", "same-password-1"))
    assert open(path, "rb").read() == before


def test_reset_one_password_must_not_equal_the_other_stored_one(tmp_path):
    path = _cfg(tmp_path, {"login_password_hash": admin_auth.hash_password("office-pass-123"),
                           "admin_password_hash": admin_auth.hash_password("owner-pass-4567")})
    with pytest.raises(ps.PasswordSetupError, match="different"):
        ps.set_passwords(path, ["admin"], _typed("office-pass-123", "office-pass-123"))


def test_a_reset_ends_older_sessions_through_the_fingerprint(tmp_path):
    path = _cfg(tmp_path, {"login_password_hash": admin_auth.hash_password("office-pass-123"),
                           "admin_password_hash": admin_auth.hash_password("owner-pass-4567")})
    old = admin_auth.fingerprint(load_config(path).dashboard.login_password_hash)
    ps.set_passwords(path, ["login"], _typed("new-office-pass-9", "new-office-pass-9"))
    assert admin_auth.fingerprint(load_config(path).dashboard.login_password_hash) != old


def test_the_command_line_helper_reads_hidden_input_and_reports(tmp_path, capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "set_pw", Path(__file__).resolve().parent.parent / "deploy" / "set_dashboard_passwords.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    path = _cfg(tmp_path)
    code = mod.main(["--config", path, "--only-missing"],
                    ask=_typed("office-pass-123", "office-pass-123", "owner-pass-4567", "owner-pass-4567"))
    assert code == 0 and "Dashboard, Advanced" in capsys.readouterr().out
    assert mod.main(["--config", path, "--only-missing"], ask=_typed()) == 0      # nothing left to ask
    assert mod.main(["--config", path, "--which", "login"], ask=_typed("x", "x")) == 1


def test_password_change_sends_an_info_email_without_the_password(tmp_path, monkeypatch):
    """Plan E6: all recipients get 'Dashboard password changed by <who> at <time>'."""
    import ui
    from core.manifest import ManifestDB
    from test_support.smtp_server import smtp_server
    from tests.test_m1_notify import _cfg as notify_cfg

    db = ManifestDB(tmp_path / "m.db")
    try:
        with smtp_server() as srv:
            cfg = notify_cfg(tmp_path, port=srv.server_address[1])
            monkeypatch.setattr(ui, "_cfg", lambda: cfg)
            monkeypatch.setattr(ui, "get_db", lambda: db)
            ui._announce_password_change(None, "Dashboard", "10.0.0.5 (session)")
    finally:
        db.close()
    assert len(srv.messages) == 1
    import email
    msg = email.message_from_string(srv.messages[0]["data"])
    text = msg["Subject"] + " ".join(
        (p.get_payload(decode=True) or b"").decode("utf-8", "replace") for p in msg.walk())
    assert "Dashboard password changed" in text and "10.0.0.5" in text
