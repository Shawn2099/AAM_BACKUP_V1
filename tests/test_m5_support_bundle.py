"""Support attachments (docs/EMAIL_CONTENT_SPEC.md §5, audit 2026-10-18; owner D1:
logs with client file names may be attached).

Every PROBLEM / ACTION NEEDED / WARNING email carries a plain-text support
report and, when it fits, a zip of the run's full log folder - so a forwarded
email lets support find the cause. Secrets never leave the server."""

from test_support import ui_login  # noqa: E402
import gzip
import io
import zipfile

import pytest

from core import notify as nt
from core import support_bundle as sb
from core.manifest import ManifestDB
from models.config import AppConfig

UNC = "\\\\10.10.151.53\\aam_nas\\FY26-27"
SMTP_PASS = "S3cr3t-Pass-xyz"
API_KEY = ui_login.LOGIN_HASH          # a stored secret the bundle must scrub
PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----"


def _cfg(tmp_path, **alerts) -> AppConfig:
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": UNC,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        notifications={"smtp_host": "smtp.example", "sender": "b@firm.example",
                       "recipients": ["o@firm.example"], "smtp_username": "u",
                       "smtp_password": SMTP_PASS},
        alerts=alerts,
    )


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _run_dir(tmp_path):
    run = tmp_path / "rt" / "run_logs" / "2026-10-18" / "run-123-lan"
    run.mkdir(parents=True)
    robocopy = ("-------\n   ROBOCOPY\n" + "copied a.xlsx\n" * 50
                + "2026/10/18 21:05:01 ERROR 5 (0x00000005) Copying File E:\\FY26-27\\open.xlsx\n"
                + f"password={SMTP_PASS}\n")
    (run / "robocopy-sync.log").write_bytes(b"\xff\xfe" + robocopy.encode("utf-16-le"))
    with gzip.open(run / "rclone-lsjson.log.gz", "wb") as f:
        f.write(b'{"level":"info","msg":"listing"}\n')
    (run / "agent.log").write_text(f'key json: "private_key": "{PRIVATE_KEY}"\napi {API_KEY}\n',
                                   encoding="utf-8")
    return run


def _event(run_dir=None, kind="FAILURE"):
    return nt.AlertEvent(
        key="backup:lan:LAN_PARTIAL", kind=kind, mode="lan", title="t",
        message=f"robocopy exit 8 - some files failed (smtp {SMTP_PASS})",
        status="LAN_PARTIAL", exit_code=8, fault_code="AAM-LAN-02", run_id="run-123-lan",
        occurred_at="2026-10-18T21:08:07+05:30",
        artefacts_dir=str(run_dir) if run_dir else None)


def _by_name(attachments):
    return {a["filename"]: a["content"] for a in attachments}


