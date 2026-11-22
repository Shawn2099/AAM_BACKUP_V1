"""Release break-it cards U3, U4, U6, U7, U11, U16, U19, U23 (agent_runs/TEST_DESIGN_RELEASE.md, section B).

One file for eight cards. Every oracle is written in this file from first principles (hand tables of UTC instants,
the April-1 rule on an IST date, ntpath splits, SMTP sink counts, raw sqlite reads); the code under test is never
asked a second time to produce the expected value. Nothing reaches the network and no system clock is changed
(`pendulum.travel_to` only freezes the library's own "now").

Cards that report a product gap are marked `xfail(strict=False, reason="FINDING: ...")`.
"""
from __future__ import annotations

import json
import math
import ntpath
import re
import socket
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from email import message_from_string
from email.header import decode_header, make_header
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pendulum
import pytest
import yaml
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given, settings, strategies as st

import flow
import ui
from core import approvals as ap
from core import fy_overlap, fy_rollover, guards, overdue, report
from core.manifest import ManifestDB
from core.report_period import ReportPeriod
from core.time_utils import IST, get_fy_prefix, now_iso
from models.config import AppConfig
from test_support import ui_login
from test_support.smtp_server import smtp_server

UTC = timezone.utc
SENDER = "backup@firm.example"
ALERTS = "ops@firm.example"


# ═════════════════════════════════════════════════════════════════════════════════════════════
# shared helpers
# ═════════════════════════════════════════════════════════════════════════════════════════════

def _unc(parent: str, fy: str) -> str:
    return f"{parent}\\{fy}"


def _notif(port: int, **extra) -> dict:
    n = {"smtp_host": "127.0.0.1", "smtp_port": port, "sender": SENDER, "recipients": [ALERTS],
         "smtp_security": "none", "smtp_auth": False, "retry_attempts": 1, "retry_base_seconds": 0.01,
         "smtp_timeout_seconds": 2, "send_on_failure": True}
    n.update(extra)
    return n


def _cfg(tmp_path, fy: str = "FY26-27", port: int = 25, *, alerts: dict | None = None, **top) -> AppConfig:
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / fy), "lan_destination": _unc("\\\\nas\\share", fy),
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        notifications=_notif(port), alerts=alerts or {}, **top)


def _dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _subject(msg: dict) -> str:
    parsed = message_from_string(msg["data"])
    return str(make_header(decode_header(parsed["Subject"] or "")))


def _header(msg: dict, name: str) -> str:
    return str(make_header(decode_header(message_from_string(msg["data"])[name] or "")))


# ═════════════════════════════════════════════════════════════════════════════════════════════
# U3 - overdue across a DST change (schedule.timezone = Europe/London)
# ═════════════════════════════════════════════════════════════════════════════════════════════

LONDON = ZoneInfo("Europe/London")


