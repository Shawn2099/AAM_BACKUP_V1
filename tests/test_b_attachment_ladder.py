"""Plan B4 (G-4): an attachment problem can never stop an alert from being delivered.

`send_email(..., attachment_rungs=[richest, ..., poorest])` measures the encoded message against
the server's advertised SIZE, sends the richest rung that fits, and on a permanent rejection of a
message with attachments (552, or a content filter's 550/554) or a hang-up during DATA drops ONE
rung and resends at once. Exactly one email is delivered; the rung is reported.
"""

import os

import pytest

from core import notify as nt
from test_support.smtp_server import smtp_server
from tests.test_m1_notify import _cfg

KB = 1024


def _rungs():
    # random bytes: base64 of them does not compress, so the sizes below are what the wire sees
    return [
        [{"filename": "report.txt", "content": b"r" * 200},
         {"filename": "logs.zip", "content": os.urandom(30 * KB)}],     # rung 0: report + logs
        [{"filename": "report.txt", "content": b"r" * 200},
         {"filename": "logs-tail.zip", "content": os.urandom(3 * KB)}],  # rung 1: report + tail
        [{"filename": "report.txt", "content": b"r" * 200}],             # rung 2: report only
        [],                                                              # rung 3: no attachment
    ]


def _send(tmp_path, srv):
    notifications = _cfg(tmp_path, port=srv.server_address[1], retry_attempts=3).notifications
    return nt.send_email(notifications, "subject", "<p>body</p>", attachment_rungs=_rungs())


def _attachment_names(raw: str) -> list[str]:
    import email
    return [p.get_filename() for p in email.message_from_string(raw).walk() if p.get_filename()]


def test_no_size_advertised_sends_the_richest_rung(tmp_path):
    with smtp_server() as srv:
        result = _send(tmp_path, srv)
    assert result.status == nt.SENT and result.rung == 0
    assert _attachment_names(srv.messages[0]["data"]) == ["report.txt", "logs.zip"]


def test_size_zero_means_no_limit(tmp_path):
    with smtp_server(size_limit=0) as srv:
        result = _send(tmp_path, srv)
    assert result.status == nt.SENT and result.rung == 0 and len(srv.messages) == 1


def test_small_advertised_size_picks_the_richest_rung_that_fits_without_a_failed_try(tmp_path):
    with smtp_server(size_limit=12 * KB) as srv:       # rung 1 (~5.8 KB encoded) fits, rung 0 does not
        result = _send(tmp_path, srv)
    assert result.status == nt.SENT and result.rung == 1
    assert _attachment_names(srv.messages[0]["data"]) == ["report.txt", "logs-tail.zip"]
    assert len(srv.messages) == 1 and srv.mail_attempts == 1      # no 552 round-trip needed


def test_tiny_advertised_size_falls_to_no_attachments(tmp_path):
    with smtp_server(size_limit=1200) as srv:
        result = _send(tmp_path, srv)
    assert result.status == nt.SENT and result.rung == 3
    assert _attachment_names(srv.messages[0]["data"]) == []


def test_552_from_a_server_that_did_not_advertise_size_drops_one_rung_and_resends_at_once(tmp_path):
    with smtp_server(fail_with="552 5.3.4 message too large") as srv:
        srv.fail_with = "552 5.3.4 message too large"
        # the server refuses every MAIL; nothing fits, so the poorest rung fails as well
        result = _send(tmp_path, srv)
    assert result.status == nt.FAILED
    assert srv.mail_attempts == 4                 # one try per rung, never three of the same message


def test_content_filter_refusing_attachments_falls_through_to_a_plain_email(tmp_path):
    with smtp_server(reject_attachments="554 5.7.1 attachment refused by content filter") as srv:
        result = _send(tmp_path, srv)
    assert result.status == nt.SENT and result.rung == 3
    assert len(srv.messages) == 1 and srv.mail_attempts == 4
    assert _attachment_names(srv.messages[0]["data"]) == []


def test_hangup_mid_data_drops_a_rung_instead_of_resending_the_same_big_message(tmp_path):
    with smtp_server(hangup_over=3 * KB) as srv:
        result = _send(tmp_path, srv)
    assert result.status == nt.SENT and result.rung == 2          # report only (~0.5 KB) goes through
    assert len(srv.messages) == 1 and srv.hangups >= 1
    assert _attachment_names(srv.messages[0]["data"]) == ["report.txt"]


def test_a_transient_451_is_still_retried_on_the_same_rung(tmp_path):
    with smtp_server(fail_with="451 try again later") as srv:
        result = _send(tmp_path, srv)
    assert result.status == nt.FAILED and result.attempts == 3 and result.rung is None
    assert srv.mail_attempts == 3                  # same message, normal retry, no rung change


def test_the_last_rung_rejected_permanently_is_failed_not_looped(tmp_path):
    with smtp_server(fail_with="550 5.1.1 no such user") as srv:
        result = _send(tmp_path, srv)
    assert result.status == nt.FAILED
    assert srv.mail_attempts <= 4


def test_plain_attachments_argument_still_works(tmp_path):
    with smtp_server() as srv:
        notifications = _cfg(tmp_path, port=srv.server_address[1]).notifications
        result = nt.send_email(notifications, "s", "<p>b</p>",
                               [{"filename": "a.csv", "content": b"1,2\n"}])
    assert result.status == nt.SENT and result.rung is None
    assert _attachment_names(srv.messages[0]["data"]) == ["a.csv"]


def test_notify_records_the_delivered_rung_and_a_small_server_still_gets_the_alert(tmp_path):
    from core.manifest import ManifestDB
    from tests.test_m1_notify import _event

    big = [{"filename": "AAM-support.txt", "content": b"r" * 200},
           {"filename": "AAM-logs.zip", "content": os.urandom(40 * KB)}]
    db = ManifestDB(tmp_path / "m.db")
    try:
        with smtp_server(size_limit=20 * KB) as srv:
            cfg = _cfg(tmp_path, port=srv.server_address[1])
            from unittest.mock import patch
            with patch.object(nt, "render_alert", return_value=("s", "<p>b</p>", big)):
                result = nt.notify(cfg, _event(), db=db)
        row = db.last_notification("backup:lan:LAN_FAILED", status=nt.SENT)
    finally:
        db.close()
    assert result.status == nt.SENT and result.rung == 1       # logs dropped, report kept
    assert row["attachments_rung"] == "report-only"
    assert _attachment_names(srv.messages[0]["data"]) == ["AAM-support.txt"]


def test_summary_report_with_csvs_too_big_for_the_server_is_still_delivered_without_them(tmp_path):
    import flow
    from core.manifest import ManifestDB
    from unittest.mock import patch

    csvs = [{"filename": "runs.csv", "content": os.urandom(40 * KB)}]
    with smtp_server(size_limit=20 * KB) as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1])
        with patch.object(flow.report, "build_summary_email",
                          return_value=("Weekly report", "<p>summary</p>", csvs)):
            flow._send_summary_report(cfg, days=7, period="Weekly")
    db = ManifestDB.from_config(cfg)
    try:
        row = db.last_notification("report:weekly", status=nt.SENT)
    finally:
        db.close()
    assert row["attachments_rung"] == "none"
    assert _attachment_names(srv.messages[0]["data"]) == []
