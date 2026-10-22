"""A1 (final audit 2026-10-20): a failed weekly audit stays authoritative until a later audit reaches a verdict.

`ManifestDB.latest_audit` returned the newest row of ANY status although its docstring promises that a
VERIFICATION_FAILED row stays authoritative. Week 1 VERIFICATION_FAILED, week 2 AUDIT_INCOMPLETE (a DNS or TLS
outage) made the banner amber, the report headline "all backups OK" and the dashboard say "could not run":
the unresolved divergence disappeared from every surface. One fix, in the one place every reader uses."""

from types import SimpleNamespace

from test_support import attention_cfg

import pendulum
import pytest

import ui
from core import report
from core.attention import RED, compute_attention
from core.manifest import ManifestDB
from core.time_utils import IST

F, I, V, P = "VERIFICATION_FAILED", "AUDIT_INCOMPLETE", "VERIFIED", "PENDING_BACKUP"


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


def _seed(db, statuses, leg="lan"):
    """Audits oldest -> newest, one every 24 h, the newest 2 h ago."""
    for i, status in enumerate(statuses):
        hours = 2 + 24 * (len(statuses) - 1 - i)
        db.record_audit({"audit_id": f"{leg}-{i}", "mode": leg, "started_at": _ago(hours),
                         "ended_at": _ago(hours), "status": status,
                         "detail": "3 files differ" if status == F else f"detail {status}",
                         "created_at": _ago(hours)})


def _fresh_runs(db):
    for rid, mode, st in (("c", "cloud", "CLOUD_COMPLETE"), ("l", "lan", "LAN_COMPLETE")):
        db.insert_run({"run_id": rid, "mode": mode, "started_at": _ago(1), "ended_at": _ago(1),
                       "status": st})


@pytest.mark.parametrize("sequence,expected,shows_newer", [
    ([F, I], F, True),            # the reported hole
    ([F, I, I], F, True),
    ([F, I, V], V, False),        # a later verdict resolves it
    ([F, I, P], P, False),
    ([V, I], I, False),           # nothing unresolved: the newest fact is "could not run"
    ([P, I], I, False),
    ([I], I, False),
    ([F], F, False),
])
def test_latest_audit_keeps_an_unresolved_failure_authoritative(db, sequence, expected, shows_newer):
    _seed(db, sequence)
    audit = db.latest_audit("lan")
    assert audit["status"] == expected
    assert ("newer_incomplete_at" in audit) is shows_newer


def test_scope_filter_applies_to_both_lookups(db):
    for aid, scope, status, hours in (("a", "full", F, 50), ("b", "full", I, 26), ("c", "quick", V, 2)):
        db.record_audit({"audit_id": aid, "mode": "lan", "scope": scope, "started_at": _ago(hours),
                         "status": status, "detail": "d", "created_at": _ago(hours)})
    assert db.latest_audit("lan", scope="full")["status"] == F
    assert db.latest_audit("lan", scope="quick")["status"] == V


def test_banner_stays_red_with_the_newer_check_mentioned(db):
    _fresh_runs(db)
    _seed(db, [F, I])
    items = [i for i in compute_attention(db, _cfg()) if i["code"] == "audit:lan"]
    assert len(items) == 1 and items[0]["level"] == RED
    assert "do not match" in items[0]["message"] and "could not run" in items[0]["message"]


def test_report_headline_still_demands_action(db):
    _seed(db, [F, I])
    summaries = [{"leg": "lan", "label": "LAN", "total": 1, "problems": []}]
    headline, act = report.report_headline(db, summaries)
    assert act is True and "safety check found differences" in headline


def test_report_row_and_dashboard_summary_keep_the_failure(db):
    _seed(db, [F, I])
    row = report._audit_row(db, "lan")
    assert row["status"] == F and "newer check" in row["detail"].lower()
    summary = ui._integrity_summary(db, "lan")
    assert summary["status"] == F and summary["newer_incomplete_at"]
