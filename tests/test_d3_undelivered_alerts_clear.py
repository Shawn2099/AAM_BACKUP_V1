"""D3 (final audit 2026-10-20): the "alert emails could not be delivered" banner clears once mail works again.

`alerts_undelivered` counted every FAILED notification of the last 7 days even after later emails were SENT: a false
RED banner and a 503 on /health for up to 7 days after the mail problem was fixed. Email is one transport, so a
SENT email newer than a failure proves delivery works again; only failures newer than the last SENT count."""

from core.attention import RED, compute_attention
from test_m3_dashboard_truth import _ago, _cfg, _codes, _run, db  # noqa: F401  (fixture)


def _notification(db, status, hours_ago, key="backup:lan:LAN_FAILED", error=None):
    db.record_notification({"event_key": key, "kind": "FAILURE", "subject": "s", "status": status,
                            "error": error, "created_at": _ago(hours_ago)})


def _healthy_backups(db):
    _run(db, "c", "cloud", "CLOUD_COMPLETE", 1)
    _run(db, "l", "lan", "LAN_COMPLETE", 1)


def test_failures_before_a_later_sent_email_are_not_shown(db):
    _healthy_backups(db)
    _notification(db, "FAILED", 100, error="535 auth")
    _notification(db, "FAILED", 90, error="535 auth")
    _notification(db, "SENT", 1, key="backup:cloud:CLOUD_FAILED")
    assert "alerts_undelivered" not in _codes(compute_attention(db, _cfg()))


def test_a_failure_after_the_last_sent_email_is_red_and_counts_only_those(db):
    _healthy_backups(db)
    _notification(db, "FAILED", 100, error="old outage")
    _notification(db, "SENT", 50)
    _notification(db, "FAILED", 3, error="535 auth")
    items = [i for i in compute_attention(db, _cfg()) if i["code"] == "alerts_undelivered"]
    assert len(items) == 1 and items[0]["level"] == RED
    assert "1 alert/report email(s)" in items[0]["message"] and "535 auth" in items[0]["message"]


def test_failures_with_no_sent_email_at_all_stay_red(db):
    _healthy_backups(db)
    _notification(db, "FAILED", 3, error="535 auth")
    assert "alerts_undelivered" in _codes(compute_attention(db, _cfg()), RED)


def test_suppressed_and_disabled_rows_do_not_count_as_proof_that_mail_works(db):
    _healthy_backups(db)
    _notification(db, "FAILED", 5, error="535 auth")
    _notification(db, "SUPPRESSED", 2)
    _notification(db, "DISABLED", 1)
    assert "alerts_undelivered" in _codes(compute_attention(db, _cfg()), RED)
