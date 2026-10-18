"""Plan K (rev 2, G-1): config.yaml has ONE locked writer.

Three writers (dashboard settings, password change, FY rollover) used to
read-modify-write config.yaml with no lock between processes, and on Windows
os.replace fails with "Access is denied" while any process has the file open.
`config_file.config_transaction` is the single path: cross-process lock ->
round-trip load -> caller edits -> backup -> atomic write (replace retried) ->
load check -> restore on an invalid result.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from filelock import FileLock

from core import config_file
from models.config import MaintenanceConfig, load_config

REPO = Path(__file__).resolve().parent.parent


def _write_config(tmp_path, extra=""):
    src = tmp_path / "src" / "FY26-27"
    src.mkdir(parents=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "# operator comment\n"
        "firm_name: Test Firm\n"
        "paths:\n"
        f"  source_drive: '{src}'\n"
        "  lan_destination: '\\\\nas\\share\\FY26-27'\n"
        f"  runtime_dir: '{tmp_path / 'rt'}'\n"
        "  gcs_key_path: ''\n"
        "cloud:\n  enabled: false\n"
        "wol:\n  enabled: false\n"
        "budget:\n  lan_max_hours: 4\n"
        "dashboard:\n  auth_enabled: true\n  api_key: secret-key\n"
        + extra,
        encoding="utf-8")
    return cfg


def test_maintenance_keys_have_defaults_and_bounds():
    fields = MaintenanceConfig.model_fields
    for name in ("config_lock_timeout_seconds", "config_replace_retries",
                 "config_replace_delay_ms"):
        assert name in fields, name
    with pytest.raises(ValueError):
        MaintenanceConfig(config_lock_timeout_seconds=0)
    with pytest.raises(ValueError):
        MaintenanceConfig(config_replace_retries=-1)
    with pytest.raises(ValueError):
        MaintenanceConfig(config_replace_delay_ms=0)


def test_transaction_writes_backs_up_and_keeps_comments(tmp_path):
    cfg = _write_config(tmp_path)
    with config_file.config_transaction(str(cfg), tmp_prefix=".t_") as txn:
        config_file.set_dotted(txn.doc, "firm_name", "New Firm")
    assert txn.backup is not None and txn.backup.exists()
    text = cfg.read_text(encoding="utf-8")
    assert "# operator comment" in text and "New Firm" in text
    assert load_config(str(cfg)).firm_name == "New Firm"


def test_exception_in_body_writes_nothing_and_makes_no_backup(tmp_path):
    cfg = _write_config(tmp_path)
    before = cfg.read_bytes()
    with pytest.raises(RuntimeError, match="compare failed"):
        with config_file.config_transaction(str(cfg), tmp_prefix=".t_") as txn:
            config_file.set_dotted(txn.doc, "firm_name", "Half Done")
            raise RuntimeError("compare failed")
    assert cfg.read_bytes() == before
    assert not list(tmp_path.glob("config.yaml.bak-*"))


def test_invalid_result_is_restored_and_reported(tmp_path):
    cfg = _write_config(tmp_path)
    before = cfg.read_bytes()
    with pytest.raises(config_file.ConfigWriteError, match="did not load"):
        with config_file.config_transaction(str(cfg), tmp_prefix=".t_") as txn:
            config_file.set_dotted(txn.doc, "budget.lan_max_hours", -5)
    assert cfg.read_bytes() == before


def test_lock_held_elsewhere_times_out_with_a_clear_error(tmp_path):
    cfg = _write_config(tmp_path, "maintenance:\n  config_lock_timeout_seconds: 1\n")
    before = cfg.read_bytes()
    with FileLock(str(cfg) + ".lock"):
        t0 = time.monotonic()
        with pytest.raises(config_file.ConfigWriteError, match="in use"):
            with config_file.config_transaction(str(cfg), tmp_prefix=".t_") as txn:
                config_file.set_dotted(txn.doc, "firm_name", "x")
        assert time.monotonic() - t0 < 10
    assert cfg.read_bytes() == before


def test_replace_is_retried_while_a_reader_holds_the_file_open(tmp_path):
    cfg = _write_config(tmp_path)
    reader = open(cfg, "rb")  # noqa: SIM115 - on Windows this blocks os.replace
    threading.Timer(0.4, reader.close).start()
    try:
        with config_file.config_transaction(str(cfg), tmp_prefix=".t_") as txn:
            config_file.set_dotted(txn.doc, "firm_name", "Replaced Under Reader")
    finally:
        reader.close()
    assert load_config(str(cfg)).firm_name == "Replaced Under Reader"


def test_replace_gives_up_after_the_configured_retries(tmp_path, monkeypatch):
    cfg = _write_config(
        tmp_path, "maintenance:\n  config_replace_retries: 2\n  config_replace_delay_ms: 10\n")
    before = cfg.read_bytes()
    calls = []

    def always_denied(src, dst):
        calls.append(1)
        raise PermissionError("Access is denied")

    monkeypatch.setattr(config_file.os, "replace", always_denied)
    with pytest.raises(config_file.ConfigWriteError, match="could not be replaced"):
        with config_file.config_transaction(str(cfg), tmp_prefix=".t_") as txn:
            config_file.set_dotted(txn.doc, "firm_name", "never")
    assert len(calls) == 3                      # 1 try + 2 retries
    assert cfg.read_bytes() == before
    assert not list(tmp_path.glob(".t_*"))      # temp file cleaned up


_WORKER = """
import sys
from core import config_file
path, ch, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
for _ in range(n):
    with config_file.config_transaction(path, tmp_prefix=".w_") as txn:
        txn.doc["firm_name"] = str(txn.doc["firm_name"]) + ch
"""


def test_two_processes_writing_at_once_lose_no_update(tmp_path):
    cfg = _write_config(tmp_path, "maintenance:\n  config_lock_timeout_seconds: 120\n")
    env = {**os.environ, "AAM_CONFIG": str(cfg), "PYTHONPATH": str(REPO)}
    n = 12
    procs = [subprocess.Popen([sys.executable, "-c", _WORKER, str(cfg), ch, str(n)],
                              cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) for ch in ("a", "b")]
    outs = [p.communicate(timeout=300)[0] for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    name = load_config(str(cfg)).firm_name
    assert name.startswith("Test Firm")
    assert name.count("a") == n and name.count("b") == n
