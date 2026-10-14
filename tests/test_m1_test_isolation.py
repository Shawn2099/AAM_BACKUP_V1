"""M1 1.7 / WP16 — the test session can never resolve the production config."""

from pathlib import Path

from test_support.sandbox_config import SANDBOX_BUCKET, is_sandbox_path


def test_config_path_resolves_to_sandbox():
    import models.config as mc

    assert is_sandbox_path(mc.CONFIG_PATH), mc.CONFIG_PATH
    repo_config = Path(mc.__file__).resolve().parent.parent / "config.yaml"
    assert Path(mc.CONFIG_PATH) != repo_config


def test_sandbox_config_is_valid_and_harmless():
    import models.config as mc

    cfg = mc.load_config(mc.CONFIG_PATH)
    assert is_sandbox_path(cfg.paths.runtime_dir)
    assert is_sandbox_path(cfg.paths.log_directory)
    assert is_sandbox_path(cfg.paths.database_path)
    assert is_sandbox_path(cfg.paths.source_drive)
    assert cfg.cloud.bucket == SANDBOX_BUCKET
    assert "aam-test-sandbox-no-such-share" in cfg.paths.lan_destination
    assert cfg.wol.enabled is False
    assert cfg.lan.shutdown_after_backup is False
    assert cfg.notifications.send_on_failure is False
