"""Step 1 of docs/PLAN_AUDIT_PENDING: the PENDING_BACKUP verdict is stored, shown and never alarmed.

PENDING_BACKUP = every backed-up file matches; some newer files wait for the next backup. It is
not a success verdict (never reads "every file matches"), not a failure (no email, flow COMPLETED,
no red banner).
"""

import re
from pathlib import Path
from types import SimpleNamespace

from test_support import attention_cfg
from unittest.mock import MagicMock

import pendulum
import pytest

import flow
from core import report
from core.attention import AMBER, RED, compute_attention
from core.manifest import ManifestDB
from core.messages import looks_action_first
from core.statuses import AuditStatus
from core.time_utils import IST

ROOT = Path(__file__).resolve().parent.parent

PENDING = {"status": "PENDING_BACKUP", "scope": "full", "files_checked": 650, "bytes_checked": None,
           "mismatches": 0, "detail": "46 files wait for the next backup"}
VERIFIED = {"status": "VERIFIED", "scope": "full", "files_checked": 10, "bytes_checked": None,
            "mismatches": 0, "detail": "verified clean"}


def _ago(hours):
    return pendulum.now(IST).subtract(hours=hours).isoformat()


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _cfg():
    return SimpleNamespace(
        cloud=SimpleNamespace(enabled=True), lan=SimpleNamespace(enabled=True),
        dashboard=SimpleNamespace(stale_grace_hours=attention_cfg.GRACE_HOURS, alert_failure_window_days=7,
                                  public_url="http://backup-server:8080"),
        paths=SimpleNamespace(source_drive="E:\\DATA\\FY26-27"), firm_name="T",
        notifications=SimpleNamespace(send_on_failure=True),
        schedule=attention_cfg.SCHEDULE, budget=attention_cfg.BUDGET)


def _fresh_runs(db):
    for rid, mode, st in (("c", "cloud", "CLOUD_COMPLETE"), ("l", "lan", "LAN_COMPLETE")):
        db.insert_run({"run_id": rid, "mode": mode, "started_at": _ago(1), "ended_at": _ago(1),
                       "status": st})


def _record(db, leg="lan", status="PENDING_BACKUP", hours=2, detail="46 files wait"):
    db.record_audit({"audit_id": f"{leg}-{status}", "mode": leg, "started_at": _ago(hours),
                     "status": status, "detail": detail, "created_at": _ago(hours)})


# ── the verdict exists and is stored ───────────────────────────────────────

def test_pending_backup_is_an_audit_status():
    assert AuditStatus.PENDING_BACKUP == "PENDING_BACKUP"


def test_record_audit_accepts_pending_backup(db):
    _record(db)
    assert db.latest_audit("lan")["status"] == "PENDING_BACKUP"


def test_record_audit_still_rejects_unknown_status(db):
    with pytest.raises(ValueError):
        db.record_audit({"audit_id": "x", "mode": "lan", "started_at": _ago(1), "status": "MAYBE"})


# ── the flow: no email, no failure ─────────────────────────────────────────

def _flow_setup(monkeypatch):
    recorded = []
    db = MagicMock()
    db.record_audit.side_effect = recorded.append
    cfg = MagicMock()
    cfg.paths.source_drive = "E:\\FY26-27"
    cfg.paths.lan_destination = "\\\\nas\\share\\FY26-27"
    cfg.cloud.enabled = cfg.lan.enabled = True
    cfg.budget.audit_max_hours = 12
    cfg.budget.lan_post_reserve_minutes = 30
    cfg.lan.subprocess_timeout_seconds = cfg.cloud.verify_timeout_seconds = 4 * 3600
    cfg.lan.shutdown_after_backup = True
    cfg.wol.enabled = True
    monkeypatch.setattr(flow, "load_config", lambda p: cfg)
    monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(flow.ManifestDB, "from_config", lambda c: db)
    monkeypatch.setattr(flow, "_backup_slot", MagicMock())
    monkeypatch.setattr(flow, "_open_run_artefacts", lambda c, i: None)
    monkeypatch.setattr(flow, "temp_rclone_config", MagicMock())
    m = {"notify": MagicMock(), "audit_cloud": MagicMock(return_value=dict(VERIFIED)),
         "audit_lan": MagicMock(return_value=dict(PENDING))}
    monkeypatch.setattr(flow, "notify", m["notify"])
    monkeypatch.setattr(flow, "wol_check_task", MagicMock())
    monkeypatch.setattr(flow, "lan_shutdown_task", MagicMock())
    monkeypatch.setattr("core.integrity.audit_cloud", m["audit_cloud"])
    monkeypatch.setattr("core.integrity.audit_lan", m["audit_lan"])
    return recorded, m