def _u(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# Hand tables. Cron "30 1 * * *" in Europe/London, run budget 4 h, grace 2 h.
#   spring: clocks go 01:00 -> 02:00 on Sun 2027-03-28, so 01:30 local does not exist that day. Standard cron
#           (Vixie, croniter, CronSim) runs a job from the skipped hour at the first real instant after the
#           gap: 02:00 BST = 01:00 UTC.  Deadline = that + 4 h + 2 h = 07:00 UTC.
#   autumn: clocks go 02:00 -> 01:00 on Sun 2027-10-31, so 01:30 local occurs twice (00:30Z BST, 01:30Z GMT).
#           A job fires ONCE that night, at the first occurrence (00:30Z).  Deadline = 06:30Z.
DST_CASES = {
    "spring": {
        "started": datetime(2027, 3, 27, 1, 30, tzinfo=LONDON),                      # 01:30Z (GMT)
        "firings_utc": [_u(2027, 3, 27, 1, 30), _u(2027, 3, 28, 1, 0), _u(2027, 3, 29, 0, 30),
                        _u(2027, 3, 30, 0, 30)],
        "next_utc": _u(2027, 3, 28, 1, 0),
        "deadline_utc": _u(2027, 3, 28, 7, 0),
        "window_end_utc": _u(2027, 3, 30, 2, 0),
        "firings_in_window": 3,                       # 28, 29, 30 March (the start itself is not "between")
    },
    "autumn": {
        "started": datetime(2027, 10, 30, 1, 30, tzinfo=LONDON),                     # 00:30Z (BST)
        "firings_utc": [_u(2027, 10, 30, 0, 30), _u(2027, 10, 31, 0, 30), _u(2027, 11, 1, 1, 30),
                        _u(2027, 11, 2, 1, 30)],
        "next_utc": _u(2027, 10, 31, 0, 30),
        "deadline_utc": _u(2027, 10, 31, 6, 30),
        "window_end_utc": _u(2027, 11, 2, 2, 0),
        "firings_in_window": 3,                       # 31 Oct, 1 Nov, 2 Nov
    },
}
CRON, BUDGET_H, GRACE_H = "30 1 * * *", 4, 2


@pytest.mark.parametrize("case", sorted(DST_CASES))
def test_u3_overdue_flips_exactly_once_at_the_hand_computed_instant(case):
    c = DST_CASES[case]
    start_utc = c["started"].astimezone(UTC)
    flags = []
    for hour in range(1, 73):                                    # hourly instants for 72 h, fake clock = the loop
        now = start_utc + timedelta(hours=hour)
        flags.append(now > overdue.deadline(CRON, "Europe/London", c["started"], BUDGET_H, GRACE_H))
    # hand oracle: overdue iff now > hand deadline
    expected = [(start_utc + timedelta(hours=h)) > c["deadline_utc"] for h in range(1, 73)]
    assert flags == expected
    assert flags.count(True) > 0 and flags.count(False) > 0                 # the oracle is not degenerate
    flips = sum(1 for a, b in zip(flags, flags[1:]) if a != b)
    assert flips == 1 and flags[0] is False and flags[-1] is True          # never early, overdue exactly once
    # and the instant itself, compared in UTC (real elapsed time, no wall-clock arithmetic)
    got = overdue.deadline(CRON, "Europe/London", c["started"], BUDGET_H, GRACE_H).astimezone(UTC)
    assert got == c["deadline_utc"]
    assert overdue.next_firing(CRON, "Europe/London", c["started"]).astimezone(UTC) == c["next_utc"]


@pytest.mark.parametrize("case", sorted(DST_CASES))
def test_u3_no_day_is_skipped_and_none_is_doubled(case):
    c = DST_CASES[case]
    start_utc = c["started"].astimezone(UTC)
    # the latest firing at each hourly instant is the hand-listed one (so every local day has exactly one run)
    for hour in range(0, 73):
        now = start_utc + timedelta(hours=hour)
        want = max(f for f in c["firings_utc"] if f <= now)
        got = overdue.latest_firing(CRON, "Europe/London", now.astimezone(LONDON)).astimezone(UTC)
        assert got == want, (case, hour)
    n = overdue.firings_between(CRON, "Europe/London", start_utc + timedelta(minutes=1), c["window_end_utc"])
    assert n == c["firings_in_window"]
    # exactly one firing per London calendar day over the window (hand list, re-read as local dates)
    days = [f.astimezone(LONDON).date() for f in c["firings_utc"]]
    assert len(days) == len(set(days)) == 4


# ═════════════════════════════════════════════════════════════════════════════════════════════
# U4 - the FY day is the IST day whatever the server's own timezone
# ═════════════════════════════════════════════════════════════════════════════════════════════

_IST_OFFSET = timedelta(hours=5, minutes=30)


def _oracle_ist_date(instant: datetime) -> date:
    return (instant.astimezone(UTC) + _IST_OFFSET).date()


def _oracle_fy(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"FY{start % 100:02d}-{(start + 1) % 100:02d}"


_PLAIN = st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2030, 12, 31, 23, 59),
                      timezones=st.just(UTC))


def _boundary() -> st.SearchStrategy:
    # 18:00-19:00Z on 31 Mar / 31 Dec: 23:30-00:30 IST, the minutes where a wrong zone changes the answer
    return st.builds(lambda year, month, minute: datetime(year, month, 31, 18, 0, tzinfo=UTC) + timedelta(minutes=minute),
                     st.integers(2026, 2030), st.sampled_from([3, 12]), st.integers(0, 59))


@pytest.fixture(scope="module")
def u4_paths(tmp_path_factory):
    return tmp_path_factory.mktemp("u4")


