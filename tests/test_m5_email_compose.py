"""Plain-language emails (docs/EMAIL_CONTENT_SPEC.md §3, §7; audit 2026-10-18).

Every email: severity word, what happened, "Is my data safe?" with each leg's
last good copy, numbered steps, what happens next (IST), the dashboard link,
and a "For support" block (fault code, reference, status, exit meaning,
server, version). Technical words only in the support block."""

import re

import pendulum
import pytest

from core import email_compose as ec
from core import notify as nt
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import AppConfig

UNC = "\\\\10.10.151.53\\aam_nas\\FY26-27"
JARGON = re.compile(r"robocopy|rclone|\bSMB\b|prefect|\bguard\b|NSSM|exit code", re.IGNORECASE)


def _cfg(tmp_path, cloud=True) -> AppConfig:
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / "FY26-27"), "lan_destination": UNC,
               "runtime_dir": str(tmp_path / "rt"), "gcs_key_path": str(tmp_path / "k.json")},
        wol={"enabled": False},
        cloud={"enabled": cloud, "bucket": "aam-test-bucket", "project_number": "123456"},
        dashboard={"public_url": "http://backup-server:8080"},
        schedule={"lan_cron": "0 21 * * *", "cloud_cron": "0 2 * * *"},
    )


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _ok(db, leg, when):
    db.insert_run({"run_id": f"{leg}-ok", "mode": leg, "started_at": when, "ended_at": when,
                   "status": f"{leg.upper()}_COMPLETE"})


def _top(text: str) -> str:
    return text.split("For support", 1)[0]


def _nas_failure():
    return nt.AlertEvent(
        key="backup:lan:LAN_FAILED", kind="FAILURE", mode="lan", title="LAN backup failed",
        message="LAN backup Failed: Cannot reach LAN host '10.10.151.53' on SMB port 445 "
                "after 8 attempt(s) - robocopy not started",
        status="LAN_FAILED", exit_code=-1, fault_code="AAM-NAS-01", run_id="run-123-lan",
        occurred_at="2026-10-18T21:08:07+05:30")


def test_problem_email_subject_and_plain_top(tmp_path, db):
    _ok(db, "lan", "2026-10-17T21:01:00+05:30")
    _ok(db, "cloud", "2026-10-18T02:01:00+05:30")
    subject, html, text = ec.compose(_cfg(tmp_path), _nas_failure(), db,
                                     now=pendulum.datetime(2026, 10, 18, 21, 9, tz=IST))
    assert subject == "[AAM Backup] PROBLEM: Office NAS backup - NAS not reachable (AAM-NAS-01)"
    top = _top(text)
    assert not JARGON.search(top), JARGON.search(top)
    assert "did not answer on the network" in top and "10.10.151.53" in top
    # Is my data safe? - each leg's last good copy, in IST
    assert "Is my data safe?" in top
    assert "Office NAS backup: last good copy 17 Oct 2026, 9:01 PM IST" in top
    assert "Online backup (Google Cloud): last good copy 18 Oct 2026, 2:01 AM IST" in top
    assert "never changed or deleted by the backup" in top
    # numbered steps, who acts, next run, dashboard
    assert "What to do (you)" in top
    assert "1. Check that the NAS is switched on" in top
    assert "The next office NAS backup runs automatically on 19 Oct 2026, 9:00 PM IST" in top
    assert "http://backup-server:8080" in top


def test_support_block_has_everything_support_needs(tmp_path, db, monkeypatch):
    monkeypatch.setattr(ec.socket, "gethostname", lambda: "WIN-SRV2016")
    _, html, text = ec.compose(_cfg(tmp_path), _nas_failure(), db)
    support = text.split("For support", 1)[1]
    for needle in ("AAM-NAS-01", "run-123-lan", "LAN_FAILED", "WIN-SRV2016",
                   "the tool did not run or did not finish", "18 Oct 2026, 9:08 PM IST",
                   "Cannot reach LAN host", ec.software_version()):
        assert needle in support, needle
    assert html.count("For support") == 1


def test_blocked_email_asks_for_a_decision_with_the_numbers(tmp_path, db):
    event = nt.AlertEvent(
        key="blocked:lan", kind="ACTION_REQUIRED", mode="lan",
        title="LAN backup blocked - approval needed",
        message="This backup would delete 1,240 of 5,800 backup files (21.4%; limits 500 files / 10%).",
        status="LAN_BLOCKED", fault_code="AAM-DEL-01", run_id="run-9-lan")
    subject, _, text = ec.compose(_cfg(tmp_path), event, db)
    assert subject.startswith("[AAM Backup] ACTION NEEDED: Office NAS backup - ")
    top = _top(text)
    assert "would delete 1,240 of 5,800" in top                 # the numbers, up top
    assert "do NOT approve" in top
    assert "Until you decide" in top and "nothing is deleted" in top


def test_resolved_email(tmp_path, db):
    event = nt.AlertEvent(key="recovered:lan", kind="RECOVERED", mode="lan",
                          title="LAN backup recovered", message="m", status="RECOVERED",
                          resolved_code="AAM-NAS-01")
    subject, _, text = ec.compose(_cfg(tmp_path), event, db)
    assert subject == "[AAM Backup] RESOLVED: Office NAS backup is working again (was AAM-NAS-01)"
    assert "NAS not reachable" in text and "Nothing to do" in text


