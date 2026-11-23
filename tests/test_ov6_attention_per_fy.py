"""OV-6.1 / OV-6.2 - overdue per (leg, FY), attention codes that carry the FY, the watchdog per FY
(plan 5.6; R-5: no red and no mail for the early FY before its first run is due; R-8: closed / not-active
FYs never appear).

Fixture world: configured FY26-27, next FY27-28 activated on 1 Jan 2027 09:00; the LAN leg runs daily at
21:00 with a 4 h budget and 2 h grace, so a leg whose clock starts before 21:00 is overdue at 03:00 the
next night."""
from unittest.mock import patch

import pendulum
import pytest

import watchdog as wd
from core import fy_overlap
from core.attention import AMBER, RED, compute_attention, leg_timing
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import AppConfig

A, B = "FY26-27", "FY27-28"
ACTIVATED = "2027-01-01T09:00:00+05:30"


def _cfg(tmp_path, fy=A, **extra) -> AppConfig:
    return AppConfig(
        paths={"source_drive": str(tmp_path / fy), "lan_destination": "\\\\nas\\backup\\" + fy,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        schedule={"lan_cron": "0 21 * * *"}, budget={"lan_max_hours": 4},
        dashboard={"public_url": "http://backup-server:8080", "stale_grace_hours": 2},
        notifications={"recipients": ["o@firm.example"]},
        **extra)


@pytest.fixture
def cfg(tmp_path):
    return _cfg(tmp_path)


@pytest.fixture
def db(cfg):
    d = ManifestDB.from_config(cfg)
    yield d
    d.close()


def at(y, mo, d, h=0, mi=0, s=0):
    return pendulum.datetime(y, mo, d, h, mi, s, tz=IST)


def _run(db, fy, status, started, leg="lan", run_id=None, **extra):
    db.insert_run({"run_id": run_id or f"{fy}-{leg}-{started.isoformat()}", "mode": leg, "fy": fy,
                   "started_at": started.isoformat(),
                   "ended_at": started.add(minutes=20).isoformat(), "status": status, **extra})


def _ok(db, fy, started):
    _run(db, fy, "LAN_COMPLETE", started)


def _activate(db, when=ACTIVATED):
    db.record_activation(B, activated_at=when, source_path="D:\\" + B, created_by_app=True, clock_state="OK")


def _codes(items, level=None):
    return {i["code"] for i in items if level is None or i["level"] == level}


def _world_on_1_january(db):
    """Dec 2026 history of the configured FY (a success the night before), FY27-28 activated at 09:00."""
    _ok(db, A, at(2026, 12, 1, 21, 0, 30))
    _ok(db, A, at(2026, 12, 31, 21, 0, 30))
    _activate(db)


# ── OV-6.1: the early FY's own clock ─────────────────────────────────────────────────────────

def test_1_january_before_the_first_run_of_the_day_nothing_is_red(cfg, db):
    _world_on_1_january(db)
    items = compute_attention(db, cfg, now=at(2027, 1, 1, 10, 0))
    assert not _codes(items, RED)
    assert f"first_run:{B}:lan" in _codes(items, AMBER)           # amber at most, and only for the new FY
    assert not [c for c in _codes(items) if c.startswith("stale:")]


def test_1_january_no_mail_for_the_early_fy(cfg, db):
    _world_on_1_january(db)
    sent = []
    with pendulum.travel_to(at(2027, 1, 1, 10, 0), freeze=True), \
            patch("core.notify.notify", side_effect=lambda c, e, db=None: sent.append(e)):
        assert wd._check_stale_legs(cfg) == []
    assert sent == []


def test_the_base_is_the_activation_not_the_install_date(cfg, db):
    """first_known_at is November (an old install); the early FY must not count from there."""
    _ok(db, A, at(2026, 11, 2, 21, 0, 30))
    _ok(db, A, at(2026, 12, 31, 21, 0, 30))
    _activate(db)
    items = compute_attention(db, cfg, now=at(2027, 1, 1, 23, 0))
    assert f"stale:{B}:lan" not in _codes(items)


def test_the_early_fy_is_stale_once_its_first_run_is_overdue_and_only_for_that_fy(cfg, db):
    _world_on_1_january(db)
    _ok(db, A, at(2027, 1, 1, 21, 0, 30))                         # the configured FY ran; FY27-28 did not
    assert f"stale:{B}:lan" not in _codes(compute_attention(db, cfg, now=at(2027, 1, 2, 2, 59)))
    items = compute_attention(db, cfg, now=at(2027, 1, 2, 3, 1))  # 21:00 + 4 h + 2 h grace has passed
    assert _codes(items, RED) == {f"stale:{B}:lan"}
    [item] = [i for i in items if i["code"] == f"stale:{B}:lan"]
    assert "FY27-28 LAN backup" in item["message"] and "never completed" in item["message"]


def test_the_configured_fy_is_independent_of_the_early_fy(cfg, db):
    _world_on_1_january(db)
    _ok(db, B, at(2027, 1, 1, 21, 0, 30))                         # only FY27-28 succeeded tonight
    items = compute_attention(db, cfg, now=at(2027, 1, 2, 3, 1))
    assert _codes(items, RED) == {f"stale:{A}:lan"}               # FY26-27 missed its run; FY27-28 is fine
    [item] = [i for i in items if i["code"] == f"stale:{A}:lan"]
    assert "FY26-27 LAN backup" in item["message"]


def test_an_early_fy_success_of_another_fy_does_not_count_for_it(cfg, db):
    """No `or earlier` for the early FY: FY26-27's fresh success says nothing about FY27-28."""
    _world_on_1_january(db)
    for day in (1, 2, 3):
        _ok(db, A, at(2027, 1, day, 21, 0, 30))
    assert f"stale:{B}:lan" in _codes(compute_attention(db, cfg, now=at(2027, 1, 4, 1, 0)))


def test_after_the_april_switch_the_new_fy_is_not_never_backed_up(tmp_path):
    """The configured FY keeps the 'or earlier' fallback: the old FY's success counts after the switch."""
    cfg = _cfg(tmp_path, fy=B)
    db = ManifestDB.from_config(cfg)
    try:
        _ok(db, A, at(2027, 3, 31, 21, 0, 30))
        items = compute_attention(db, cfg, now=at(2027, 4, 1, 12, 0))
        assert not [c for c in _codes(items) if c.startswith("stale:")]
    finally:
        db.close()


def test_leg_timing_of_the_early_fy_waits_then_goes_overdue(cfg, db):
    _world_on_1_january(db)
    waiting = leg_timing(db, cfg, "lan", now=at(2027, 1, 1, 10, 0), fy=B, role=fy_overlap.EARLY)
    assert waiting["state"] == "waiting"
    late = leg_timing(db, cfg, "lan", now=at(2027, 1, 2, 3, 1), fy=B, role=fy_overlap.EARLY)
    assert late["state"] == "overdue"
    assert leg_timing(db, cfg, "lan", now=at(2027, 1, 1, 10, 0))["state"] == "on_time"     # configured FY, as before


# ── OV-6.2: codes carry the FY ───────────────────────────────────────────────────────────────

def test_the_fy_codes_of_a_failed_run_and_an_audit_are_per_fy(cfg, db):
    _world_on_1_january(db)
    _ok(db, A, at(2027, 1, 1, 21, 0, 30))
    _run(db, B, "LAN_FAILED", at(2027, 1, 1, 21, 0, 40))
    db.record_audit({"audit_id": "au1", "mode": "lan", "fy": B, "started_at": at(2027, 1, 1, 12).isoformat(),
                     "status": "VERIFICATION_FAILED", "detail": "x", "created_at": at(2027, 1, 1, 12).isoformat()})
    items = compute_attention(db, cfg, now=at(2027, 1, 1, 22, 0))
    assert {f"last_run:{B}:lan", f"audit:{B}:lan"} <= _codes(items, RED)
    assert f"last_run:{A}:lan" not in _codes(items) and f"audit:{A}:lan" not in _codes(items)
    msg = {i["code"]: i["message"] for i in items}
    assert "FY27-28 LAN backup" in msg[f"last_run:{B}:lan"] and "FY27-28 LAN backup" in msg[f"audit:{B}:lan"]


def test_degraded_run_logs_code_carries_the_fy(cfg, db):
    _world_on_1_january(db)
    _run(db, B, "LAN_COMPLETE", at(2027, 1, 1, 21, 0, 40), extended_metrics='{"artefacts_degraded": true}')
    assert f"run_logs:{B}:lan" in _codes(compute_attention(db, cfg, now=at(2027, 1, 1, 22, 0)), AMBER)


def test_approvals_show_per_active_fy_and_never_for_a_closed_fy(cfg, db):
    _world_on_1_january(db)
    _ok(db, A, at(2027, 1, 1, 21, 0, 30))
    _ok(db, B, at(2027, 1, 1, 21, 0, 30))
    guards = [{"guard": "G3_MASS_DELETE", "reason": "r", "metrics": {}}]
    for fy in (A, B, "FY25-26"):                                   # FY25-26: a closed year, not active
        db.open_approval_request(leg="lan", blocked_run_id=f"b-{fy}", fy=fy, guards=guards,
                                 phrase="DELETE 9", created_at=at(2027, 1, 1, 21, 5).isoformat())
    items = compute_attention(db, cfg, now=at(2027, 1, 1, 22, 0))
    approvals = {c for c in _codes(items, RED) if c.startswith("approval:")}
    assert approvals == {f"approval:{A}:lan", f"approval:{B}:lan"}


def test_one_active_fy_keeps_todays_wording_but_the_code_has_the_fy(cfg, db):
    _ok(db, A, at(2026, 10, 20, 21, 0, 30))
    items = compute_attention(db, cfg, now=at(2026, 10, 25, 12, 0))
    [item] = [i for i in items if i["code"] == f"stale:{A}:lan"]
    assert "The last success was" in item["message"]
    assert "FY26-27 LAN backup" not in item["message"] and "Check why the LAN backup has not succeeded" in item["message"]


def test_global_codes_are_unchanged(cfg, db):
    _world_on_1_january(db)
    _run(db, B, "LAN_COMPLETE", at(2027, 1, 1, 21, 0, 40),
         extended_metrics='{"nas_shutdown": {"outcome": "FAILED", "detail": "still on"}}')
    codes = _codes(compute_attention(db, cfg, now=at(2027, 1, 1, 22, 0)), AMBER)
    assert "nas_shutdown" in codes


def test_an_unreadable_active_set_falls_back_to_the_configured_fy_with_an_amber_item(cfg, db):
    _ok(db, A, at(2026, 10, 20, 21, 0, 30))
    with patch.object(fy_overlap, "active_folders", side_effect=OSError("boom")):
        items = compute_attention(db, cfg, now=at(2026, 10, 25, 12, 0))
    assert f"stale:{A}:lan" in _codes(items, RED)                  # the configured FY is still judged
    [note] = [i for i in items if i["code"] == "overlap:active_set"]
    assert note["level"] == AMBER and "could not work out which year folders are backed up" in note["message"].lower()


# ── OV-6.2: the watchdog per (leg, FY) ───────────────────────────────────────────────────────

def _both_stale(db):
    _ok(db, A, at(2026, 12, 30, 21, 0, 30))
    _activate(db, "2026-12-30T09:00:00+05:30")


def test_the_watchdog_mails_once_per_leg_and_fy(cfg, db):
    _both_stale(db)
    sent = []
    with pendulum.travel_to(at(2027, 1, 2, 12, 0), freeze=True), \
            patch("core.notify.notify", side_effect=lambda c, e, db=None: sent.append(e)):
        assert wd._check_stale_legs(cfg) == ["lan"]                # the legs, once each
    assert sorted((e.key, e.fy, e.mode) for e in sent) == [
        (f"stale:{A}:lan", A, "lan"), (f"stale:{B}:lan", B, "lan")]


def test_a_failure_mail_of_one_fy_suppresses_only_that_fy(cfg, db):
    _both_stale(db)
    db.record_notification({
        "event_key": f"backup:{B}:lan:LAN_FAILED", "kind": "FAILURE", "mode": "lan", "subject": "s",
        "status": "SENT", "fy": B, "created_at": at(2027, 1, 2, 6, 0).isoformat()})
    sent = []
    with pendulum.travel_to(at(2027, 1, 2, 12, 0), freeze=True), \
            patch("core.notify.notify", side_effect=lambda c, e, db=None: sent.append(e)):
        wd._check_stale_legs(cfg)
    assert [e.key for e in sent] == [f"stale:{A}:lan"]             # FY27-28 was already told, FY26-27 was not


def test_a_stale_code_without_a_known_fy_is_not_suppressed(tmp_path):
    """Fail closed: when the FY of an old-style code cannot be worked out nothing is skipped."""
    cfg = _cfg(tmp_path)
    d = ManifestDB.from_config(cfg)
    d.record_notification({"event_key": "backup:lan:LAN_FAILED", "kind": "FAILURE", "mode": "lan",
                           "subject": "s", "status": "SENT", "fy": A,
                           "created_at": at(2027, 1, 2, 6, 0).isoformat()})
    d.close()
    item = {"code": "stale:lan", "message": "overdue", "due_at": at(2027, 1, 2, 3, 0).isoformat()}
    sent = []
    with patch("core.attention.compute_attention", return_value=[item]), \
            patch.object(wd, "_stale_code_parts", return_value=(None, "lan")), \
            patch("core.notify.notify", side_effect=lambda c, e, db=None: sent.append(e)):
        assert wd._check_stale_legs(cfg) == ["lan"]
    assert [e.key for e in sent] == ["stale:lan"]


def test_stale_item_not_hidden_by_a_newer_fys_success(cfg, db):
    """Review I1: FY26-27 fails every night while FY27-28 succeeds -> FY26-27 is stale (a newer FY's
    success must not stand in for it)."""
    _activate(db)
    for day in range(1, 11):
        _ok(db, B, at(2027, 1, day, 21, 0, 30))
        _run(db, A, "LAN_FAILED", at(2027, 1, day, 21, 30, 0))
    items = compute_attention(db, cfg, now=at(2027, 1, 11, 12, 0))
    assert "stale:FY26-27:lan" in _codes(items)
    assert db.last_successful_run_or_earlier("lan", fy=A) is None


def test_last_success_fallback_works_across_the_century_wrap(db):
    _run(db, "FY99-00", "LAN_COMPLETE", started=at(2000, 3, 1, 21), leg="lan", run_id="old")
    _run(db, "FY01-02", "LAN_COMPLETE", started=at(2001, 6, 1, 21), leg="lan", run_id="newer")
    assert db.last_successful_run_or_earlier("lan", fy="FY00-01")["run_id"] == "old"