def _local_tz_ctx(name: str):
    class _Ctx:
        def __enter__(self):
            pendulum.set_local_timezone(pendulum.timezone(name))

        def __exit__(self, *exc):
            pendulum.set_local_timezone()
    return _Ctx()


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(instant=st.one_of(_PLAIN, _boundary()), local=st.sampled_from(["UTC", "America/Los_Angeles"]))
@example(instant=datetime(2027, 3, 31, 18, 30, tzinfo=UTC), local="UTC")           # 00:00 IST on 1 April
@example(instant=datetime(2027, 3, 31, 18, 29, tzinfo=UTC), local="America/Los_Angeles")   # 23:59 IST on 31 March
@example(instant=datetime(2027, 12, 31, 18, 30, tzinfo=UTC), local="UTC")          # 00:00 IST on 1 January
def test_u4_fy_day_and_overlap_window_follow_ist_not_the_server_zone(u4_paths, instant, local):
    want_date = _oracle_ist_date(instant)
    want_fy = _oracle_fy(want_date)
    with _local_tz_ctx(local):
        # (a) "now" sources
        with pendulum.travel_to(pendulum.instance(instant), freeze=True):
            assert fy_overlap.run_date() == pendulum.date(want_date.year, want_date.month, want_date.day)
            assert get_fy_prefix() == want_fy
            assert fy_rollover.effective_fy_prefix("E:\\Data") == want_fy        # a non-FY source falls back to the calendar
            cfg_same = _cfg_fy(u4_paths, want_fy)
            assert fy_rollover.detect_rollover(cfg_same.paths.source_drive, cfg_same.paths.lan_destination) is False
            # a configured year one behind IS a rollover, one ahead is not decided by the server zone either
            behind = _oracle_fy(date(want_date.year - 1 if want_date.month >= 4 else want_date.year - 2, 6, 1))
            cfg_old = _cfg_fy(u4_paths, behind)
            assert fy_rollover.detect_rollover(cfg_old.paths.source_drive, cfg_old.paths.lan_destination) is True
        # (b) a scheduled instant, expressed in three zones: same IST day
        for zone in (UTC, ZoneInfo("America/Los_Angeles"), ZoneInfo("Asia/Kolkata")):
            rd = fy_overlap.run_date(instant.astimezone(zone))
            assert (rd.year, rd.month, rd.day) == (want_date.year, want_date.month, want_date.day)
        # (c) the overlap window follows the IST month
        rd = fy_overlap.run_date(instant)
        cfg = _cfg_fy(u4_paths, want_fy)
        assert fy_overlap.in_window(cfg, rd) is (want_date.month <= 3)        # early_start_month default 1; window = Jan..Mar


_CFG_CACHE: dict = {}


def _cfg_fy(tmp, fy: str) -> AppConfig:
    if fy not in _CFG_CACHE:
        _CFG_CACHE[fy] = AppConfig(
            paths={"source_drive": f"E:\\Data\\{fy}", "lan_destination": _unc("\\\\nas\\share", fy),
                   "runtime_dir": str(tmp / "rt")},
            wol={"enabled": False}, cloud={"enabled": False}, dashboard=dict(ui_login.DASHBOARD))
    return _CFG_CACHE[fy]


# ═════════════════════════════════════════════════════════════════════════════════════════════
# U6 - alert storm / silence over 60 nights x {cloud, lan} x {FY26-27, FY27-28}
# ═════════════════════════════════════════════════════════════════════════════════════════════

OUTCOMES = ["COMPLETE", "FAILED", "PARTIAL", "BLOCKED", "MISSED"]
FYS = ("FY26-27", "FY27-28")
COOLDOWN_H = 12                                      # alerts.repeat_cooldown_hours (default), written out here


def _drive_night(cfg_by_fy, db, leg: str, fy: str, night: int, outcome: str, when: pendulum.DateTime) -> str:
    """One night of one (leg, FY) through the same entry points the pipelines use. Returns the run id."""
    cfg = cfg_by_fy[fy]
    run_id = f"{leg}|{fy}|{night}"
    iso = when.isoformat()
    status = f"{leg.upper()}_{outcome}"
    db.insert_run({"run_id": run_id, "mode": leg, "fy": fy, "started_at": iso, "ended_at": iso, "status": status})
    if outcome == "COMPLETE":
        flow._send_recovery_if_needed(cfg, leg, run_id)
    elif outcome in ("FAILED", "PARTIAL"):
        flow._send_run_alert(cfg, leg, status, f"{leg} {outcome} on night {night}", 1, iso, run_id=run_id)
    elif outcome == "BLOCKED":       # the event flow._open_block_request builds (it needs a live guard record)
        from core import faults
        flow.notify(cfg, flow.AlertEvent(
            key=f"blocked:{fy}:{leg}", kind="ACTION_REQUIRED", mode=leg, fy=fy,
            title=f"{leg.upper()} backup blocked - approval needed", message="guard blocked",
            status=status, fault_code=faults.UNKNOWN, run_id=run_id, occurred_at=iso))
    else:                            # MISSED: the event flow._missed_if_no_budget builds
        flow.notify(cfg, flow.AlertEvent(
            key=f"missed:{fy}:{leg}", kind="WARNING", mode=leg, fy=fy,
            title=f"{leg.upper()} backup night missed", message="budget used up", status=status,
            occurred_at=iso, fault_code="AAM-SYS-07", run_id=run_id))
    return run_id


def _streaks(nights: list[str]) -> list[tuple[int, int]]:
    """(first, last) night index of every maximal run of non-COMPLETE nights - hand-computed."""
    out, start = [], None
    for i, o in enumerate(nights):
        if o != "COMPLETE" and start is None:
            start = i
        if o == "COMPLETE" and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(nights) - 1))
    return out


