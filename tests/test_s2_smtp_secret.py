"""Plan S2: notifications.smtp_password encrypted at rest with DPAPI; a plain value still loads
(dev / tests) but the dashboard shows amber."""
import sys

import pytest
import yaml

from core import config_edit as ce
from core import secrets as sec
from core.attention import AMBER, compute_attention
from core.manifest import ManifestDB
from models.config import NotificationConfig, load_config
from test_support.sandbox_config import _config_dict

windows = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")


def _cfg_file(tmp_path, smtp_password):
    d = _config_dict(tmp_path)
    d["notifications"] = {"smtp_host": "smtp.example", "sender": "b@firm.example",
                          "recipients": ["o@firm.example"], "smtp_username": "u",
                          "smtp_password": smtp_password}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(d), encoding="utf-8")
    return path


@windows
def test_a_protected_value_is_decrypted_on_load_and_flagged_protected():
    n = NotificationConfig(smtp_password=sec.protect("Mail-Pass-1"))
    assert n.smtp_password == "Mail-Pass-1" and n.smtp_password.protected is True


def test_a_plain_value_still_loads_and_is_flagged_plain():
    n = NotificationConfig(smtp_password="Mail-Pass-1")
    assert n.smtp_password == "Mail-Pass-1" and n.smtp_password.protected is False
    assert NotificationConfig().smtp_password == "" and getattr(NotificationConfig().smtp_password, "protected", True) is True


def test_a_value_that_cannot_be_decrypted_here_loads_empty_not_crashing(monkeypatch):
    """A config.yaml copied from another server: the services must still start; mail then reports
    its missing password (email_config_problem) and the owner re-enters it."""
    monkeypatch.setattr(sec, "unprotect", lambda v: (_ for _ in ()).throw(sec.SecretsError("other machine")))
    n = NotificationConfig(smtp_password="dpapi:AAAA")
    assert n.smtp_password == ""


@windows
def test_saving_the_password_in_settings_writes_it_encrypted(tmp_path):
    path = _cfg_file(tmp_path, "")
    ce.apply_changes(str(path), {"notifications.smtp_password": "Typed-In-Settings-9"}, phrase=None,
                     actor="t", lock_busy=lambda: None, now_iso=lambda: "2026-10-19T10:00:00")
    text = path.read_text(encoding="utf-8")
    assert "Typed-In-Settings-9" not in text and "smtp_password: 'dpapi:" in text
    assert load_config(str(path)).notifications.smtp_password == "Typed-In-Settings-9"


@windows
def test_the_settings_page_still_never_shows_the_value(tmp_path):
    path = _cfg_file(tmp_path, sec.protect("Mail-Pass-1"))
    fields = {f["key"]: f for s in ce.catalog(load_config(str(path))) for f in s["fields"]}
    f = fields["notifications.smtp_password"]
    assert f["value"] is None and f["is_set"] is True


def test_dashboard_is_amber_while_the_password_is_stored_plain(tmp_path, monkeypatch):
    monkeypatch.setattr(sec, "available", lambda: True)
    db = ManifestDB(tmp_path / "m.db")
    try:
        plain = load_config(str(_cfg_file(tmp_path, "Plain-Pass-1")))
        items = [i for i in compute_attention(db, plain) if i["code"] == "smtp_password_plain"]
        assert len(items) == 1 and items[0]["level"] == AMBER
        empty = load_config(str(_cfg_file(tmp_path, "")))
        assert not [i for i in compute_attention(db, empty) if i["code"] == "smtp_password_plain"]
    finally:
        db.close()


def test_a_config_can_be_deep_copied_and_pickled_keeping_the_flag():
    """Found by the audit lab: model_copy(deep=True) must survive the secret wrapper."""
    import copy
    import pickle

    cfg = load_config_with_plain_password()
    for clone in (cfg.model_copy(deep=True), copy.deepcopy(cfg), pickle.loads(pickle.dumps(cfg))):
        assert clone.notifications.smtp_password == "Plain-Pass-1"
        assert clone.notifications.smtp_password.protected is False


def load_config_with_plain_password():
    from models.config import AppConfig
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    return load_config(str(_cfg_file(tmp, "Plain-Pass-1")))
