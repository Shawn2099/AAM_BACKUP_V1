"""B7 - attention items of the next financial year (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, section 2.5).

One test per row of docs/TESTMATRIX_B7_2026-11-24.md: T01-T46 and T50-T54. T47/T48 live in
tests/test_fy_overlap_calendar.py, T49 is the regression run of the existing files, the C-rows belong to C2.

Layer U = core.attention.compute_attention / leg_timing, W = watchdog._check_stale_legs. The database (real
SQLite), the config (real AppConfig) and the FY maths are real; the clock is pendulum.travel_to. The watchdog
rows (T39-T42) use the real notify() with a local SMTP server; T43/T44 record the events instead.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pendulum
import pytest
import yaml

import watchdog
from core import fy_overlap, overdue
from core import notify as nt
from core.attention import AMBER, RED, compute_attention, leg_timing
from core.health import CLOCK_OK
from core.manifest import ManifestDB
from models.config import AppConfig, load_config
from test_support import attention_cfg
from test_support.sandbox_config import _config_dict
from test_support.smtp_server import smtp_server

import time_machine  # noqa: E402,F401  hard import: a missing package must fail loudly, never skip the file (pendulum.travel_to needs it)

TZ = "Asia/Kolkata"
OWNER_SRC = "E:\\FY26-27"
OWNER_NAS = "\\\\nas\\backup\\FY26-27"
ACTIVATED = "2026-12-31T06:00:00+05:30"
NEXT_SRC = "E:\\FY27-28"
NEXT_NAS = "\\\\nas\\backup\\FY27-28"
SWITCHED = "2027-04-02T06:10:00+05:30"

CLOUD_OK, CLOUD_FAIL, CLOUD_MISS = "CLOUD_COMPLETE", "CLOUD_FAILED", "CLOUD_MISSED"
CLOUD_PART, CLOUD_RUN, CLOUD_BLOCKED = "CLOUD_PARTIAL", "CLOUD_RUNNING", "CLOUD_BLOCKED"
LAN_OK, LAN_FAIL, LAN_RUN, LAN_BLOCKED = "LAN_COMPLETE", "LAN_FAILED", "LAN_RUNNING", "LAN_BLOCKED"
AUDIT_OK, AUDIT_BAD = "VERIFIED", "VERIFICATION_FAILED"


# ── helpers ───────────────────────────────────────────────────────────────

def at(*parts):
    """A moment in IST: at(2026, 12, 31, 6) = 31 Dec 2026 06:00."""
    return pendulum.datetime(*parts, tz=TZ)


def iso(moment) -> str:
    return moment.isoformat()


def deadline(started, cron="0 18 * * *", budget=6, grace=2):
    """overdue.deadline for a leg whose last success started at `started` (cloud 18:00 by default)."""
    start = pendulum.parse(started) if isinstance(started, str) else started
    return pendulum.instance(overdue.deadline(cron, TZ, start, budget, grace))


def _cfg(tmp_path: Path, *, src=OWNER_SRC, nas=OWNER_NAS, overlap=True, activate_on=None, cloud=True,
         lan=True, cloud_scheduled=True, lan_scheduled=True, smtp_port=None) -> AppConfig:
    (tmp_path / "rt").mkdir(parents=True, exist_ok=True)
    key = tmp_path / "keys" / "sandbox-key.json"                 # an enabled cloud leg needs a key file
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text('{"type": "service_account", "sandbox": true}', encoding="utf-8")
    rollover = {"overlap_enabled": overlap}
    if activate_on:
        rollover["overlap_activate_on"] = activate_on
    notifications = {"recipients": ["o@firm.example"]}
    if smtp_port:
        notifications.update({"smtp_host": "127.0.0.1", "smtp_port": smtp_port, "sender": "b@firm.example",
                              "smtp_security": "none", "smtp_auth": False, "retry_attempts": 1,
                              "retry_base_seconds": 0.01})
    return AppConfig(
        paths={"source_drive": src, "lan_destination": nas, "runtime_dir": str(tmp_path / "rt"),
               "gcs_key_path": str(key)},
        wol={"enabled": False},
        cloud={"enabled": cloud, "scheduled": cloud_scheduled, "bucket": "aam-test-bucket",
               "project_number": "0"},
        lan={"enabled": lan, "scheduled": lan_scheduled},
        dashboard={"public_url": "http://backup-server:8080"},
        notifications=notifications,
        alerts={"stale_repeat_hours": 6},
        rollover=rollover,
    )


def _overlap_off(cfg: AppConfig) -> AppConfig:
    return cfg.model_copy(update={"rollover": cfg.rollover.model_copy(update={"overlap_enabled": False})})


def _site(tmp_path: Path, *, overlap: bool, next_files=(), lan=True) -> AppConfig:
    """Owner-style folders in a temp root: FY26-27 (the configured source) and FY27-28 (with `next_files`)."""
    src = tmp_path / "src"
    (src / "FY26-27").mkdir(parents=True, exist_ok=True)
    nxt = src / "FY27-28"
    nxt.mkdir(parents=True, exist_ok=True)
    for name in next_files:
        (nxt / name).write_bytes(b"x")
    return _cfg(tmp_path, src=str(src / "FY26-27"), overlap=overlap, lan=lan)


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    d.set_meta_created_at("2026-01-01T00:00:00+05:30")
    yield d
    d.close()


def _fresh_db(tmp_path: Path, name: str) -> ManifestDB:
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    return ManifestDB(folder / "m.db")


@contextmanager
def _db_of(cfg: AppConfig):
    """The database the watchdog opens for this config (ManifestDB.from_config, the same file)."""
    d = ManifestDB.from_config(cfg)
    try:
        yield d
    finally:
        d.close()


def add_run(db, run_id, mode, status, started, *, bytes_copied=0, metrics=None):
    db.insert_run({"run_id": run_id, "mode": mode, "started_at": iso(started), "ended_at": iso(started),
                   "status": status, "exit_code": 0, "bytes_copied": bytes_copied,
                   "extended_metrics": None if metrics is None else json.dumps(metrics)})


def add_audit(db, audit_id, mode, status, started, detail=None):
    db.record_audit({"audit_id": audit_id, "mode": mode, "started_at": iso(started),
                     "ended_at": iso(started), "status": status, "detail": detail})


def activate(db, activated=ACTIVATED, fy="FY27-28", **extra):
    """The activation record the daily check writes on 31 Dec (plan 3, step 4)."""
    db.set_overlap_record(fy, {"activated_at": activated, "from_fy": "FY26-27",
                               "source_folder": NEXT_SRC if fy == "FY27-28" else "E:\\" + fy, "clock": {},
                               **extra})


def mail(db, key, kind, created, mode="rollover", status="SENT"):
    db.record_notification({"event_key": key, "kind": kind, "mode": mode, "subject": "subject",
                            "status": status, "attempts": 1, "created_at": created,
                            "fault_code": "AAM-ROL-07" if key.startswith("rollover:") else None})


def items(db, cfg, when):
    """compute_attention at `when` (the clock is frozen there, as the dashboard and /health see it)."""
    with pendulum.travel_to(when, freeze=True):
        return compute_attention(db, cfg, now=when)


def codes(db, cfg, when):
    return [i["code"] for i in items(db, cfg, when)]


def only(found, code):
    """The one item with this code (fails when there are none or several)."""
    hits = [i for i in found if i["code"] == code]
    assert len(hits) == 1, (code, [i["code"] for i in found])
    return hits[0]


def next_items(found):
    return [i for i in found if "FY27-28" in i["code"]]


NIGHTS = (("1230", at(2026, 12, 30, 1)), ("1231", at(2026, 12, 31, 1)),
          ("0101", at(2027, 1, 1, 1)), ("0102", at(2027, 1, 2, 1)))   # NAS 01:00 nights, 30 Dec - 2 Jan


def lan_nightly(db):
    """Configured-FY NAS backups every night 30 Dec - 2 Jan (succeeding)."""
    for name, started in NIGHTS:
        add_run(db, f"l-lan-{name}", "lan", LAN_OK, started)


def lan_failing(db, *, next_fy=True):
    """Configured-FY NAS backups failing every night; FY27-28 failing too when `next_fy`."""
    for name, started in NIGHTS:
        add_run(db, f"l-lan-{name}", "lan", LAN_FAIL, started)
    if next_fy:
        for name, started in NIGHTS[2:]:
            add_run(db, f"l-lan-{name}-FY27-28", "lan", LAN_FAIL, started)


def cloud_failing(db, *, next_fy=True):
    """Configured and FY27-28 cloud backups failing every night (31 Dec and 1 Jan at 18:00)."""
    for when, name in ((at(2026, 12, 31, 18), "1231"), (at(2027, 1, 1, 18), "0101")):
        add_run(db, f"c-cloud-{name}", "cloud", CLOUD_FAIL, when)
        if next_fy:
            add_run(db, f"c-cloud-{name}-FY27-28", "cloud", CLOUD_FAIL, when)


NOW2 = at(2027, 1, 2, 12)     # the watchdog rows: the NAS deadline of FY27-28 has long passed


# ── U: rows that must hold on the baseline too ────────────────────────────

def test_T01_no_record_gives_baseline_items(tmp_path, db):
    cfg = _cfg(tmp_path)
    add_run(db, "c-cloud-1229", "cloud", CLOUD_OK, at(2026, 12, 29, 18))
    add_run(db, "c-lan-1230", "lan", LAN_OK, at(2026, 12, 30, 1))
    when = at(2026, 12, 30, 12)
    found = items(db, cfg, when)
    assert found == items(db, _overlap_off(cfg), when)
    assert not [i for i in found if "FY" in i["code"]]


def test_T02_overlap_off_without_record_is_baseline(tmp_path, db):
    on, off = _cfg(tmp_path), _cfg(tmp_path, overlap=False)
    add_run(db, "c-cloud-1229", "cloud", CLOUD_OK, at(2026, 12, 29, 18))
    add_run(db, "c-lan-1230", "lan", LAN_OK, at(2026, 12, 30, 1))
    when = at(2026, 12, 30, 12)
    found = items(db, off, when)
    assert found == items(db, on, when)
    assert not [i for i in found if i["code"].startswith("overlap_off:")]


def test_T03_config_without_overlap_keys_is_baseline(tmp_path):
    cfg = SimpleNamespace(
        cloud=SimpleNamespace(enabled=True), lan=SimpleNamespace(enabled=True),
        dashboard=SimpleNamespace(stale_grace_hours=attention_cfg.GRACE_HOURS, alert_failure_window_days=7,
                                  catching_up_max_days=14, public_url="http://backup-server:8080", port=8080,
                                  bind_address="127.0.0.1"),
        firm_name="T", notifications=SimpleNamespace(send_on_failure=True),
        schedule=attention_cfg.SCHEDULE, budget=attention_cfg.BUDGET)     # no `rollover`, no `paths`
    db = MagicMock()
    db.last_run.return_value = None
    db.last_successful_run.return_value = None
    db.latest_audit.return_value = None
    db.open_approvals.return_value = []
    db.latest_notification_for_mode.return_value = None
    db.notification_failures_since.return_value = []
    db.last_config_change_at.return_value = None
    db.first_known_at.return_value = None
    db.last_sent_at.return_value = None
    db.overlap_record.return_value = {"activated_at": ACTIVATED}          # a record is there anyway
    with pendulum.travel_to(at(2026, 12, 31, 12), freeze=True):
        found = compute_attention(db, cfg, now=at(2026, 12, 31, 12))
    assert isinstance(found, list)
    assert not [i for i in found if "FY" in i["code"]]


def test_T04_missing_key_counts_as_enabled(tmp_path):
    d = _config_dict(tmp_path)                        # a loaded config: no rollover.overlap_enabled key
    assert "overlap_enabled" not in d.get("rollover", {})
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(d), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.rollover.overlap_enabled is True
    db = ManifestDB.from_config(cfg)
    try:
        activate(db)
        assert "first_run:cloud:FY27-28" in codes(db, cfg, at(2026, 12, 31, 12))
    finally:
        db.close()


# ── U: the next FY's own clock (B7 rows T05-T14) ──────────────────────────

def test_T05_next_fy_waiting_from_activation(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    found = items(db, cfg, at(2026, 12, 31, 12))
    assert only(found, "first_run:cloud:FY27-28")["level"] == AMBER
    assert only(found, "first_run:lan:FY27-28")["level"] == AMBER
    assert not [i for i in found if i["code"].startswith("stale:") and "FY27-28" in i["code"]]


def test_T06_next_fy_overdue_counted_from_activation(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    due = deadline(ACTIVATED)                         # cloud 18:00, 6 h budget, 2 h grace
    assert "stale:cloud:FY27-28" not in codes(db, cfg, due.subtract(seconds=60))
    item = only(items(db, cfg, due.add(seconds=60)), "stale:cloud:FY27-28")
    assert item["level"] == RED
    assert item.get("due_at")


def test_T07_next_fy_base_is_activation_not_system_birth(tmp_path, db):
    db.set_meta_created_at("2025-12-31T00:00:00+05:30")          # the system is a year old
    cfg = _cfg(tmp_path)
    activate(db)
    assert "stale:cloud:FY27-28" not in codes(db, cfg, at(2026, 12, 31, 12))


def test_T08_schedule_since_after_activation_wins(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    db.set_schedule_in_effect({"cloud_cron": "0 17 * * *", "lan_cron": "0 1 * * *", "timezone": TZ})
    db.set_schedule_in_effect({"cloud_cron": "0 18 * * *", "lan_cron": "0 1 * * *", "timezone": TZ},
                              since="2027-01-15T00:00:00+05:30")
    assert "stale:cloud:FY27-28" not in codes(db, cfg, at(2027, 1, 15, 12))
    due = deadline("2027-01-15T00:00:00+05:30")
    assert only(items(db, cfg, due.add(seconds=60)), "stale:cloud:FY27-28")["level"] == RED


def test_T09_leg_enabled_after_activation(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    db.record_config_change({"changed_at": "2027-01-10T09:00:00+05:30", "changed_by": "test",
                             "key": "lan.enabled", "old_value": "false", "new_value": "true", "risky": 0})
    assert "stale:lan:FY27-28" not in codes(db, cfg, at(2027, 1, 10, 12))
    due = deadline("2027-01-10T09:00:00+05:30", "0 1 * * *", 4, 2)
    assert only(items(db, cfg, due.add(seconds=60)), "stale:lan:FY27-28")["level"] == RED


def test_T10_record_without_activation_time_fails_closed(tmp_path, db):
    cfg = _cfg(tmp_path)
    for bad in ({}, {"activated_at": "garbage"}):
        db.set_overlap_record("FY27-28", bad)
        found = items(db, cfg, at(2026, 12, 31, 12))
        item = only(found, "stale:cloud:FY27-28")
        assert item["level"] == RED and "could not be placed on the schedule" in item["message"], bad
        assert "first_run:cloud:FY27-28" not in [i["code"] for i in found], bad


def test_T11_next_fy_success_clears_waiting(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    add_run(db, "c-cloud-FY27-28", "cloud", CLOUD_OK, at(2027, 1, 1, 18))
    found = codes(db, cfg, at(2027, 1, 2, 12))
    assert "first_run:cloud:FY27-28" not in found
    assert "stale:cloud:FY27-28" not in found


def test_T12_configured_success_does_not_cover_next_fy(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    for day in (30, 31):
        add_run(db, f"c-cloud-12{day}", "cloud", CLOUD_OK, at(2026, 12, day, 18))
    add_run(db, "c-cloud-0101", "cloud", CLOUD_OK, at(2027, 1, 1, 18))
    found = items(db, cfg, at(2027, 1, 2, 12))
    assert only(found, "stale:cloud:FY27-28")["level"] == RED
    assert "stale:cloud" not in [i["code"] for i in found]


def test_T13_next_fy_success_does_not_cover_configured(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    add_run(db, "c-cloud-1230", "cloud", CLOUD_OK, at(2026, 12, 30, 12))      # 3 days old
    add_run(db, "c-cloud-FY27-28", "cloud", CLOUD_OK, at(2027, 1, 1, 18))
    found = items(db, cfg, at(2027, 1, 2, 12))
    assert only(found, "stale:cloud")["level"] == RED
    assert "stale:cloud:FY27-28" not in [i["code"] for i in found]


def test_T14_legacy_rows_belong_to_configured_fy(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    add_run(db, "c-cloud-1230", "cloud", CLOUD_OK, at(2026, 12, 30, 18))                    # no metrics
    add_run(db, "c-cloud-1230b", "cloud", CLOUD_OK, at(2026, 12, 30, 18, 30), metrics={"guards": {}})
    found = items(db, cfg, at(2026, 12, 31, 12))
    assert only(found, "first_run:cloud:FY27-28")["level"] == AMBER
    assert "stale:cloud:FY27-28" not in [i["code"] for i in found]


# ── U: failures, missed and catching-up runs per FY (B7 rows T15-T23) ──────

def test_T15_both_fys_failing_give_two_sets(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    cloud_failing(db)
    found = items(db, cfg, at(2027, 1, 2, 12))
    codes_ = [i["code"] for i in found]
    for code in ("stale:cloud", "stale:cloud:FY27-28", "last_run:cloud", "last_run:cloud:FY27-28"):
        assert only(found, code)["level"] == RED, code
    assert codes_.index("stale:cloud") < codes_.index("stale:cloud:FY27-28")
    assert codes_.index("last_run:cloud") < codes_.index("last_run:cloud:FY27-28")


def test_T16_one_fy_ok_other_stale(tmp_path, db):
    cfg = _cfg(tmp_path, cloud=False)
    activate(db)
    lan_nightly(db)                                   # configured NAS FY OK; FY27-28 NAS never ran
    found = codes(db, cfg, at(2027, 1, 2, 12))
    legs = [c for c in found if "lan" in c or "cloud" in c]
    assert legs == ["stale:lan:FY27-28"]


def test_T17_next_fy_missed_is_amber(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    add_run(db, "c-cloud-FY27-28", "cloud", CLOUD_MISS, at(2026, 12, 31, 18))
    found = items(db, cfg, at(2027, 1, 1, 12))
    assert only(found, "last_run:cloud:FY27-28")["level"] == AMBER


def test_T18_next_fy_catching_up(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    add_run(db, "c-cloud-FY27-28", "cloud", CLOUD_PART, at(2026, 12, 31, 18), bytes_copied=5000,
            metrics={"budget": {"stopped": True}})
    when = at(2027, 1, 1, 12)
    found = items(db, cfg, when)
    assert only(found, "catching_up:cloud:FY27-28")["level"] == AMBER
    assert "stale:cloud:FY27-28" not in [i["code"] for i in found]
    configured = sorted(c for c in codes(db, cfg, when) if "FY27-28" not in c)
    assert configured == sorted(codes(db, _overlap_off(cfg), when))


def test_T19_next_fy_audit_item(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    add_audit(db, "aud-cloud-full-1230", "cloud", AUDIT_OK, at(2026, 12, 30, 10))
    add_audit(db, "aud-cloud-full-FY27-28", "cloud", AUDIT_BAD, at(2027, 1, 1, 10), detail="2 files differ")
    found = items(db, cfg, at(2027, 1, 2, 12))
    assert only(found, "audit:cloud:FY27-28")["level"] == RED
    assert "audit:cloud" not in [i["code"] for i in found]


def test_T20_nas_shutdown_read_from_last_pass(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    add_run(db, "l-lan-1231", "lan", LAN_OK, at(2026, 12, 31, 1))                             # no metrics
    add_run(db, "l-lan-FY27-28", "lan", LAN_OK, at(2027, 1, 1, 1),
            metrics={"nas_shutdown": {"outcome": "UNVERIFIED", "detail": "no answer"}})
    found = items(db, cfg, at(2027, 1, 1, 5))
    hits = [i for i in found if i["code"] == "nas_shutdown"]
    assert len(hits) == 1 and hits[0]["level"] == AMBER


def test_T21_clock_item_once(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    clock = {"clock": {"state": "UNVERIFIED", "reason": "no time source answered"}}
    add_run(db, "c-cloud-1231", "cloud", CLOUD_OK, at(2026, 12, 31, 18), metrics=clock)
    add_run(db, "c-cloud-FY27-28", "cloud", CLOUD_OK, at(2026, 12, 31, 18), metrics=clock)
    found = items(db, cfg, at(2027, 1, 1, 12))
    assert len([i for i in found if i["code"] == "clock"]) == 1


def test_T22_disabled_leg_has_no_items_for_any_fy(tmp_path, db):
    cfg = _cfg(tmp_path, lan=False)
    activate(db)
    found = codes(db, cfg, at(2027, 1, 2, 12))
    assert not [c for c in found if c.startswith(("stale:lan", "first_run:lan", "last_run:lan"))]


def test_T23_next_fy_recovery_clears_stale(tmp_path):
    cfg = _cfg(tmp_path, cloud=False)
    with _db_of(cfg) as db:
        activate(db)
        lan_nightly(db)
        when = at(2027, 1, 2, 12)
        assert "stale:lan:FY27-28" in codes(db, cfg, when)            # the T16 state
        add_run(db, "l-lan-FY27-28", "lan", LAN_OK, at(2027, 1, 2, 9))
        assert "stale:lan:FY27-28" not in codes(db, cfg, when)
        with pendulum.travel_to(when, freeze=True):                   # U + W: the watchdog agrees
            assert watchdog._check_stale_legs(cfg) == []
        assert db.last_notification("stale:lan:FY27-28") is None


def test_T24_items_disappear_after_switch(tmp_path, db):
    cfg = _cfg(tmp_path, src=NEXT_SRC, nas=NEXT_NAS)
    db.set_overlap_record("FY27-28", {"activated_at": ACTIVATED, "from_fy": "FY26-27",
                                      "source_folder": NEXT_SRC, "clock": {}, "ended_at": SWITCHED})
    found = items(db, cfg, at(2027, 4, 3, 12))
    codes_ = [i["code"] for i in found]
    assert not [c for c in codes_ if c.endswith((":FY27-28", ":FY28-29"))]
    assert not [c for c in codes_ if c.startswith("overlap_off:")]
    assert "rollover:activation:FY28-29" not in codes_


def test_T25_switch_pending_keeps_next_fy_items(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    found = items(db, cfg, at(2027, 4, 2, 12))                 # config still FY26-27, 2 Apr: switch blocked
    assert only(found, "stale:cloud:FY27-28")["level"] == RED
    assert not [i for i in found if i["code"].startswith("rollover:unprotected:")]


# ── U: overlap switched off with files in the next FY (B7 rows T26-T29) ──────

def test_T26_overlap_off_red_item(tmp_path, db):
    cfg = _site(tmp_path, overlap=False, next_files=("a.xlsx",), lan=False)
    activate(db)
    found = items(db, cfg, at(2026, 12, 31, 12))
    assert only(found, "overlap_off:FY27-28")["level"] == RED
    assert [i["code"] for i in next_items(found)] == ["overlap_off:FY27-28"]


def test_T27_overlap_off_placeholder_only_no_item(tmp_path, db):
    cfg = _site(tmp_path, overlap=False, next_files=("README_FY.txt",), lan=False)
    activate(db)
    assert not [c for c in codes(db, cfg, at(2026, 12, 31, 12)) if c.startswith("overlap_off:")]


def test_T28_overlap_off_after_end_no_item(tmp_path, db):
    cfg = _site(tmp_path, overlap=False, next_files=("a.xlsx",), lan=False)
    activate(db, ended_at=SWITCHED)
    assert not [c for c in codes(db, cfg, at(2026, 12, 31, 12)) if c.startswith("overlap_off:")]


def test_T29_overlap_off_not_doubled_with_unprotected(tmp_path, db):
    cfg = _site(tmp_path, overlap=False, next_files=("a.xlsx",), lan=False)
    activate(db)
    found = items(db, cfg, at(2027, 4, 2, 12))               # 2 Apr: the switch is pending
    assert [(i["code"], i["level"]) for i in next_items(found)] == [("rollover:unprotected:FY27-28", RED)]


# ── U: the activation waiting item (B7 rows T30-T36) ──────────────────────

def test_T30_activation_item_day_boundaries(tmp_path):
    cfg = _cfg(tmp_path)
    cases = [
        (at(2026, 12, 30, 23, 59), None, None),                        # the day before: nothing
        (at(2026, 12, 31, 0, 0), None, None),                          # activation day, check not run yet
        (at(2026, 12, 31, 6, 5), "2026-12-31T06:00:00+05:30", AMBER),  # the check ran and waits
        (at(2026, 12, 31, 23, 59), None, None),                        # activation day, no mail
        (at(2027, 1, 1, 0, 0), None, RED),                             # the day after: RED, mail or not
    ]
    for n, (when, mail_at, level) in enumerate(cases):
        d = _fresh_db(tmp_path, f"case{n}")
        try:
            if mail_at:
                mail(d, "rollover:activation:FY27-28", "WARNING", mail_at)
            got = [i["level"] for i in items(d, cfg, when) if i["code"] == "rollover:activation:FY27-28"]
            assert got == ([level] if level else []), when
        finally:
            d.close()


def test_T31_activation_item_uses_ist_day(tmp_path):
    cfg = _cfg(tmp_path)
    for n, (utc, level) in enumerate([("2026-12-31T18:29:00Z", None), ("2026-12-31T18:30:00Z", RED)]):
        d = _fresh_db(tmp_path, f"utc{n}")
        try:
            found = items(d, cfg, pendulum.parse(utc))
            got = [i["level"] for i in found if i["code"] == "rollover:activation:FY27-28"]
            assert got == ([level] if level else []), utc
        finally:
            d.close()


def test_T32_server_off_on_activation_day(tmp_path, db):
    cfg = _cfg(tmp_path)
    item = only(items(db, cfg, at(2027, 1, 1, 5)), "rollover:activation:FY27-28")
    assert item["level"] == RED and "the daily check has not run since" in item["message"]
    activate(db, activated="2027-01-01T06:00:00+05:30")
    later = items(db, cfg, at(2027, 1, 1, 6, 5))
    assert not [i for i in later if i["code"] == "rollover:activation:FY27-28"]
    assert only(later, "first_run:cloud:FY27-28")["level"] == AMBER


def test_T33_activation_item_names_the_reason_and_is_no_blocker(tmp_path, db):
    cfg = _cfg(tmp_path)
    mail(db, "rollover:activation:FY27-28", "WARNING", "2026-12-31T06:00:00+05:30")
    found = items(db, cfg, at(2026, 12, 31, 6, 5))
    item = only(found, "rollover:activation:FY27-28")
    assert item["level"] == AMBER
    assert "Backups of FY26-27 go on as usual; nothing waits for a backup or an approval" in item["message"]
    # NOT CHECKED: the matrix also wants the mail's reason in the text. notifications stores the subject and
    # the error only, not the mail body, so the reason cannot be quoted from the database yet (see the report).
    codes_ = [i["code"] for i in found]
    assert not [c for c in codes_ if c.startswith("approval:") or "blocked" in c]


def test_T34_no_activation_item_on_clock_behind_or_jump(tmp_path, db):
    cfg = _cfg(tmp_path)
    # Only the jump is testable through compute_attention: a clock BEHIND (FY25-26) is always before the activation
    # date, so the date check returns first and the classify_rollover guard for "behind" cannot be reached from here
    # (it is a defensive second line; the rollover check itself alerts on a clock that is behind).
    for when in (at(2028, 6, 1, 12),):                           # the clock says FY28-29
        assert not [c for c in codes(db, cfg, when) if c.startswith("rollover:activation:")], when


def test_T35_no_activation_item_when_off(tmp_path, db):
    cfg = _cfg(tmp_path, overlap=False)
    assert not [c for c in codes(db, cfg, at(2027, 1, 1, 12)) if c.startswith("rollover:activation")]


def test_T36_activation_item_custom_date(tmp_path):
    cfg = _cfg(tmp_path, activate_on="01-15")
    cases = [
        (at(2027, 1, 14, 12), None, None),
        (at(2027, 1, 15, 7), "2027-01-15T06:00:00+05:30", AMBER),
        (at(2027, 1, 15, 7), None, None),
        (at(2027, 1, 16, 0), None, RED),
    ]
    for n, (when, mail_at, level) in enumerate(cases):
        d = _fresh_db(tmp_path, f"custom{n}")
        try:
            if mail_at:
                mail(d, "rollover:activation:FY27-28", "WARNING", mail_at)
            got = [i["level"] for i in items(d, cfg, when) if i["code"] == "rollover:activation:FY27-28"]
            assert got == ([level] if level else []), when
        finally:
            d.close()


def test_T37_overdue_across_feb_29(tmp_path, db):
    cfg = _cfg(tmp_path, src=NEXT_SRC, nas=NEXT_NAS)           # configured FY27-28, next FY28-29
    db.set_overlap_record("FY28-29", {"activated_at": "2027-12-31T06:00:00+05:30", "from_fy": "FY27-28",
                                      "source_folder": "E:\\FY28-29", "clock": {}})
    add_run(db, "l-lan-FY28-29", "lan", LAN_OK, at(2028, 2, 29, 1))
    due = deadline("2028-02-29T01:00:00+05:30", "0 1 * * *", 4, 2)   # LAN 01:00 + 4 h + 2 h
    assert due == at(2028, 3, 1, 7)
    assert "stale:lan:FY28-29" not in codes(db, cfg, at(2028, 2, 29, 8))
    assert only(items(db, cfg, due.add(seconds=60)), "stale:lan:FY28-29")["level"] == RED


def test_T38_record_read_error_is_reported(tmp_path, db, monkeypatch):
    cfg = _cfg(tmp_path)
    add_run(db, "c-cloud-1230", "cloud", CLOUD_FAIL, at(2026, 12, 30, 18))

    def broken(fy):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(db, "overlap_record", broken)
    found = items(db, cfg, at(2026, 12, 31, 12))                  # must not raise
    hits = [i for i in found if i["code"] == "overlap:unknown"]
    assert len(hits) == 1 and hits[0]["level"] == RED
    assert "could not be read" in hits[0]["message"] and "it may not be backed up" in hits[0]["message"]
    assert "last_run:cloud" in [i["code"] for i in found]


def test_T38b_overlap_off_record_read_error_is_reported(tmp_path, db, monkeypatch):
    cfg = _overlap_off(_cfg(tmp_path))
    add_run(db, "c-cloud-1230", "cloud", CLOUD_FAIL, at(2026, 12, 30, 18))

    def broken(fy):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(db, "overlap_record", broken)
    found = items(db, cfg, at(2026, 12, 31, 12))                  # must not raise
    hits = [i for i in found if i["code"] == "overlap:unknown"]
    assert len(hits) == 1 and hits[0]["level"] == RED
    assert "last_run:cloud" in [i["code"] for i in found]


# ── W: the watchdog mails the next FY (B7 rows T39-T44) ──────────────────

def test_T39_watchdog_mails_next_fy_stale_once(tmp_path):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, cloud=False, smtp_port=srv.server_address[1])
        with _db_of(cfg) as db:
            activate(db)
            lan_nightly(db)
            with pendulum.travel_to(NOW2, freeze=True):
                assert watchdog._check_stale_legs(cfg) == ["lan:FY27-28"]
                first = db.last_notification("stale:lan:FY27-28")
                assert (first["status"], first["mode"]) == (nt.SENT, "lan:FY27-28")
                assert watchdog._check_stale_legs(cfg) == ["lan:FY27-28"]
                assert db.last_notification("stale:lan:FY27-28")["status"] == nt.SUPPRESSED


def test_T40_watchdog_repeats_after_cooldown(tmp_path):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, cloud=False, smtp_port=srv.server_address[1])
        with _db_of(cfg) as db:
            activate(db)
            lan_nightly(db)
            with pendulum.travel_to(NOW2, freeze=True):
                watchdog._check_stale_legs(cfg)
                assert db.last_notification("stale:lan:FY27-28")["status"] == nt.SENT
                watchdog._check_stale_legs(cfg)
                assert db.last_notification("stale:lan:FY27-28")["status"] == nt.SUPPRESSED
            later = NOW2.add(seconds=cfg.alerts.stale_repeat_hours * 3600 + 60)
            with pendulum.travel_to(later, freeze=True):
                assert watchdog._check_stale_legs(cfg) == ["lan:FY27-28"]
                assert db.last_notification("stale:lan:FY27-28")["status"] == nt.SENT


def test_T41_watchdog_told_about_is_per_fy(tmp_path):
    for name, group, mailed in (("configured", "lan", True), ("next", "lan:FY27-28", False)):
        sub = tmp_path / name
        with smtp_server() as srv:
            cfg = _cfg(sub, cloud=False, smtp_port=srv.server_address[1])
            with _db_of(cfg) as db:
                activate(db)
                lan_nightly(db)
                mail(db, "backup:lan:LAN_FAILED", "FAILURE", "2027-01-02T06:00:00+05:30", mode=group)
                with pendulum.travel_to(NOW2, freeze=True):
                    watchdog._check_stale_legs(cfg)
                row = db.last_notification("stale:lan:FY27-28")
                if mailed:
                    assert row is not None and row["status"] == nt.SENT, name
                else:
                    assert row is None, name


def test_T42_watchdog_both_fys_two_mails(tmp_path):
    with smtp_server() as srv:
        cfg = _cfg(tmp_path, lan=False, smtp_port=srv.server_address[1])
        with _db_of(cfg) as db:
            activate(db)
            cloud_failing(db)
            with pendulum.travel_to(NOW2, freeze=True):
                assert watchdog._check_stale_legs(cfg) == ["cloud", "cloud:FY27-28"]
            configured = db.last_notification("stale:cloud")
            next_fy = db.last_notification("stale:cloud:FY27-28")
            assert (configured["status"], configured["mode"]) == (nt.SENT, "cloud")
            assert (next_fy["status"], next_fy["mode"]) == (nt.SENT, "cloud:FY27-28")


def test_T43_watchdog_title_names_fy(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(nt, "notify", lambda cfg, event, db=None: sent.append(event))
    cfg = _cfg(tmp_path / "with", cloud=False)
    with _db_of(cfg) as db:
        activate(db)
        lan_failing(db)
        with pendulum.travel_to(NOW2, freeze=True):
            watchdog._check_stale_legs(cfg)
    assert {e.key: e.title for e in sent} == {"stale:lan": "LAN backup (FY26-27) overdue",
                                              "stale:lan:FY27-28": "LAN backup (FY27-28) overdue"}
    sent.clear()
    cfg = _cfg(tmp_path / "without", cloud=False)                 # the same configured state, no record
    with _db_of(cfg) as db:
        lan_failing(db, next_fy=False)
        with pendulum.travel_to(NOW2, freeze=True):
            watchdog._check_stale_legs(cfg)
    assert {e.key: e.title for e in sent} == {"stale:lan": "LAN backup overdue"}


def _from_watchdog_check() -> bool:
    import sys
    return sys._getframe(2).f_code.co_name == "_check_stale_legs"


def test_T43b_watchdog_title_lookup_failure_is_logged_and_mails_go_on(tmp_path, monkeypatch):
    from loguru import logger
    from core import fy_rollover
    real_active, real_configured = fy_overlap.active_next_fy, fy_rollover.configured_fy
    for name, patched in (("overlap", "active_next_fy"), ("configured", "configured_fy")):
        sent, logs = [], []
        monkeypatch.setattr(nt, "notify", lambda cfg, event, db=None: sent.append(event))

        def broken_active(*a, **k):
            if _from_watchdog_check():
                raise RuntimeError("database is locked")
            return real_active(*a, **k)

        def broken_configured(*a, **k):
            if _from_watchdog_check():
                raise RuntimeError("path unreadable")
            return real_configured(*a, **k)
        monkeypatch.setattr(fy_overlap, "active_next_fy", broken_active if patched == "active_next_fy" else real_active)
        monkeypatch.setattr(fy_rollover, "configured_fy",
                            broken_configured if patched == "configured_fy" else real_configured)
        sink = logger.add(lambda m: logs.append(str(m)), level="WARNING")
        try:
            cfg = _cfg(tmp_path / name, cloud=False)
            with _db_of(cfg) as db:
                activate(db)
                lan_failing(db)
                with pendulum.travel_to(NOW2, freeze=True):
                    watchdog._check_stale_legs(cfg)
        finally:
            logger.remove(sink)
        assert "stale:lan" in [e.key for e in sent], name                 # the configured FY's mail is not skipped
        assert any("database is locked" in m or "path unreadable" in m for m in logs), name


def test_T44_watchdog_mails_only_stale(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(nt, "notify", lambda cfg, event, db=None: sent.append(event))
    # (a) the next FY waits for its first backup
    cfg = _cfg(tmp_path / "waiting", cloud=True, lan=False)
    with _db_of(cfg) as db:
        activate(db)
        add_run(db, "c-cloud-1230", "cloud", CLOUD_OK, at(2026, 12, 30, 18))
        assert "first_run:cloud:FY27-28" in codes(db, cfg, at(2026, 12, 31, 12))
        with pendulum.travel_to(at(2026, 12, 31, 12), freeze=True):
            assert watchdog._check_stale_legs(cfg) == []
    # (b) the activation waits for the check
    cfg = _cfg(tmp_path / "activation", cloud=True, lan=False)
    with _db_of(cfg) as db:
        add_run(db, "c-cloud-1230", "cloud", CLOUD_OK, at(2026, 12, 30, 18))
        mail(db, "rollover:activation:FY27-28", "WARNING", "2026-12-31T06:00:00+05:30")
        assert "rollover:activation:FY27-28" in codes(db, cfg, at(2026, 12, 31, 6, 5))
        with pendulum.travel_to(at(2026, 12, 31, 6, 5), freeze=True):
            assert watchdog._check_stale_legs(cfg) == []
    # (c) the overlap is switched off while files wait in the next FY's folder
    cfg = _site(tmp_path / "off", overlap=False, next_files=("a.xlsx",), lan=False)
    with _db_of(cfg) as db:
        activate(db)
        add_run(db, "c-cloud-1230", "cloud", CLOUD_OK, at(2026, 12, 30, 18))
        assert "overlap_off:FY27-28" in codes(db, cfg, at(2026, 12, 31, 12))
        with pendulum.travel_to(at(2026, 12, 31, 12), freeze=True):
            assert watchdog._check_stale_legs(cfg) == []
    assert sent == []


# ── T: the card timing per FY (B7 rows T45-T46) ───────────────────────────

def test_T45_leg_timing_per_fy(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    assert leg_timing(db, cfg, "cloud", at(2026, 12, 31, 12), fy="FY27-28")["state"] == "waiting"
    due = deadline(ACTIVATED)
    assert leg_timing(db, cfg, "cloud", due.add(seconds=60), fy="FY27-28")["state"] == "overdue"
    add_run(db, "c-cloud-FY27-28", "cloud", CLOUD_OK, at(2027, 1, 1, 18))
    assert leg_timing(db, cfg, "cloud", at(2027, 1, 2, 12), fy="FY27-28")["state"] == "on_time"


def test_T10b_leg_timing_unreadable_clock_is_overdue(tmp_path, db):
    cfg = _cfg(tmp_path)
    now = at(2026, 12, 31, 12)
    assert leg_timing(db, cfg, "cloud", now, fy="FY27-28")["state"] == "overdue"      # no record at all
    for bad in ({}, {"activated_at": "garbage"}):
        db.set_overlap_record("FY27-28", bad)
        assert leg_timing(db, cfg, "cloud", now, fy="FY27-28")["state"] == "overdue", bad


def test_T46_leg_timing_default_is_configured_fy(tmp_path, db):
    cfg = _cfg(tmp_path)
    db.set_meta_created_at("2026-12-31T05:00:00+05:30")
    add_run(db, "c-cloud-FY27-28", "cloud", CLOUD_OK, at(2026, 12, 31, 10))   # the next FY ran; the FY has not
    assert leg_timing(db, cfg, "cloud", at(2026, 12, 31, 12))["state"] == "waiting"


# ── U: the approval, the configured texts and the mail-key codes (B7 rows T50-T53) ──

def test_T50_rows_before_switch_name_their_own_fy(tmp_path, db):
    cfg = _cfg(tmp_path, src=NEXT_SRC, nas=NEXT_NAS)
    db.set_overlap_record("FY27-28", {"activated_at": ACTIVATED, "from_fy": "FY26-27",
                                      "source_folder": NEXT_SRC, "clock": {}, "ended_at": SWITCHED})
    add_audit(db, "aud-cloud-full-0328", "cloud", AUDIT_BAD, at(2027, 3, 28, 10), detail="2 files differ")
    add_run(db, "c-cloud-0402", "cloud", CLOUD_FAIL, at(2027, 4, 2, 6))
    found = items(db, cfg, at(2027, 4, 2, 12))
    for code in ("audit:cloud", "last_run:cloud"):
        message = only(found, code)["message"]
        assert "FY26-27" in message and "FY27-28" not in message, code


def test_T51_activation_item_not_doubled_with_unprotected(tmp_path, db):
    cfg = _site(tmp_path, overlap=True, next_files=("a.xlsx",), lan=False)     # no activation record
    found = items(db, cfg, at(2027, 4, 2, 12))                                  # 2 Apr, FORWARD
    assert [(i["code"], i["level"]) for i in next_items(found)] == [("rollover:unprotected:FY27-28", RED)]


def test_T52_next_fy_approval_has_own_code(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    db.open_approval_request(leg="lan", blocked_run_id="l-lan-1231", fy="FY26-27", guards=[], phrase="p",
                             created_at=iso(at(2026, 12, 31, 1)), keep_fy="FY26-27")
    db.open_approval_request(leg="lan", blocked_run_id="l-lan-FY27-28", fy="FY27-28", guards=[], phrase="p",
                             created_at=iso(at(2027, 1, 1, 1)), keep_fy="FY26-27")
    found = items(db, cfg, at(2027, 1, 1, 12))
    only(found, "approval:lan")
    assert "FY27-28" in only(found, "approval:lan:FY27-28")["message"]

    d = _fresh_db(tmp_path, "fyless")                                          # an FY-less request
    try:
        activate(d)
        d.open_approval_request(leg="lan", blocked_run_id="l-lan-x", fy=None, guards=[], phrase="p",
                                created_at=iso(at(2027, 1, 1, 1)))
        plain = [i["code"] for i in items(d, cfg, at(2027, 1, 1, 12))]
        assert "approval:lan" in plain and "approval:lan:FY27-28" not in plain
    finally:
        d.close()


def test_T53_configured_items_name_fy_only_during_overlap(tmp_path, db):
    cfg = _cfg(tmp_path)
    activate(db)
    cloud_failing(db)
    with_record = items(db, cfg, at(2027, 1, 2, 12))
    no_record = _fresh_db(tmp_path, "norecord")
    try:
        cloud_failing(no_record)
        without = items(no_record, cfg, at(2027, 1, 2, 12))
        baseline = items(no_record, _overlap_off(cfg), at(2027, 1, 2, 12))
        assert sorted(i["code"] for i in with_record if "FY27-28" not in i["code"]) == \
            sorted(i["code"] for i in without if "FY27-28" not in i["code"])
        assert "FY26-27" in only(with_record, "stale:cloud")["message"]
        assert only(without, "stale:cloud")["message"] == only(baseline, "stale:cloud")["message"]
    finally:
        no_record.close()


# ── U: the activation is never gated by backups (B7 row T54, guard of the B1 code) ──

def test_T54_activation_ignores_backup_state(tmp_path):
    variants = {
        "failed": dict(cloud=CLOUD_FAIL, lan=LAN_FAIL),
        "running": dict(cloud=CLOUD_RUN, lan=LAN_RUN),
        "blocked": dict(cloud=CLOUD_BLOCKED, lan=LAN_BLOCKED),
        "lan_off": dict(cloud=CLOUD_OK, lan=None, lan_enabled=False),
        "cloud_manual": dict(cloud=CLOUD_OK, lan=LAN_OK, cloud_scheduled=False),
    }
    clock = SimpleNamespace(state=CLOCK_OK, reason="", to_metrics=lambda: {})
    for name, spec in variants.items():
        root = tmp_path / name
        src = root / "src"
        (src / "FY26-27").mkdir(parents=True, exist_ok=True)
        cfg = _cfg(root, src=str(src / "FY26-27"), nas="\\\\nonexistent-nas\\backup\\FY26-27",
                   lan=spec.get("lan_enabled", True), cloud_scheduled=spec.get("cloud_scheduled", True))
        with _db_of(cfg) as db:
            add_run(db, "c-cloud-1230", "cloud", spec["cloud"], at(2026, 12, 30, 18))
            if spec["lan"]:
                add_run(db, "l-lan-1230", "lan", spec["lan"], at(2026, 12, 30, 1))
            with patch("core.fy_overlap.check_clock", return_value=clock), \
                    patch("core.fy_overlap.notify", MagicMock()):
                result = fy_overlap.activate_if_due(cfg, db, now=at(2026, 12, 31, 6))
            assert result == fy_overlap.ACTIVATED, name
            assert (src / "FY27-28").is_dir(), name
            assert (src / "FY27-28" / cfg.rollover.source_placeholder_name).is_file(), name
            assert db.overlap_record("FY27-28") is not None, name