_NIGHTS = st.lists(st.sampled_from(OUTCOMES), min_size=60, max_size=60)


@settings(max_examples=3, deadline=None, suppress_health_check=list(HealthCheck))
@given(plans=st.fixed_dictionaries({f"{leg}|{fy}": _NIGHTS for leg in ("cloud", "lan") for fy in FYS}))
@example(plans={f"{leg}|{fy}": ["FAILED"] * 60 for leg in ("cloud", "lan") for fy in FYS})         # a month of flapping NAS
@example(plans={f"{leg}|{fy}": (["FAILED", "PARTIAL", "BLOCKED", "MISSED"] * 15) for leg in ("cloud", "lan") for fy in FYS})
@example(plans={f"{leg}|{fy}": (["FAILED", "FAILED", "COMPLETE"] * 20) for leg in ("cloud", "lan") for fy in FYS})
def test_u6_storm_and_silence_bounds_from_the_smtp_sink(tmp_path_factory, plans):
    tmp = tmp_path_factory.mktemp("u6")
    base = pendulum.datetime(2027, 1, 4, 21, 0, tz=IST)
    with smtp_server() as srv:
        cfg_by_fy = {fy: _cfg(tmp, fy, srv.server_address[1], alerts={"repeat_cooldown_hours": COOLDOWN_H}) for fy in FYS}
        db = ManifestDB.from_config(cfg_by_fy[FYS[0]])
        try:
            for night in range(60):                                       # the fake clock: one step per night
                when = base.add(days=night)
                with pendulum.travel_to(when, freeze=True):
                    for key, nights in plans.items():
                        leg, fy = key.split("|")
                        _drive_night(cfg_by_fy, db, leg, fy, night, nights[night], when)
        finally:
            db.close()
        mails = list(srv.messages)

    # classify every captured mail by the run it is about (X-AAM-Reference = our run id) and by its subject
    problem: dict[str, list[int]] = {k: [] for k in plans}
    recovered: dict[str, list[int]] = {k: [] for k in plans}
    for m in mails:
        ref = _header(m, "X-AAM-Reference")
        leg, fy, night = ref.split("|")
        k = f"{leg}|{fy}"
        (recovered if "resolved:" in _subject(m).lower() else problem)[k].append(int(night))

    for key, nights in plans.items():
        streaks = _streaks(nights)
        failing = sum(1 for o in nights if o != "COMPLETE")
        # storm: never more than one problem mail per failing night, and never more than the cooldown formula allows
        assert len(problem[key]) <= failing, key
        assert len(set(problem[key])) == len(problem[key]), f"{key}: two problem mails for one night"
        bound = sum(1 + math.ceil(24 * (last - first + 1) / COOLDOWN_H) for first, last in streaks) + len(recovered[key])
        assert len(problem[key]) <= bound, key
        # silence: every failure streak produced at least one mail inside the streak
        for first, last in streaks:
            assert any(first <= n <= last for n in problem[key]), f"{key}: streak {first}-{last} got no mail"
        # recoveries: at most one per night, only on a COMPLETE night that directly follows a failing night
        assert len(set(recovered[key])) == len(recovered[key]), key
        for n in recovered[key]:
            assert nights[n] == "COMPLETE" and n > 0 and nights[n - 1] != "COMPLETE", (key, n)
        assert len(recovered[key]) <= len(streaks)


# ═════════════════════════════════════════════════════════════════════════════════════════════
# U7 - the mail path is down for 3 days: the dashboard says so
# ═════════════════════════════════════════════════════════════════════════════════════════════

def test_u7_three_failed_nights_with_no_mail_server_show_a_red_banner(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, "FY26-27", _dead_port())
    cfg.lan.enabled = True
    db = ManifestDB.from_config(cfg)
    real_now = pendulum.now(IST)
    try:
        for night, hours_ago in enumerate((48, 24, 0)):
            when = real_now.subtract(hours=hours_ago)
            with pendulum.travel_to(when, freeze=True):
                _drive_night({"FY26-27": cfg}, db, "lan", "FY26-27", night, "FAILED", when)
        # oracle 1: raw sqlite - three delivery attempts, all FAILED, each with a reason
        raw = sqlite3.connect(cfg.paths.database_path)
        rows = raw.execute("SELECT status, error FROM notifications ORDER BY id").fetchall()
        raw.close()
        assert [r[0] for r in rows] == ["FAILED"] * 3 and all(r[1] for r in rows)

        monkeypatch.setattr(ui, "_cfg", lambda: cfg)
        monkeypatch.setattr(ui, "get_db", lambda: db)
        monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
        monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
        ui._RATE_LIMITS.clear()
        client = TestClient(ui.app, raise_server_exceptions=False, follow_redirects=False,
                            headers={"Accept": "application/json"}, cookies={"session": ui_login.SESSION_TOKEN})
        data = client.get("/status").json()
        banner = [i for i in data["attention"] if i["code"] == "alerts_undelivered"]
        assert len(banner) == 1 and banner[0]["level"] == "red"
        assert "could not be delivered" in banner[0]["message"] and "3 alert/report email(s)" in banner[0]["message"]
        health = client.get("/health")
        assert health.status_code == 503 and any("could not be delivered" in r for r in health.json()["reasons"])
    finally:
        db.close()


