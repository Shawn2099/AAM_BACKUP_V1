"""deploy/send_test_mail.py: one real mail to both lists proves the mail setup before the services are installed
(fresh-install plan R-12)."""

import importlib.util
from pathlib import Path

import yaml

from test_support import ui_login
from test_support.smtp_server import smtp_server

_spec = importlib.util.spec_from_file_location(
    "send_test_mail", Path(__file__).resolve().parent.parent / "deploy" / "send_test_mail.py")
stm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stm)


def _config_file(tmp_path, port, alerts=("ops@firm.example",), reports=("boss@firm.example",), host="127.0.0.1"):
    cfg = {
        "firm_name": "AAM",
        "paths": {"source_drive": str(tmp_path / "FY26-27"), "lan_destination": "\\\\nas\\share\\FY26-27",
                  "runtime_dir": str(tmp_path / "rt")},
        "wol": {"enabled": False}, "cloud": {"enabled": False},
        "dashboard": {"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        "notifications": {"smtp_host": host, "smtp_port": port, "sender": "b@firm.example",
                          "recipients": list(alerts), "report_recipients": list(reports),
                          "smtp_security": "none", "smtp_auth": False, "retry_attempts": 1,
                          "retry_base_seconds": 0.01},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(p)


def test_one_mail_reaches_both_lists(tmp_path, capsys):
    with smtp_server() as srv:
        assert stm.main(["x", _config_file(tmp_path, srv.server_address[1])]) == 0
        assert len(srv.messages) == 1
    out = capsys.readouterr().out
    assert out.startswith("OK Test mail") and "ops@firm.example" in out and "boss@firm.example" in out


def test_a_refusing_server_is_a_failure_with_a_reason(tmp_path, capsys):
    with smtp_server() as srv:
        srv.fail_with = "550 5.7.1 relay denied"
        assert stm.main(["x", _config_file(tmp_path, srv.server_address[1])]) == 1
    assert capsys.readouterr().out.startswith("FAIL Test mail: not delivered")


def test_no_recipients_and_no_host_are_failures(tmp_path, capsys):
    assert stm.main(["x", _config_file(tmp_path, 25, alerts=(), reports=())]) == 1
    assert "no recipients" in capsys.readouterr().out
    assert stm.main(["x", _config_file(tmp_path, 25, host="")]) == 1
    assert "smtp_host is empty" in capsys.readouterr().out
