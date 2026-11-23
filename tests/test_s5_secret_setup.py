"""Plan S5 (SMTP password only; the Google key stays plain by owner decision 2026-10-19):
the installer encrypts a plain mail password, and the readiness probe reports whether the
password decrypts for the account it runs as."""
import sys

import pytest
import yaml

from core import secret_setup, secrets as secrets_store
from models.config import load_config
from test_support.sandbox_config import _config_dict

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")


def _cfg(tmp_path, password):
    d = _config_dict(tmp_path)
    d["notifications"] = {**d.get("notifications", {}), "smtp_password": password}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(d), encoding="utf-8")
    return str(path)


def _raw_password(path):
    return yaml.safe_load(open(path, encoding="utf-8"))["notifications"]["smtp_password"]


def test_plain_password_is_encrypted_and_still_loads(tmp_path):
    path = _cfg(tmp_path, "mail-secret-1")
    assert secret_setup.encrypt_smtp_password(path) == "encrypted"
    raw = _raw_password(path)
    assert secrets_store.is_protected(raw) and "mail-secret-1" not in open(path, encoding="utf-8").read()
    loaded = load_config(path).notifications.smtp_password
    assert str(loaded) == "mail-secret-1" and loaded.protected


def test_already_encrypted_is_left_alone(tmp_path):
    path = _cfg(tmp_path, secrets_store.protect("mail-secret-1"))
    before = open(path, "rb").read()
    assert secret_setup.encrypt_smtp_password(path) == "already encrypted"
    assert open(path, "rb").read() == before


def test_empty_password_is_not_a_secret(tmp_path):
    path = _cfg(tmp_path, "")
    before = open(path, "rb").read()
    assert secret_setup.encrypt_smtp_password(path) == "no password set"
    assert open(path, "rb").read() == before


def test_check_reports_each_state(tmp_path):
    ok = load_config(_cfg(tmp_path, secrets_store.protect("x"))).notifications
    assert secret_setup.check_smtp_password(ok).startswith("encrypted and readable")
    plain = load_config(_cfg(tmp_path, "x")).notifications
    with pytest.raises(secret_setup.SecretProblem, match="not encrypted"):
        secret_setup.check_smtp_password(plain)
    bad = load_config(_cfg(tmp_path, "dpapi:AAAA")).notifications
    with pytest.raises(secret_setup.SecretProblem, match="cannot be decrypted"):
        secret_setup.check_smtp_password(bad)
    none = load_config(_cfg(tmp_path, "")).notifications
    assert "no mail password" in secret_setup.check_smtp_password(none)