# ═════════════════════════════════════════════════════════════════════════════════════════════
# U11 - invalid config at boot: who alerts?
# ═════════════════════════════════════════════════════════════════════════════════════════════

class _StopLoop(BaseException):
    """Raised by the patched sleep to leave watchdog.main after exactly one iteration."""


def _config_files(tmp_path, port: int):
    base = {
        "firm_name": "AAM",
        "paths": {"source_drive": str(tmp_path / "FY26-27"), "lan_destination": "\\\\nas\\share\\FY26-27",
                  "runtime_dir": str(tmp_path / "rt")},
        "wol": {"enabled": False}, "cloud": {"enabled": False},
        "notifications": _notif(port), "dashboard": dict(ui_login.DASHBOARD),
    }
    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump(base), encoding="utf-8")
    bad_doc = {**base, "lan": {"foo": 1}}                                    # an unknown key: `lan.foo`
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(bad_doc), encoding="utf-8")
    return good, bad


@pytest.fixture
def watchdog_rig(monkeypatch, tmp_path):
    import watchdog
    monkeypatch.setattr(watchdog, "_configure_logging", lambda: None)        # never create C:\BackupAgent\logs
    monkeypatch.setattr(watchdog, "LOG_DIR", watchdog.LOG_DIR)               # restore whatever main() may set
    monkeypatch.setattr(watchdog, "BACKUP_LOCK_PATH", watchdog.BACKUP_LOCK_PATH)
    monkeypatch.setattr(watchdog, "_check_health", lambda: True)
    monkeypatch.setattr(watchdog, "_service_state", lambda service: "RUNNING")

    def _stop(_seconds):
        raise _StopLoop

    monkeypatch.setattr(watchdog.time, "sleep", _stop)
    monkeypatch.setattr(watchdog, "_attention_raised", False)
    return watchdog


def _point_config_at(monkeypatch, path):
    import models.config as mc
    monkeypatch.setattr(mc, "CONFIG_PATH", str(path))


def test_u11_control_a_valid_config_lets_the_watchdog_reach_the_mail_sink(watchdog_rig, monkeypatch, tmp_path):
    with smtp_server() as srv:
        good, _bad = _config_files(tmp_path, srv.server_address[1])
        _point_config_at(monkeypatch, good)
        watchdog_rig._notify({"key": "watchdog:probe", "kind": "ACTION_REQUIRED", "title": "probe",
                              "message": "control", "status": "PROBE", "fault_code": "AAM-SVC-01"})
        assert len(srv.messages) == 1                                         # the sink and the path work


def test_u11_watchdog_keeps_running_with_an_invalid_config(watchdog_rig, monkeypatch, tmp_path):
    with smtp_server() as srv:
        _good, bad = _config_files(tmp_path, srv.server_address[1])
        _point_config_at(monkeypatch, bad)
        with pytest.raises(_StopLoop):                      # reached the loop's sleep: it did not exit or raise
            watchdog_rig.main()


def test_u11_launch_dies_before_prefect_on_an_invalid_config(monkeypatch, tmp_path):
    import launch
    import pydantic
    from models.config import load_config
    with smtp_server() as srv:
        good, bad = _config_files(tmp_path, srv.server_address[1])
        assert load_config(str(good)).firm_name == "AAM"                  # control: the same file minus the typo loads
        _point_config_at(monkeypatch, bad)
        api = MagicMock(return_value=True)
        monkeypatch.setattr(launch, "_check_prefect_api", api)
        with pytest.raises(pydantic.ValidationError, match="foo"):        # an uncaught error = non-zero exit under NSSM
            launch.main()
        api.assert_not_called()                                           # died before touching Prefect
        assert srv.messages == []


def test_u11_invalid_config_sends_one_action_required_mail(watchdog_rig, monkeypatch, tmp_path):
    with smtp_server() as srv:
        _good, bad = _config_files(tmp_path, srv.server_address[1])
        _point_config_at(monkeypatch, bad)
        with pytest.raises(_StopLoop):
            watchdog_rig.main()
        assert len(srv.messages) == 1
        assert "backup" in _subject(srv.messages[0]).lower()