def test_unknown_code_and_disabled_leg(tmp_path, db):
    event = nt.AlertEvent(key="k", kind="FAILURE", mode="cloud", title="t", message="boom",
                          status="CLOUD_FAILED", fault_code=None)
    subject, _, text = ec.compose(_cfg(tmp_path, cloud=False), event, db)
    assert "(AAM-SYS-06)" in subject and "Unexpected program error" in subject
    assert "Online backup (Google Cloud): last good copy" not in text   # disabled leg not listed


def test_uncoded_event_still_reads_plainly(tmp_path, db):
    event = nt.AlertEvent(key="x", kind="WARNING", mode="report", title="Something to know",
                          message="Plain message.")
    subject, _, text = ec.compose(_cfg(tmp_path), event, db)
    assert subject == "[AAM Backup] WARNING: Something to know"
    assert "Plain message." in text


def test_exit_meanings():
    assert ec.exit_meaning("cloud", 7) == "fatal error (a retry will not fix it)"
    assert ec.exit_meaning("lan", 0) == "no change"
    assert ec.exit_meaning("lan", 10) == "extra files in the backup; some files could not be copied"
    assert ec.exit_meaning("lan", None) == "the tool did not run or did not finish"


def test_notify_sends_the_composed_email(tmp_path, db):
    from test_support.smtp_server import smtp_server
    import email as em

    with smtp_server() as srv:
        cfg = _cfg(tmp_path)
        cfg.notifications.smtp_host = "127.0.0.1"
        cfg.notifications.smtp_port = srv.server_address[1]
        cfg.notifications.sender = "b@firm.example"
        cfg.notifications.recipients = ["o@firm.example"]
        cfg.notifications.smtp_security = "none"
        cfg.notifications.smtp_auth = False
        cfg.notifications.retry_attempts = 1
        assert nt.notify(cfg, _nas_failure(), db=db).status == nt.SENT
    msg = em.message_from_string(srv.messages[0]["data"])
    assert msg["Subject"].startswith("[AAM Backup] PROBLEM:")
    assert msg["X-AAM-Fault-Code"] == "AAM-NAS-01"
    assert msg["X-AAM-Reference"] == "run-123-lan"
    assert db.last_notification("backup:lan:LAN_FAILED")["subject"] == msg["Subject"]


# ── every producer sets its fault code ───────────────────────────────────

def test_run_alert_derives_the_code_from_the_evidence(monkeypatch):
    import flow
    sent = []
    monkeypatch.setattr(flow, "notify", lambda cfg, ev, db=None: sent.append(ev))
    flow._send_run_alert(object(), "lan", "LAN_FAILED", "m", 16, "2026-10-18T21:00:00+05:30",
                         run_id="r1", sync_result={"exit_code": 16})
    flow._send_run_alert(object(), "cloud", "CLOUD_FAILED", "m", 1, "2026-10-18T02:00:00+05:30",
                         fault_code="AAM-CLD-02")
    assert [(e.fault_code, e.run_id) for e in sent] == [("AAM-LAN-01", "r1"), ("AAM-CLD-02", None)]


def test_resolved_email_names_the_fixed_code(tmp_path, monkeypatch):
    import json
    import flow
    cfg = _cfg(tmp_path)
    db = ManifestDB.from_config(cfg)
    try:
        db.insert_run({"run_id": "bad", "mode": "lan", "started_at": "2026-10-17T21:00:00+05:30",
                       "status": "LAN_FAILED",
                       "extended_metrics": json.dumps({"fault_code": "AAM-NAS-01"})})
        db.insert_run({"run_id": "good", "mode": "lan", "started_at": "2026-10-18T21:00:00+05:30",
                       "status": "LAN_COMPLETE"})
    finally:
        db.close()
    sent = []
    monkeypatch.setattr(flow, "notify", lambda c, ev, db=None: sent.append(ev))
    flow._send_recovery_if_needed(cfg, "lan", "good")
    [event] = sent
    assert event.kind == "RECOVERED" and event.resolved_code == "AAM-NAS-01"


def test_watchdog_codes():
    import watchdog as wd
    assert wd._watchdog_fault("start_suppressed:AamBackupAgent") == "AAM-SVC-03"
    assert wd._watchdog_fault("stuck_stop:AamPrefectServer") == "AAM-SVC-02"
    assert wd._watchdog_fault("live_lock_cap") == "AAM-SVC-02"
    assert wd._watchdog_fault("restart_failed") == "AAM-SVC-01"


def test_every_producer_names_a_code():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for path, codes in {
        "flow.py": ["AAM-NAS-06", "AAM-CLK-03", "AAM-ROL-03", "AAM-AUD-01",
                    "AAM-AUD-02", "blocked_guard_code(guard_record)"],
        "watchdog.py": ["AAM-SYS-03", "AAM-SVC-04"],
        "core/backup_repository.py": ["AAM-SYS-02"],
    }.items():
        text = (root / path).read_text(encoding="utf-8")
        for code in codes:
            assert code in text, (path, code)


def test_technical_messages_stay_in_the_support_block(tmp_path, db):
    event = nt.AlertEvent(key="watchdog:x", kind="ACTION_REQUIRED", mode="watchdog", title="t",
                          message="AamBackupAgent is STOPPED and auto-start is suppressed",
                          status="START_SUPPRESSED", fault_code="AAM-SVC-03")
    _, _, text = ec.compose(_cfg(tmp_path), event, db)
    top = _top(text)
    assert "Details:" not in top and "AamBackupAgent" not in top
    assert "What to do (fix by support)" in top
    assert "AamBackupAgent is STOPPED" in text.split("For support", 1)[1]
