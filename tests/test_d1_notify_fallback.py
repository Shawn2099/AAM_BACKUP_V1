"""D1 (final audit 2026-10-20): an alert is never lost because building it failed.

`notify()` wrapped the DB open, the incident lookup, compose and the support bundle in one try; any exception
there returned FAILED with NO email, no fallback and no notifications row (reproduced 4 ways against a real SMTP
server) - email is the only push channel, so the failure itself was silenced. Now a short plain notice is sent
without the DB, compose or attachments; the outcome is recorded when the DB works; a process-local cooldown keeps a
broken DB from turning a persistent condition (the watchdog loops every minute) into a mail storm."""

import contextlib
from unittest import mock

import pytest

from core import notify as nt
from core.manifest import ManifestDB
from test_support.smtp_server import smtp_server
from tests.test_m1_notify import _cfg, _decoded, _event


@pytest.fixture(autouse=True)
def _fresh_throttle():
    nt._FALLBACK_SENT.clear()
    yield
    nt._FALLBACK_SENT.clear()


def _run(tmp_path, breakage, *, event=None, **notif):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1], **notif)
        (tmp_path / "rt").mkdir(exist_ok=True)
        with contextlib.ExitStack() as stack:
            for p in breakage:
                stack.enter_context(p)
            result = nt.notify(cfg, event or _event())
        return result, list(srv.messages), cfg


BREAKAGES = {
    "db_open": lambda: [mock.patch("core.manifest.ManifestDB.from_config", side_effect=RuntimeError("db locked"))],
    "compose": lambda: [mock.patch("core.email_compose.compose", side_effect=RuntimeError("compose boom"))],
    "support_bundle": lambda: [mock.patch("core.support_bundle.build", side_effect=RuntimeError("zip fail"))],
    "incident_lookup": lambda: [mock.patch("core.manifest.ManifestDB.last_notification",
                                           side_effect=RuntimeError("sqlite busy"))],
}


@pytest.mark.parametrize("name", sorted(BREAKAGES))
def test_a_short_notice_is_sent_when_building_the_alert_fails(tmp_path, name):
    result, messages, _cfg_ = _run(tmp_path, BREAKAGES[name]())
    assert result.status == nt.SENT and len(messages) == 1
    text = _decoded(messages[0]["data"])
    assert "LAN backup failed" in text and "robocopy exit 16" in text        # the event itself
    assert "could not be prepared" in text                                   # and why this notice is short
    assert "Subject:" in messages[0]["data"] and "LAN backup failed" in messages[0]["data"]
    assert "[attachment" not in text                                         # nothing that could fail again


def test_the_outcome_is_recorded_when_the_db_still_works(tmp_path):
    result, _messages, cfg = _run(tmp_path, BREAKAGES["compose"]())
    db = ManifestDB.from_config(cfg)
    rows = [dict(r) for r in db._get_conn().execute("select * from notifications")]
    db.close()
    assert result.status == nt.SENT
    assert [r["status"] for r in rows] == ["SENT"] and rows[0]["event_key"] == "backup:lan:LAN_FAILED"


def test_a_broken_db_does_not_cause_a_mail_storm(tmp_path):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, port=srv.server_address[1])
        (tmp_path / "rt").mkdir(exist_ok=True)
        with mock.patch("core.manifest.ManifestDB.from_config", side_effect=RuntimeError("db locked")):
            first = nt.notify(cfg, _event())
            again = nt.notify(cfg, _event())                       # e.g. the watchdog's next loop
            other = nt.notify(cfg, _event(key="backup:cloud:CLOUD_FAILED"))
        assert (first.status, again.status, other.status) == (nt.SENT, nt.SUPPRESSED, nt.SENT)
        assert len(srv.messages) == 2


def test_the_send_on_failure_switch_still_applies_to_routine_events(tmp_path):
    result, messages, _ = _run(tmp_path, BREAKAGES["db_open"](), send_on_failure=False)
    assert result.status == nt.DISABLED and not messages
    action = _event(key="blocked:lan", kind="ACTION_REQUIRED")
    result, messages, _ = _run(tmp_path, BREAKAGES["db_open"](), event=action, send_on_failure=False)
    assert result.status == nt.SENT and len(messages) == 1                  # a decision is never silenced


def test_a_dead_mail_server_still_never_raises(tmp_path):
    cfg = _cfg(tmp_path, port=1)                                            # nothing listens
    (tmp_path / "rt").mkdir(exist_ok=True)
    with mock.patch("core.manifest.ManifestDB.from_config", side_effect=RuntimeError("db locked")):
        result = nt.notify(cfg, _event())
    assert result.status == nt.FAILED