def test_u11_invalid_config_mail_is_throttled_across_iterations(watchdog_rig, monkeypatch, tmp_path):
    with smtp_server() as srv:
        _good, bad = _config_files(tmp_path, srv.server_address[1])
        _point_config_at(monkeypatch, bad)
        for _ in range(2):
            with pytest.raises(_StopLoop):
                watchdog_rig.main()
        assert len(srv.messages) == 1


# ═════════════════════════════════════════════════════════════════════════════════════════════
# U16 - folder derivation never escapes (fy_overlap.active_folders)
# ═════════════════════════════════════════════════════════════════════════════════════════════

SOURCE_PARENTS = ["E:\\", "E:\\Data", "D:\\a b\\c"]
NAS_PARENTS = ["\\\\nas\\share", "\\\\nas\\share\\x"]


def _fy_name(start: int) -> str:
    return f"FY{start % 100:02d}-{(start + 1) % 100:02d}"


def _hand_calendar_fy(d: date) -> str:
    return _oracle_fy(d)


@pytest.fixture(scope="module")
def u16_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("u16")


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(src_parent=st.sampled_from(SOURCE_PARENTS), nas_parent=st.sampled_from(NAS_PARENTS),
       start=st.integers(20, 98), offset_days=st.integers(-420, 800), overlap=st.booleans(),
       activated=st.booleans())
def test_u16_active_folders_never_escape_the_configured_parents(u16_dir, src_parent, nas_parent, start, offset_days,
                                                               overlap, activated):
    configured = _fy_name(start)
    source = ntpath.join(src_parent, configured)
    nas = ntpath.join(nas_parent, configured)
    cfg = AppConfig(paths={"source_drive": source, "lan_destination": nas, "runtime_dir": str(u16_dir / "rt")},
                    wol={"enabled": False}, cloud={"enabled": False}, dashboard=dict(ui_login.DASHBOARD),
                    rollover={"overlap_enabled": overlap})
    run_date = date(2000 + start, 4, 1) + timedelta(days=offset_days)
    db = ManifestDB(str(u16_dir / f"m{start}{int(activated)}.db"))
    try:
        nxt = _fy_name(start + 1)
        if activated:
            db.record_activation(nxt, activated_at="2020-01-01T00:00:00+05:30", source_path="x", created_by_app=True)
        folders = fy_overlap.active_folders(cfg, db, pendulum.date(run_date.year, run_date.month, run_date.day))
    finally:
        db.close()

    # hand oracle of WHICH folders: the early year joins only with the switch on, an activation row, and either
    # January..March of the configured year or the (not yet switched) first days of the next one
    cal = _hand_calendar_fy(run_date)
    joins = overlap and activated and ((cal == configured and run_date.month <= 3) or cal == nxt)
    assert folders == ([nxt, configured] if joins else [configured])

    seen_src, seen_nas, seen_prefix = set(), set(), set()
    for fy in folders:
        view = fy_overlap.folder_view(cfg, fy)
        s_head, s_leaf = ntpath.split(view.paths.source_drive)
        n_head, n_leaf = ntpath.split(view.paths.lan_destination)
        prefix = fy_rollover.effective_fy_prefix(view.paths.source_drive)            # the cloud prefix of that folder
        assert s_leaf == fy == n_leaf == prefix
        assert s_head == ntpath.split(source)[0] and n_head == ntpath.split(nas)[0]    # same configured parents
        drive, rest = ntpath.splitdrive(view.paths.lan_destination)
        assert rest.strip("\\") == (n_head[len(drive):].strip("\\") + "\\" + fy).strip("\\")   # nothing above the parent
        assert view.paths.lan_destination.rstrip("\\") != drive.rstrip("\\")          # never the share root itself
        assert view.paths.source_drive.rstrip("\\") != ntpath.splitdrive(view.paths.source_drive)[0].rstrip("\\")
        seen_src.add(view.paths.source_drive.lower())
        seen_nas.add(view.paths.lan_destination.lower())
        seen_prefix.add(prefix)
    assert len(seen_src) == len(seen_nas) == len(seen_prefix) == len(folders)


# ═════════════════════════════════════════════════════════════════════════════════════════════
# U19 - weekly report during the overlap when one FY never ran
# ═════════════════════════════════════════════════════════════════════════════════════════════

