"""D2 (final audit 2026-10-20): the banner and /health go red when the latest run of a leg failed.

compute_attention was red only for staleness (no success within 26 h), an audit, approvals and undelivered
alerts. After a failed night /health stayed 200 "healthy" until 26 h had passed since the last success (reproduced:
success 25 h ago, CLOUD_FAILED 1 h ago -> []), and an existing test even encoded "failures don't count". The
decision is by core.statuses category (never by string): FAILED is red, a MISSED night is amber (the server was
off or busy; the next run continues), PARTIAL (a budget-stopped upload that resumes) and RUNNING are not alarms."""

import pytest

from core.attention import AMBER, RED, compute_attention
from test_m3_dashboard_truth import _cfg, _codes, _run, client, db  # noqa: F401  (fixtures)


@pytest.mark.parametrize("status", [
    "CLOUD_FAILED", "CLOUD_VERIFY_FAILED", "CLOUD_SUSPECT", "CLOUD_ABANDONED", "CLOUD_SLOT_TIMEOUT",
])
def test_a_failed_latest_run_is_red_even_while_the_last_success_is_recent(db, status):
    _run(db, "ok", "cloud", "CLOUD_COMPLETE", 25)          # still inside the 26 h stale window
    _run(db, "bad", "cloud", status, 1)
    _run(db, "l", "lan", "LAN_COMPLETE", 1)
    items = compute_attention(db, _cfg())
    assert _codes(items, RED) == {"last_run:cloud"}
    assert "did not succeed" in items[0]["message"].lower()


def test_a_missed_night_is_amber_not_red(db):
    _run(db, "ok", "lan", "LAN_COMPLETE", 25)
    _run(db, "m", "lan", "LAN_MISSED", 1)
    _run(db, "c", "cloud", "CLOUD_COMPLETE", 1)
    items = compute_attention(db, _cfg())
    assert _codes(items, AMBER) == {"last_run:lan"} and not _codes(items, RED)


@pytest.mark.parametrize("status", ["CLOUD_PARTIAL", "CLOUD_RUNNING", "CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"])
def test_partial_running_and_good_runs_are_not_alarms(db, status):
    _run(db, "ok", "cloud", "CLOUD_COMPLETE", 25)
    _run(db, "now", "cloud", status, 1)
    _run(db, "l", "lan", "LAN_COMPLETE", 1)
    assert compute_attention(db, _cfg()) == []


def test_a_later_success_clears_it(db):
    _run(db, "ok", "cloud", "CLOUD_COMPLETE", 30)
    _run(db, "bad", "cloud", "CLOUD_FAILED", 20)
    _run(db, "fixed", "cloud", "CLOUD_COMPLETE", 2)
    _run(db, "l", "lan", "LAN_COMPLETE", 1)
    assert compute_attention(db, _cfg()) == []


def test_a_disabled_leg_is_ignored(db):
    _run(db, "ok", "cloud", "CLOUD_COMPLETE", 1)
    _run(db, "bad", "lan", "LAN_FAILED", 1)
    assert compute_attention(db, _cfg(lan=False)) == []


def test_health_endpoint_is_503_after_a_failed_night(client, db):    # noqa: F811
    _run(db, "ok", "cloud", "CLOUD_COMPLETE", 25)
    _run(db, "bad", "cloud", "CLOUD_FAILED", 1)
    _run(db, "l", "lan", "LAN_COMPLETE", 1)
    resp = client.get("/health")
    assert resp.status_code == 503 and resp.json()["status"] == "attention"
    assert any("cloud" in r.lower() for r in resp.json()["reasons"])
