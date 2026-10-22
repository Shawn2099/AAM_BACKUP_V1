"""deploy/config.install.yaml: the first-install template must load as written once the site values are in,
carry no secrets, and keep the unproven features off (fresh-install plan)."""

from pathlib import Path

import yaml

from models.config import AppConfig

TEMPLATE = Path(__file__).resolve().parent.parent / "deploy" / "config.install.yaml"


def _doc(filled: bool = True) -> dict:
    doc = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    if filled:
        doc["cloud"]["bucket"] = "client-backup-bucket"      # the one value that is deliberately invalid until filled in
    return doc


def test_an_unfilled_template_does_not_load():
    import pytest
    with pytest.raises(Exception, match="bucket"):
        AppConfig(**_doc(filled=False))


def test_template_loads_as_a_config_with_the_dashboard_login_still_to_set():
    doc = _doc()
    cfg = AppConfig(**doc)
    assert cfg.paths.source_drive == r"D:\FY26-27"
    assert cfg.paths.lan_destination == r"\\192.0.2.10\backup\FY26-27"


def test_template_has_no_secrets_and_the_unproven_features_off():
    doc = _doc()
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "smtp_password" not in text and "password_hash" not in text
    assert doc["lan"]["shutdown_after_backup"] is False   # only after the shutdown test with the owner at the NAS
    assert doc["wol"]["enabled"] is False
    assert doc["dashboard"]["bind_address"] != "127.0.0.1"  # a fresh install must be reachable from the office


def test_template_only_uses_keys_the_config_knows():
    # StrictModel forbids unknown keys: the load above would raise; this names the intent
    AppConfig(**_doc())
    assert "network_adapters" not in TEMPLATE.read_text(encoding="utf-8")


def test_the_old_trial_box_collector_is_gone():
    assert not (TEMPLATE.parent / "02_collect_config.ps1").exists()