def test_u19_weekly_report_does_not_say_all_fine_when_the_early_fy_never_ran(tmp_path):
    A, B = "FY26-27", "FY27-28"
    cfg = _cfg(tmp_path, A, schedule={"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *"})
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        db.record_activation(B, activated_at="2027-01-01T09:00:00+05:30", source_path="x", created_by_app=True)
        day = pendulum.datetime(2027, 1, 11, tz=IST)                               # Mon 11 Jan .. Sun 17 Jan
        for n in range(7):
            for leg, hour in (("cloud", 2), ("lan", 21)):
                t = day.add(days=n).at(hour, 0).isoformat()
                db.insert_run({"run_id": f"{A}-{leg}-{n}", "mode": leg, "fy": A, "started_at": t, "ended_at": t,
                               "status": f"{leg.upper()}_COMPLETE", "files_copied": 5, "bytes_copied": 50})
        rp = ReportPeriod.previous_week(pendulum.datetime(2027, 1, 18, 8, tz=IST))
        with patch.object(fy_overlap, "run_date", return_value=pendulum.date(2027, 1, 20)):
            subject, body, _att = report.build_summary_email(
                db, "Firm", 7, "Weekly", legs=("cloud", "lan"), report_period=rp,
                schedule=cfg.schedule, config=cfg)
    finally:
        db.close()
    text = re.sub(r"<[^>]+>", " ", body)
    # hand oracle: FY27-28 is named, as not backed up, for BOTH legs; the subject asks for action, never "OK"
    assert "action needed" in subject.lower()
    assert "all backups ok" not in subject.lower() and "all backups ok" not in text.lower()
    assert "FY27-28" in subject                                                    # the headline names the folder
    flat = re.sub(r"\s+", " ", text)
    # the per-leg cards come before the summary block; cut them at the first "Open items"
    cards = flat[:flat.index("Open items")]
    early = cards[cards.index("FY27-28"):cards.index("FY26-27")]                  # the FY27-28 section
    current = cards[cards.index("FY26-27"):]
    for leg_name in ("Online backup (Google Cloud)", "Office NAS backup"):
        assert re.search(re.escape(leg_name) + r" 0 of 0 runs OK", early), leg_name
    assert early.count("No backup ran in this period") == 2                         # one per leg
    assert current.count("7 of 7 runs OK") == 2 and "No backup ran" not in current   # the healthy year is not blamed


# ═════════════════════════════════════════════════════════════════════════════════════════════
# U23 - an approval does not bypass guards that changed after it was given
# ═════════════════════════════════════════════════════════════════════════════════════════════

BASE_DEST = {"cloud": "gs://aam-a/FY26-27", "lan": "\\\\nas\\share\\FY26-27"}


def _signed_client() -> TestClient:
    return TestClient(ui.app, raise_server_exceptions=False, follow_redirects=False,
                      headers={"Accept": "application/json"}, cookies={"session": ui_login.SESSION_TOKEN})


class _U23:
    """A blocked leg with an approval on the dashboard, then a changed world and the re-run."""

    def __init__(self, tmp_path, monkeypatch, leg: str):
        self.leg = leg
        src = tmp_path / "FY26-27"
        src.mkdir()
        (src / "ledger.xlsx").write_text("x")
        self.cfg = AppConfig(
            paths={"source_drive": str(src), "lan_destination": "\\\\nas\\share\\FY26-27",
                   "runtime_dir": str(tmp_path / "rt")},
            wol={"enabled": False}, cloud={"enabled": False, "bucket": "aam-a"},
            dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD})
        self.cfg.cloud.enabled = True
        self.db = ManifestDB.from_config(self.cfg)
        self.db.insert_run({
            "run_id": "base", "mode": leg, "fy": "FY26-27", "started_at": "2027-01-01T21:00:00+05:30",
            "ended_at": "2027-01-01T21:05:00+05:30", "status": f"{leg.upper()}_COMPLETE",
            "extended_metrics": json.dumps({"guards": {"fy": "FY26-27", "source_file_count": 400,
                                                       "destination": BASE_DEST[leg]}})})
        self.transfers: list[dict] = []
        self.deployments: list[dict] = []
        self.monkeypatch = monkeypatch

        async def fake_arun(name, parameters=None, timeout=None):
            self.deployments.append({"name": name, "parameters": parameters})
            return SimpleNamespace(id="flow-run-1", state=None)

        monkeypatch.setattr(ui, "_cfg", lambda: self.cfg)
        monkeypatch.setattr(ui, "get_db", lambda: self.db)
        monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
        monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
        monkeypatch.setattr(ui, "arun_deployment", fake_arun)
        monkeypatch.setattr(ui, "_active_fy_list", lambda: ["FY26-27"])
        monkeypatch.setattr(ui, "_configured_fy", lambda: "FY26-27")
        ui._RATE_LIMITS.clear()

    # -- the guard task with fixed measurements: 400 source files, 1000 at the destination, 600 planned deletes
    def run_guards(self, approval_id=None, run_id="run-1"):
        if self.leg == "cloud":
            def count(target, *a, **k):
                return 400 if target == self.cfg.paths.source_drive else 1000

            def sync(**kw):
                self.transfers.append(kw)
                return {"status": "CLOUD_COMPLETE", "error": None, "planned_deletes": 600}

            @__import__("contextlib").contextmanager
            def fake_conf(*a, **k):
                yield "conf"

            with patch("flow.temp_rclone_config", fake_conf), patch("flow.count_objects", side_effect=count), \
                    patch("flow.run_cloud_sync", side_effect=sync):
                return flow.cloud_guards_task.fn(self.cfg, "FY26-27", None, approval_id=approval_id, run_id=run_id)
        dry = {"counts": {"total": 400, "copied": 0, "skipped": 400, "mismatch": 0, "failed": 0, "extras": 600},
               "planned_deletes": 600, "canary_present": True}
        with patch("flow.guards.resolve_host", return_value={"10.0.0.9"}), \
                patch("flow.guards.local_addresses", return_value={"127.0.0.1", "10.0.0.5"}), \
                patch("flow.run_lan_sync", side_effect=lambda **kw: self.transfers.append(kw)):
            return flow.lan_guards_task.fn(self.cfg, "FY26-27", dry, 1000, approval_id=approval_id, run_id=run_id)

    def block_and_approve(self) -> str:
        with pytest.raises(guards.GuardBlocked) as first:
            self.run_guards(run_id="run-b")
        assert [r["guard"] for r in first.value.record["results"] if r["verdict"] == guards.BLOCK] == ["G3_MASS_DELETE"]
        flow._open_block_request(self.cfg, self.leg, "run-b", first.value.record, now_iso())
        (req,) = self.db.open_approvals()
        resp = _signed_client().post(f"/approvals/{req['approval_id']}/approve", json={"phrase": "APPROVE"})
        assert resp.status_code in (200, 202), resp.text
        assert self.db.get_approval(req["approval_id"])["status"] == ap.APPROVED
        assert self.deployments[-1]["parameters"]["approval_id"] == req["approval_id"]
        return req["approval_id"]

    def change_world(self):
        if self.leg == "cloud":
            self.cfg.cloud.bucket = "aam-b"                          # bucket aam-a -> aam-b
        else:
            self.cfg.paths.lan_destination = "\\\\nas\\other\\FY26-27"   # another share


