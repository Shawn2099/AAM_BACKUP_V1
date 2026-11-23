"""Plan G1/G2: a sample for every fault code, and the preview command."""
import importlib.util
from pathlib import Path

import yaml

from core import faults
from core.email_samples import RETIRED, samples
from test_support.sandbox_config import _config_dict
from test_support.smtp_server import smtp_server
from tests.test_m1_notify import _cfg


def test_every_fault_code_has_a_sample(tmp_path):
    names = {s["name"] for s in samples(_cfg(tmp_path))}
    missing = set(faults.CATALOG) - RETIRED - names
    assert not missing, f"no email sample for {sorted(missing)}"
    assert RETIRED <= set(faults.CATALOG)


def test_samples_cover_reminder_resolved_and_password_notices(tmp_path):
    names = {s["name"] for s in samples(_cfg(tmp_path))}
    assert any("reminder" in n for n in names) and "RESOLVED" in names
    assert {"Dashboard password changed", "Advanced password changed"} <= names


def test_samples_are_real_emails_with_a_subject_and_both_bodies(tmp_path):
    for s in samples(_cfg(tmp_path)):
        assert s["subject"].startswith("[") and "<html" in s["html"].lower() and s["text"].strip(), s["name"]


def _tool():
    spec = importlib.util.spec_from_file_location(
        "preview_emails", Path(__file__).resolve().parent.parent / "tools" / "preview_emails.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_preview_tool_sends_every_sample_to_the_given_address_only(tmp_path):
    tool = _tool()
    with smtp_server() as srv:
        d = _config_dict(tmp_path)
        d["notifications"] = {"smtp_host": "127.0.0.1", "smtp_port": srv.server_address[1],
                              "sender": "b@firm.example", "recipients": ["other@firm.example"],
                              "smtp_security": "none", "smtp_auth": False, "retry_attempts": 1,
                              "retry_base_seconds": 0.01}
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.safe_dump(d), encoding="utf-8")
        code = tool.main(["--config", str(cfg), "--to", "owner@firm.example"])
        expected = len(samples(tool.load_config(str(cfg))))
    assert code == 0 and len(srv.messages) == expected
    assert all(len(m["rcpts"]) == 1 and "owner@firm.example" in m["rcpts"][0] for m in srv.messages)


def test_preview_tool_writes_eml_files_marked_preview(tmp_path):
    tool = _tool()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(_config_dict(tmp_path)), encoding="utf-8")
    assert tool.main(["--config", str(cfg), "--out", str(tmp_path / "out")]) == 0
    files = sorted((tmp_path / "out").glob("*.eml"))
    assert files and b"[PREVIEW]" in files[0].read_bytes()
