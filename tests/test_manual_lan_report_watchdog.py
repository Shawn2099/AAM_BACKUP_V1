"""Manual-only / off legs in the reports and the watchdog (owner 2026-11-23, LAN and cloud; cards T1, T4, T10)."""
from unittest.mock import patch

import pendulum
import pytest

from core import fy_overlap, report
from core.manifest import ManifestDB
from core.report_period import ReportPeriod
from core.time_utils import IST
from models.config import AppConfig

A = "FY26-27"
OK = {"lan": "LAN_COMPLETE", "cloud": "CLOUD_COMPLETE"}


def at(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


def _cfg(tmp_path, lan=None, cloud=None) -> AppConfig:
    key = tmp_path / "key.json"
    key.write_text("{}")
    return AppConfig(
        paths={"source_drive": str(tmp_path / A), "lan_destination": "\\\\nas\\backup\\" + A,
               "runtime_dir": str(tmp_path / "rt"), "gcs_key_path": str(key)},
        wol={"enabled": False},
        lan={"enabled": False, **(lan or {})},
        cloud={"bucket": "aam-test-bucket", "project_number": "1", "enabled": False, **(cloud or {})},
        schedule={"lan_cron": "0 21 * * *", "cloud_cron": "0 2 * * *"},
        notifications={"recipients": ["o@firm.example"]})


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    yield d
    d.close()


def _row(db, leg, when, status=None):
    t = when.isoformat()
    db.insert_run({"fy": A, "run_id": f"{leg}-{t}", "mode": leg, "started_at": t, "ended_at": t,
                   "status": status or OK[leg], "files_copied": 5, "bytes_copied": 50})


WEEK = ReportPeriod.previous_week(at(2027, 1, 4, 8))          # Mon 28 Dec 2026 - Sun 3 Jan 2027


def _model(db, cfg, legs):
    with patch.object(fy_overlap, "run_date", return_value=at(2027, 1, 4).date()):
        return report.build_report_model(db, WEEK, legs=legs, schedule=cfg.schedule, config=cfg)


@pytest.mark.parametrize("leg", ["lan", "cloud"])
def test_manual_only_leg_with_one_manual_run_is_not_a_shortfall(db, tmp_path, leg):      # T10
    cfg = _cfg(tmp_path, **{leg: {"enabled": True, "scheduled": False}})
    _row(db, leg, at(2026, 12, 15, 15))             # the leg ran before the period
    _row(db, leg, at(2026, 12, 30, 15))             # one manual run at 15:00 (covers no scheduled night)
    m = _model(db, cfg, (leg,))
    assert m["act"] is False and "scheduled nights have no backup record" not in m["headline"]
    assert "no backup ran" not in m["headline"]
    html_out = report.render_report_html(m, "Firm")
    assert "manual only, last backup" in html_out and "have no backup record" not in html_out


@pytest.mark.parametrize("leg", ["lan", "cloud"])
def test_nightly_leg_with_the_same_history_is_still_a_shortfall(db, tmp_path, leg):
    cfg = _cfg(tmp_path, **{leg: {"enabled": True, "scheduled": True}})
    _row(db, leg, at(2026, 12, 15, 15))
    _row(db, leg, at(2026, 12, 30, 15))
    m = _model(db, cfg, (leg,))
    assert m["act"] is True and "scheduled nights have no backup record" in m["headline"]


@pytest.mark.parametrize("leg", ["lan", "cloud"])
def test_manual_only_leg_with_no_run_in_the_period_does_not_say_no_backup_ran(db, tmp_path, leg):
    cfg = _cfg(tmp_path, **{leg: {"enabled": True, "scheduled": False}})
    _row(db, leg, at(2026, 12, 10, 15))
    m = _model(db, cfg, (leg,))
    assert m["act"] is False and "no backup ran" not in m["headline"]
    assert "Manual only: no backup was started in this period." in report.render_report_html(m, "Firm")


@pytest.mark.parametrize("leg", ["lan", "cloud"])
def test_old_manual_backup_is_a_soft_amber_note_not_action_needed(db, tmp_path, leg):
    cfg = _cfg(tmp_path, **{leg: {"enabled": True, "scheduled": False, "on_demand_warn_after_days": 5}})
    _row(db, leg, at(2026, 11, 1, 15))
    m = _model(db, cfg, (leg,))
    assert m["act"] is False and m["soft_warn"] is True
    assert m["headline"].startswith("note: ") and "manual only, last backup" in m["headline"]
    assert "#b45309" in report.render_report_html(m, "Firm")


@pytest.mark.parametrize("leg,label", [("lan", "LAN"), ("cloud", "Cloud")])
def test_off_leg_is_stated_in_the_report(db, tmp_path, leg, label):                   # T1
    other = "cloud" if leg == "lan" else "lan"
    cfg = _cfg(tmp_path, **{other: {"enabled": True, "scheduled": True}})
    _row(db, other, at(2026, 12, 29, 21 if other == "lan" else 2))
    for d in range(28, 32):
        _row(db, other, at(2026, 12, d, 21 if other == "lan" else 2))
    for d in (1, 2, 3):
        _row(db, other, at(2027, 1, d, 21 if other == "lan" else 2))
    m = _model(db, cfg, (other,))
    assert f"{label}: off" in [n["text"] for n in m["mode_notes"]]
    assert f"{label}: off" in report.render_report_html(m, "Firm")


def test_both_legs_manual_says_nothing_runs_automatically(db, tmp_path):
    cfg = _cfg(tmp_path, lan={"enabled": True, "scheduled": False},
               cloud={"enabled": True, "scheduled": False})
    _row(db, "lan", at(2026, 12, 29, 15))
    _row(db, "cloud", at(2026, 12, 29, 16))
    m = _model(db, cfg, ("lan", "cloud"))
    texts = [n["text"] for n in m["mode_notes"]]
    assert any("Nothing runs automatically" in t for t in texts)
    assert m["act"] is False and m["soft_warn"] is True and m["headline"].startswith("note: ")


def test_watchdog_sends_no_overdue_mail_for_a_manual_leg(db, tmp_path, monkeypatch):    # T4
    import watchdog
    from core import notify as nt

    for leg in ("lan", "cloud"):
        cfg = _cfg(tmp_path, **{leg: {"enabled": True, "scheduled": False}})
        _row(db, leg, pendulum.now(IST).subtract(days=10))
        sent = []
        monkeypatch.setattr(nt, "notify", lambda *a, **k: sent.append(a))
        monkeypatch.setattr(ManifestDB, "from_config", classmethod(lambda cls, c: db))
        monkeypatch.setattr(db, "close", lambda: None)
        assert watchdog._check_stale_legs(cfg) == []
        assert sent == []


def test_watchdog_still_mails_an_overdue_nightly_leg(db, tmp_path, monkeypatch):
    import watchdog
    from core import notify as nt

    cfg = _cfg(tmp_path, lan={"enabled": True, "scheduled": True})
    _row(db, "lan", pendulum.now(IST).subtract(days=10))
    sent = []
    monkeypatch.setattr(nt, "notify", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(ManifestDB, "from_config", classmethod(lambda cls, c: db))
    monkeypatch.setattr(db, "close", lambda: None)
    assert watchdog._check_stale_legs(cfg) == ["lan"]
    assert len(sent) == 1