def _real_transfers(rig: _U23) -> list:
    if rig.leg == "cloud":
        return [t for t in rig.transfers if not t.get("dry_run")]
    return list(rig.transfers)


@pytest.mark.parametrize("leg", ["cloud", "lan"])
def test_u23_approval_does_not_bypass_a_guard_that_changed_afterwards(tmp_path, monkeypatch, leg):
    rig = _U23(tmp_path, monkeypatch, leg)
    try:
        approval_id = rig.block_and_approve()
        rig.change_world()
        with pytest.raises(guards.GuardBlocked) as again:                     # re-measured: blocks again
            rig.run_guards(approval_id=approval_id, run_id="run-2")
        blocked = {r["guard"] for r in again.value.record["results"] if r["verdict"] == guards.BLOCK}
        assert "G4_DESTINATION_IDENTITY" in blocked and "G3_MASS_DELETE" in blocked
        assert not again.value.record.get("approved_guards")                  # nothing was overridden
        assert again.value.record.get("approval_rejected")                    # the dashboard can say why
        # the approval was not consumed, and no transfer was started (cloud: only --dry-run measurements ran)
        used = rig.db.get_approval(approval_id)
        assert used["status"] != ap.USED and not used.get("used_at")
        assert _real_transfers(rig) == []
        # the block opens a NEW approval that names the changed destination; the old one is still re-checked if ever used
        flow._open_block_request(rig.cfg, leg, "run-2", again.value.record, now_iso())
        pending = [r for r in rig.db.open_approvals() if r["status"] == ap.PENDING]
        assert len(pending) == 1 and pending[0]["approval_id"] != approval_id
        assert "G4_DESTINATION_IDENTITY" in {g["guard"] for g in pending[0]["guards"]}
        # (the old approval stays APPROVED-unused until it expires; it is re-checked, never trusted, when used)
        assert not rig.db.get_approval(approval_id).get("used_at")
    finally:
        rig.db.close()


@pytest.mark.parametrize("leg", ["cloud", "lan"])
def test_u23_control_an_unchanged_world_lets_the_approval_through_exactly_once(tmp_path, monkeypatch, leg):
    rig = _U23(tmp_path, monkeypatch, leg)
    try:
        approval_id = rig.block_and_approve()
        record = rig.run_guards(approval_id=approval_id, run_id="run-2")        # same world: G3 overridden once
        assert record["approved_guards"] == ["G3_MASS_DELETE"]
        assert rig.db.get_approval(approval_id)["status"] == ap.USED
        with pytest.raises(guards.GuardBlocked):                                # a third run cannot reuse it
            rig.run_guards(approval_id=approval_id, run_id="run-3")
    finally:
        rig.db.close()
