"""opus-decision L3: the watchdog sends ONE RESOLVED mail when config.yaml is valid again.

After an invalid config the watchdog mails once and leaves the marker `watchdog_invalid_config.sent` in the
runtime dir. When the config loads again and the marker exists, one RESOLVED mail goes out through
core.notify ("the agent can start") and the marker is removed. The invalid-config mail itself stays out of the
notifications table (without a valid config the DB path is not trusted); the RESOLVED one is recorded."""
from __future__ import annotations

import email
import email.header

import pytest

from test_break_release_s1 import _config_files, _point_config_at, _StopLoop, watchdog_rig  # noqa: F401
from test_support.smtp_server import smtp_server


def _subject(msg) -> str:
    m = email.message_from_string(msg["data"])
    return str(email.header.make_header(email.header.decode_header(m["Subject"])))


def test_fixing_the_config_sends_one_resolved_mail_and_removes_the_marker(watchdog_rig, monkeypatch, tmp_path):  # noqa: F811
    with smtp_server() as srv:
        good, bad = _config_files(tmp_path, srv.server_address[1])
        marker = tmp_path / "rt" / "watchdog_invalid_config.sent"
        _point_config_at(monkeypatch, bad)
        with pytest.raises(_StopLoop):
            watchdog_rig.main()
        assert len(srv.messages) == 1 and marker.exists()

        _point_config_at(monkeypatch, good)
        watchdog_rig._alert_invalid_config()
        assert len(srv.messages) == 2
        assert "RESOLVED" in _subject(srv.messages[1]) and not marker.exists()
        watchdog_rig._alert_invalid_config()                      # nothing more once the marker is gone
        assert len(srv.messages) == 2


def test_a_valid_config_without_a_marker_sends_nothing(watchdog_rig, monkeypatch, tmp_path):  # noqa: F811
    with smtp_server() as srv:
        good, _bad = _config_files(tmp_path, srv.server_address[1])
        _point_config_at(monkeypatch, good)
        watchdog_rig._alert_invalid_config()
        assert srv.messages == []


def test_the_resolved_mail_is_recorded_in_the_notifications_table(watchdog_rig, monkeypatch, tmp_path):  # noqa: F811
    from core.manifest import ManifestDB
    from models.config import load_config
    with smtp_server() as srv:
        good, bad = _config_files(tmp_path, srv.server_address[1])
        _point_config_at(monkeypatch, bad)
        with pytest.raises(_StopLoop):
            watchdog_rig.main()
        _point_config_at(monkeypatch, good)
        watchdog_rig._alert_invalid_config()
        db = ManifestDB.from_config(load_config(str(good)))
        try:
            rows = db._get_conn().execute(
                "select kind, status from notifications where kind = 'RECOVERED'").fetchall()
        finally:
            db.close()
        assert [tuple(r) for r in rows] == [("RECOVERED", "SENT")]
