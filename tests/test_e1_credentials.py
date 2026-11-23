"""Plan E1: dashboard sign-in password hash (`dashboard.login_password_hash`), verified like the
Advanced one, never from the API key; credential fingerprints for session invalidation (E2)."""
import pytest
from pydantic import ValidationError

from core import admin_auth
from models.config import DashboardConfig


def _dash(**kw):
    return DashboardConfig(**kw)


def test_login_hash_must_be_a_hash_not_a_password():
    assert _dash(login_password_hash="").login_password_hash == ""
    good = admin_auth.hash_password("a long enough pass")
    assert _dash(login_password_hash=good).login_password_hash == good
    with pytest.raises(ValidationError):
        _dash(login_password_hash="hunter2")


def test_verify_login_password_uses_only_the_login_hash():
    d = _dash(login_password_hash=admin_auth.hash_password("office-pass-1"),
              admin_password_hash=admin_auth.hash_password("owner-pass-22"))
    assert admin_auth.verify_login_password("office-pass-1", d)
    assert not admin_auth.verify_login_password("owner-pass-22", d)      # Advanced password != sign-in
    assert not admin_auth.verify_login_password("the-api-key", d)        # no other secret signs in
    assert not admin_auth.verify_login_password("", d)


def test_no_login_hash_means_nobody_signs_in():
    d = _dash()
    assert not admin_auth.verify_login_password("the-api-key", d)
    assert not admin_auth.verify_login_password("", d)
    assert not admin_auth.credentials_set(d)


def test_credentials_set_needs_both_hashes():
    h = admin_auth.hash_password("x" * 12)
    assert not admin_auth.credentials_set(_dash(login_password_hash=h))
    assert not admin_auth.credentials_set(_dash(admin_password_hash=h))
    assert admin_auth.credentials_set(_dash(login_password_hash=h, admin_password_hash=h))


def test_fingerprint_changes_with_the_hash_and_is_empty_without_one():
    a, b = admin_auth.hash_password("same-password-1"), admin_auth.hash_password("same-password-1")
    assert a != b                                                   # salted
    assert admin_auth.fingerprint(a) != admin_auth.fingerprint(b)
    assert admin_auth.fingerprint(a) == admin_auth.fingerprint(a)
    assert admin_auth.fingerprint("") == ""
    assert a not in admin_auth.fingerprint(a) and len(admin_auth.fingerprint(a)) == 16


def test_set_password_hash_writes_the_login_key_and_hides_the_value(tmp_path):
    from core import config_edit as ce
    from core.manifest import ManifestDB
    import yaml
    from test_support.sandbox_config import _config_dict
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(_config_dict(tmp_path)), encoding="utf-8")
    db = ManifestDB(tmp_path / "m.db")
    try:
        h = admin_auth.hash_password("office-pass-1")
        ce.set_password_hash(str(cfg), "dashboard.login_password_hash", h, actor="t", db=db,
                             now_iso=lambda: "2026-10-19T09:00:00+05:30")
        assert "login_password_hash: " + h in cfg.read_text(encoding="utf-8")
        row = db.config_changes(1)[0]
        assert row["key"] == "dashboard.login_password_hash" and row["new_value"] == "'(hidden)'"
    finally:
        db.close()
    assert "dashboard.login_password_hash" in ce.LOCKED and "dashboard.login_password_hash" in ce.HIDDEN
    with pytest.raises(ValueError):
        ce.set_password_hash(str(cfg), "dashboard.port", "x", actor="t")
