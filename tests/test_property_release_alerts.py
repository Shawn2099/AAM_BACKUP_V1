"""U25 (agent_runs/TEST_DESIGN_RELEASE.md) - an overdue leg is not mailed without limit while nothing runs.

The LAN leg last succeeded 72 h before the start of the simulation and no run has happened since. The
watchdog's stale check (watchdog._check_stale_legs, the real function, the real notify path and its
cooldown read from the manifest DB) is called once per watchdog.stale_check_interval_seconds of a fake
clock, over 72 h, against a real SMTP sink.

Oracle (from the card): the number of "backup overdue" mails for stale:FY26-27:lan is at least 1 and at
most ceil(72 / alerts.stale_repeat_hours). The checks run from 0 h up to, not including, the 72 h mark
(0 h, 0.5 h, ... 71.5 h): one mail at the start and one more per full repeat window, which is the bound
the card's formula describes.

Also checked: with the default 24 h repeat the mails leave exactly at 0 h, 24 h and 48 h.
"""

import math
import tempfile
from pathlib import Path

import pendulum
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import watchdog as wd
from core.manifest import ManifestDB
from models.config import AppConfig
from test_support.smtp_server import smtp_server

BASE = pendulum.datetime(2026, 10, 22, 15, 0, tz="Asia/Kolkata")
WINDOW_HOURS = 72
STALE_KEY = "stale:FY26-27:lan"


def _cfg(tmp: Path, port: int, repeat_hours: float) -> AppConfig:
    return AppConfig(
        paths={"source_drive": str(tmp / "FY26-27"), "lan_destination": "\\\\nas\\backup\\FY26-27",
               "runtime_dir": str(tmp / "rt"), "gcs_key_path": str(tmp / "key.json")},
        wol={"enabled": False},
        cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080"},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": port, "sender": "b@test.local",
                       "recipients": ["ops@test.local"], "smtp_security": "none",
                       "smtp_auth": False, "retry_attempts": 1, "retry_base_seconds": 0.01,
                       "smtp_timeout_seconds": 5, "send_on_failure": True},
        alerts={"repeat_cooldown_hours": 12, "stale_repeat_hours": repeat_hours},
    )


def _sent_hours(tmp: Path, repeat_hours: float) -> list[float]:
    """Simulate 72 h of stale checks and return the hour offsets from BASE at which a stale mail SENT.

    Each distinct SENT row for the stale key is one mail on the wire; the number of SMTP messages the sink
    received must match it (checked)."""
    with smtp_server() as srv:
        cfg = _cfg(tmp, srv.server_address[1], repeat_hours)
        interval = cfg.watchdog.stale_check_interval_seconds
        db = ManifestDB.from_config(cfg)
        sent_at: list[str] = []
        try:
            started = BASE.subtract(hours=WINDOW_HOURS).isoformat()   # last LAN success: 3 days ago
            db.insert_run({"fy": "FY26-27", "run_id": "last-lan", "mode": "lan", "started_at": started,
                           "ended_at": started, "status": "LAN_COMPLETE"})
            for k in range(WINDOW_HOURS * 3600 // interval):
                with pendulum.travel_to(BASE.add(seconds=k * interval), freeze=True):
                    assert wd._check_stale_legs(cfg) == ["lan"]    # still overdue at every check
                    last = db.last_notification(STALE_KEY, status="SENT")
                if last and (not sent_at or sent_at[-1] != last["created_at"]):
                    sent_at.append(last["created_at"])
        finally:
            db.close()
    assert len(sent_at) == len(srv.messages), "every SENT row must be one mail on the wire"
    return [(pendulum.parse(t) - BASE).total_seconds() / 3600 for t in sent_at]


@pytest.mark.parametrize("repeat_hours", [1, 6, 24, 72])
def test_overdue_leg_mails_stay_within_the_repeat_bound(repeat_hours):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        mails = _sent_hours(Path(d), repeat_hours)
    assert 1 <= len(mails) <= math.ceil(WINDOW_HOURS / repeat_hours), (repeat_hours, mails)


@settings(max_examples=8, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(repeat_hours=st.integers(min_value=1, max_value=72))
def test_property_any_repeat_window_bounds_the_mails_over_72_hours(repeat_hours):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        mails = _sent_hours(Path(d), repeat_hours)
    assert 1 <= len(mails) <= math.ceil(WINDOW_HOURS / repeat_hours), (repeat_hours, mails)


def test_default_24h_repeat_mails_leave_at_0_24_and_48_hours():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        mails = _sent_hours(Path(d), 24)
    assert mails == pytest.approx([0.0, 24.0, 48.0], abs=0.01)