def _zip_texts(content: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        return {n: z.read(n).decode("utf-8") for n in z.namelist()}


def test_support_report_and_logs_zip(tmp_path, db):
    db.insert_run({"run_id": "old-1", "mode": "lan", "started_at": "2026-10-17T21:00:00+05:30",
                   "status": "LAN_COMPLETE"})
    files = _by_name(sb.build(_cfg(tmp_path), _event(_run_dir(tmp_path)), db))
    assert set(files) == {"AAM-support_20261018-2108_AAM-LAN-02.txt",
                          "AAM-logs_20261018-2108_AAM-LAN-02.zip"}
    report = files["AAM-support_20261018-2108_AAM-LAN-02.txt"].decode("utf-8")
    for needle in ("AAM BACKUP - SUPPORT REPORT", "AAM-LAN-02", "Some files could not be copied",
                   "run-123-lan", "LAN_PARTIAL", "some files could not be copied",
                   "CAUSE", "robocopy exit 8", "SETTINGS IN EFFECT", UNC,
                   "LAST RUNS", "17 Oct 2026, 9:00 PM IST", "DATA SAFETY",
                   "TOOL LOGS", "ERROR 5 (0x00000005) Copying File", "FULL LOGS on the server",
                   "run-123-lan", sb.software_version()):
        assert needle in report, needle
    # error lines come first in the tool-log excerpt
    excerpt = report.split("robocopy-sync.log", 1)[1]
    assert excerpt.index("ERROR 5") < excerpt.index("copied a.xlsx")
    texts = _zip_texts(files["AAM-logs_20261018-2108_AAM-LAN-02.zip"])
    assert "ROBOCOPY" in texts["robocopy-sync.log"]               # UTF-16 -> UTF-8
    assert '"listing"' in texts["rclone-lsjson.log"]               # .gz unpacked
    assert all(n.endswith((".log", ".txt", ".json")) for n in texts)


def test_secrets_never_leave_the_server(tmp_path, db):
    files = sb.build(_cfg(tmp_path), _event(_run_dir(tmp_path)), db)
    blob = "\n".join(
        (a["content"].decode("utf-8") if a["filename"].endswith(".txt")
         else "\n".join(_zip_texts(a["content"]).values())) for a in files)
    for secret in (SMTP_PASS, API_KEY, "MIIEvQIBADANBgkq"):
        assert secret not in blob, secret
    assert "[REDACTED]" in blob


def test_zip_left_out_when_too_big_and_the_report_says_where_the_logs_are(tmp_path, db):
    run = _run_dir(tmp_path)
    import base64
    import os
    (run / "big.log").write_bytes(base64.b64encode(os.urandom(1_500_000)))  # ~2 MB random text
    files = _by_name(sb.build(_cfg(tmp_path, attachments_max_total_kb=200), _event(run), db))
    assert not [n for n in files if n.endswith(".zip")]
    report = next(v for n, v in files.items() if n.endswith(".txt")).decode("utf-8")
    assert "not attached" in report and str(run) in report
    assert sum(len(v) for v in files.values()) <= 200 * 1024


def test_no_logs_zip_when_file_names_must_not_leave(tmp_path, db):
    files = _by_name(sb.build(_cfg(tmp_path, attach_file_names=False),
                              _event(_run_dir(tmp_path)), db))
    assert [n for n in files if n.endswith(".txt")] and not [n for n in files if n.endswith(".zip")]


def test_resolved_emails_carry_no_attachments(tmp_path, db):
    assert sb.build(_cfg(tmp_path), _event(kind="RECOVERED"), db) == []


def test_without_a_run_folder_the_report_still_goes(tmp_path, db):
    files = _by_name(sb.build(_cfg(tmp_path), _event(None), db))
    assert list(files) == ["AAM-support_20261018-2108_AAM-LAN-02.txt"]


def test_the_email_carries_the_attachments(tmp_path, db):
    from test_support.smtp_server import smtp_server
    import email as em

    with smtp_server() as srv:
        cfg = _cfg(tmp_path)
        cfg.notifications.smtp_host = "127.0.0.1"
        cfg.notifications.smtp_port = srv.server_address[1]
        cfg.notifications.smtp_security = "none"
        cfg.notifications.smtp_auth = False
        cfg.notifications.retry_attempts = 1
        assert nt.notify(cfg, _event(_run_dir(tmp_path)), db=db).status == nt.SENT
    msg = em.message_from_string(srv.messages[0]["data"])
    names = sorted(p.get_filename() for p in msg.walk() if p.get_filename())
    assert names == ["AAM-logs_20261018-2108_AAM-LAN-02.zip",
                     "AAM-support_20261018-2108_AAM-LAN-02.txt"]


def test_pipelines_hand_the_run_folder_to_the_email(tmp_path):
    from unittest.mock import MagicMock

    import flow
    from core.time_utils import now_iso
    from tests.test_m3_alerts import _cfg as rig_cfg, _cloud_rig, _failures
    with _cloud_rig() as m:
        m["_open_run_artefacts"].return_value = MagicMock(dir=tmp_path, degraded=False)
        m["cloud_sync_task"].with_options.return_value.side_effect = RuntimeError("killed")
        with pytest.raises(RuntimeError):
            flow._run_cloud_pipeline(rig_cfg(), "run-a", now_iso())
        [event] = _failures(m["notify"])
        assert event.artefacts_dir == str(tmp_path) and event.run_id == "run-a"