def test_pending_backup_is_recorded_without_email_and_the_flow_completes(monkeypatch):
    recorded, m = _flow_setup(monkeypatch)
    flow.integrity_audit_flow.fn("x.yaml", "all")          # must not raise
    m["notify"].assert_not_called()
    assert {r["mode"]: r["status"] for r in recorded} == {"cloud": "VERIFIED", "lan": "PENDING_BACKUP"}


def test_a_real_difference_next_to_pending_still_alarms(monkeypatch):
    recorded, m = _flow_setup(monkeypatch)
    m["audit_cloud"].return_value = {"status": "VERIFICATION_FAILED", "scope": "full",
                                     "files_checked": 1, "bytes_checked": None, "mismatches": 2,
                                     "detail": "content mismatch"}
    with pytest.raises(Exception):
        flow.integrity_audit_flow.fn("x.yaml", "all")
    assert m["notify"].call_count == 1
    assert m["notify"].call_args.args[1].kind == "ACTION_REQUIRED"


# ── the dashboard banner: amber, action first, never red ───────────────────

def test_pending_backup_is_a_small_amber_note_not_red(db):
    _fresh_runs(db)
    _record(db, "lan")
    items = compute_attention(db, _cfg())
    mine = [i for i in items if i["code"] == "audit:lan"]
    assert [i["level"] for i in mine] == [AMBER]
    assert RED not in {i["level"] for i in items}
    assert looks_action_first(mine[0]["message"])
    assert "46 files wait" in mine[0]["message"]


def test_pending_note_clears_when_a_later_check_passes(db):
    _fresh_runs(db)
    _record(db, "lan", hours=3)
    db.record_audit({"audit_id": "later", "mode": "lan", "started_at": _ago(1),
                     "status": "VERIFIED", "created_at": _ago(1)})
    assert compute_attention(db, _cfg()) == []


# ── reports: never "all backups OK / every file matches" on pending ────────

def test_report_row_says_newer_files_wait(db):
    _record(db, "lan")
    row = report._audit_row(db, "lan")
    assert row["status"] == "NEWER FILES WAIT FOR THE NEXT BACKUP"
    assert "46 files wait" in row["detail"]


def test_pending_is_not_an_action_needed_report_subject(db):
    src = (ROOT / "core" / "report.py").read_text(encoding="utf-8")
    # the subject test names VERIFICATION_FAILED only; PENDING_BACKUP must not be added to it
    assert 'status") in ("VERIFICATION_FAILED", "PENDING_BACKUP")' not in src


# ── the pages: both renderers know the new verdict ─────────────────────────

def test_dashboard_js_words_pending_backup_in_both_renderers():
    js = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
    integrity = re.search(r"function integrityText.*?\n}\n", js, re.S).group(0)
    safety = re.search(r"function safetyText.*?\n}\n", js, re.S).group(0)
    for body in (integrity, safety):
        assert "PENDING_BACKUP" in body
    # never worded as a match or as a failure
    assert "every file matches" not in re.search(r"PENDING_BACKUP'.*", safety).group(0)
    assert "FAILED" not in re.search(r"PENDING_BACKUP'.*", integrity).group(0)


def test_failed_check_banner_says_what_to_do_then_press_check_now(db):
    _fresh_runs(db)
    _record(db, "lan", status="VERIFICATION_FAILED", detail="content-changed=1")
    [item] = [i for i in compute_attention(db, _cfg()) if i["code"] == "audit:lan"]
    assert item["level"] == RED and looks_action_first(item["message"])
    assert item["message"].startswith("Open the LAN backup details") and "Check now" in item["message"]
