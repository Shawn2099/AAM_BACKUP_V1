"""B9 - dashboard API and page for the FY overlap (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, B9).

One test per row of docs/TESTMATRIX_B9_2026-11-24.md (U, R, T, P, O, J, E rows). Rows R07 and G01 are the
regression run of existing files; the K1 helper has one extra test (test_k1_active_fys). The E rows (matrix:
extension of tests/test_e8_dashboard_e2e.py) live here, with their own copy of that file's server fixture.

Layer A = HTTP through TestClient(ui.app) (signed in by tests/conftest.py), P = pure helper, J = JS / template
source, E = a real uvicorn server. Database (real SQLite), config (real AppConfig) and FY maths are real;
Prefect is faked (arun_deployment, _is_running, _active_leg_runs); the clock is pendulum.travel_to.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pendulum
import pytest
import uvicorn
import yaml
from fastapi.testclient import TestClient
from prefect.client.schemas.objects import StateType

import ui
from core import approvals as ap
from core import attention, fy_overlap, night_strip
from core.manifest import ManifestDB
from core.time_utils import cron_to_human
from models.config import AppConfig
from test_support import attention_cfg, ui_login
from test_support.sandbox_config import _config_dict

pytest.importorskip("time_machine")   # pendulum.travel_to needs it

ROOT = Path(__file__).resolve().parents[1]
TZ = "Asia/Kolkata"
OWNER_SRC = "E:\\FY26-27"
OWNER_NAS = "\\\\nas\\backup\\FY26-27"
NEXT_SRC = "E:\\FY27-28"
NEXT_NAS = "\\\\nas\\backup\\FY27-28"
ACTIVATED = "2026-12-31T06:00:00+05:30"
SWITCHED = "2027-04-02T06:10:00+05:30"
NEW_FIELDS = {"fy", "scheduled", "scheduled_setting", "schedule_text", "last_success_fy", "leg_busy", "run_note"}


# ── helpers ───────────────────────────────────────────────────────────────

def at(*parts):
    return pendulum.datetime(*parts, tz=TZ)


def iso(moment) -> str:
    return moment.isoformat()


NOW = at(2027, 1, 10, 12)


def _cfg(tmp_path: Path, *, src=OWNER_SRC, nas=OWNER_NAS, overlap=True, cloud=True, lan=True,
         cloud_scheduled=True, lan_scheduled=True) -> AppConfig:
    (tmp_path / "rt").mkdir(parents=True, exist_ok=True)
    key = tmp_path / "keys" / "sandbox-key.json"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text('{"type": "service_account", "sandbox": true}', encoding="utf-8")
    return AppConfig(
        paths={"source_drive": src, "lan_destination": nas, "runtime_dir": str(tmp_path / "rt"),
               "gcs_key_path": str(key)},
        wol={"enabled": False},
        cloud={"enabled": cloud, "scheduled": cloud_scheduled, "bucket": "aam-test-bucket",
               "project_number": "0"},
        lan={"enabled": lan, "scheduled": lan_scheduled},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        notifications={"recipients": ["o@firm.example"]},
        alerts={"stale_repeat_hours": 6},
        rollover={"overlap_enabled": overlap},
    )


def add_run(db, run_id, mode, status, started, *, metrics=None, files=0):
    db.insert_run({"run_id": run_id, "mode": mode, "started_at": iso(started), "ended_at": iso(started),
                   "status": status, "exit_code": 0, "bytes_copied": 0, "files_copied": files,
                   "extended_metrics": None if metrics is None else json.dumps(metrics)})


def add_audit(db, audit_id, mode, status, started):
    db.record_audit({"audit_id": audit_id, "mode": mode, "started_at": iso(started),
                     "ended_at": iso(started), "status": status, "detail": None})


def activate(db, activated=ACTIVATED, fy="FY27-28", **extra):
    db.set_overlap_record(fy, {"activated_at": activated, "from_fy": "FY26-27",
                               "source_folder": NEXT_SRC, "clock": {}, **extra})


class Rig:
    """The dashboard with a real config and database and a faked Prefect."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.mp = monkeypatch
        self.db = ManifestDB(str(tmp_path / "m.db"))
        self.db.set_meta_created_at("2026-01-01T00:00:00+05:30")
        self.cfg = _cfg(tmp_path, overlap=False)
        self.calls: list[dict] = []
        self.db_calls = 0
        self._stack = ExitStack()
        monkeypatch.setattr(ui, "_cfg", lambda: self.cfg)
        monkeypatch.setattr(ui, "get_db", self._get_db)
        monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
        self.is_running = AsyncMock(return_value=False)
        self.verify_running = AsyncMock(return_value=False)
        self.active_runs = AsyncMock(return_value=[])
        monkeypatch.setattr(ui, "_is_running", self.is_running)
        monkeypatch.setattr(ui, "_is_verify_running", self.verify_running)
        monkeypatch.setattr(ui, "_active_leg_runs", self.active_runs, raising=False)

        async def fake_arun(*args, **kwargs):
            self.calls.append({"args": args, "kwargs": kwargs})
            return SimpleNamespace(id="flow-run-1", state=None)

        monkeypatch.setattr(ui, "arun_deployment", fake_arun)
        self.at(NOW)
        self.client = TestClient(ui.app, raise_server_exceptions=False)

    def _get_db(self):
        self.db_calls += 1
        return self.db

    def at(self, when):
        """Freeze the clock at `when`; the dashboard session is registered again inside that clock."""
        self._stack.close()
        self._stack = ExitStack()
        self._stack.enter_context(pendulum.travel_to(when, freeze=True))
        ui_login.register(ui)

    def use(self, **kw):
        self.cfg = _cfg(self.tmp_path, **kw)
        return self.cfg

    def close(self):
        self._stack.close()
        self.db.close()

    def status(self) -> dict:
        r = self.client.get("/status")
        assert r.status_code == 200, r.text
        return r.json()

    def post(self, url, **kw):
        return self.client.post(url, **kw)

    def params(self):
        """The `parameters` of the last arun_deployment call ('<absent>' when the kwarg was not passed)."""
        return self.calls[-1]["kwargs"].get("parameters", "<absent>")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.close()


def seed_baseline(db):
    """Three nights of both legs (ok, partial, failed) and one audit; no overlap record."""
    for day, cloud, lan in ((7, "CLOUD_COMPLETE", "LAN_COMPLETE"), (8, "CLOUD_PARTIAL", "LAN_PARTIAL"),
                            (9, "CLOUD_FAILED", "LAN_FAILED")):
        add_run(db, f"b{day}-cloud", "cloud", cloud, at(2027, 1, day, 18), files=10 * day)
        add_run(db, f"b{day}-lan", "lan", lan, at(2027, 1, day, 1), files=10 * day)
    add_audit(db, "a1-cloud", "cloud", "VERIFIED", at(2027, 1, 9, 20))


def seed_both_fys(db):
    """Configured and FY27-28 rows with different results per FY (U05)."""
    add_run(db, "c8-cloud", "cloud", "CLOUD_COMPLETE", at(2027, 1, 8, 18))
    add_run(db, "c9-cloud", "cloud", "CLOUD_FAILED", at(2027, 1, 9, 18))
    add_run(db, "n8-cloud-FY27-28", "cloud", "CLOUD_FAILED", at(2027, 1, 8, 18), metrics={"fy": "FY27-28"})
    add_run(db, "n9-cloud-FY27-28", "cloud", "CLOUD_COMPLETE", at(2027, 1, 9, 18), metrics={"fy": "FY27-28"})
    add_run(db, "c9-lan", "lan", "LAN_COMPLETE", at(2027, 1, 9, 1))
    add_run(db, "n9-lan-FY27-28", "lan", "LAN_FAILED", at(2027, 1, 9, 1), metrics={"fy": "FY27-28"})
    add_audit(db, "ac-cloud", "cloud", "VERIFICATION_FAILED", at(2027, 1, 9, 20))
    add_audit(db, "an-cloud-FY27-28", "cloud", "VERIFIED", at(2027, 1, 9, 21))


def night(card, day):
    return next(n for n in card["nights"] if n["date"] == day)["state"]


def strip_card(card: dict) -> dict:
    out = {k: v for k, v in card.items() if k not in NEW_FIELDS}
    if isinstance(out.get("last_run"), dict):
        out["last_run"] = {k: v for k, v in out["last_run"].items() if k not in ("fy", "total_files")}
    if isinstance(out.get("integrity"), dict):
        out["integrity"] = {k: v for k, v in out["integrity"].items() if k != "fy"}
    return out


def baseline_view(s: dict) -> dict:
    """/status without the additive keys of B9 and without the machine-dependent health block."""
    out = {k: v for k, v in s.items() if k not in ("overlap", "legs", "health")}
    out["cloud"], out["lan"] = strip_card(s["cloud"]), strip_card(s["lan"])
    out["recent_runs"] = [{k: v for k, v in r.items() if k != "fy"} for r in s["recent_runs"]]
    return out


def seed_switch(db, *, late_ok: bool):
    """The first year after the switch (U10 / U11): config FY27-28, overlap:FY27-28 ended 2 Apr 06:10."""
    db.set_overlap_record("FY27-28", {"activated_at": ACTIVATED, "from_fy": "FY26-27", "ended_at": SWITCHED})
    for day in (28, 29, 30, 31):
        add_run(db, f"m{day}-cloud", "cloud", "CLOUD_FAILED", at(2027, 3, day, 18))
        add_run(db, f"m{day}-cloud-FY27-28", "cloud", "CLOUD_COMPLETE", at(2027, 3, day, 19),
                metrics={"fy": "FY27-28"})
    add_run(db, "a1-cloud", "cloud", "CLOUD_FAILED", at(2027, 4, 1, 18))
    if late_ok:
        add_run(db, "a2-cloud", "cloud", "CLOUD_COMPLETE", at(2027, 4, 2, 18))
    else:
        add_run(db, "a2-cloud", "cloud", "CLOUD_FAILED", at(2027, 4, 2, 6), metrics={"fy": "FY26-27"})


# ── U: /status ────────────────────────────────────────────────────────────

def test_status_without_overlap_matches_baseline(rig):
    """U01: /status with the overlap never used equals the pre-B9 answer; the new label fields are None."""
    seed_baseline(rig.db)
    s = rig.status()
    assert "cloud_next" not in s and "lan_next" not in s
    view = baseline_view(s)
    assert set(view) == {"firm", "fy_prefix", "attention", "verify_running", "schedule", "cloud", "lan",
                         "manifest", "recent_runs"}
    cloud, lan = view["cloud"], view["lan"]
    assert set(cloud) == {"running", "run_state", "enabled", "last_run", "last_success", "timing",
                          "last_run_formatted", "integrity", "nights"}
    assert set(lan) == set(cloud)
    assert (cloud["running"], cloud["run_state"], cloud["enabled"]) == (False, "idle", True)
    assert cloud["last_run"]["status"] == "CLOUD_FAILED" and cloud["last_run"]["started_at"] == iso(at(2027, 1, 9, 18))
    assert cloud["last_success"] == iso(at(2027, 1, 7, 18))
    assert cloud["integrity"]["status"] == "VERIFIED"
    assert lan["last_run"]["status"] == "LAN_FAILED" and lan["last_success"] == iso(at(2027, 1, 7, 1))
    assert lan["integrity"]["status"] == "NOT_VERIFIED"
    assert [n["state"] for n in cloud["nights"] if n["state"] != "none"] == ["ok", "partial", "bad"]
    assert [n["state"] for n in lan["nights"] if n["state"] != "none"] == ["ok", "partial", "bad"]
    assert view["schedule"] == {"cloud_cron": cron_to_human(rig.cfg.schedule.cloud_cron, rig.cfg.schedule.timezone),
                                "lan_cron": cron_to_human(rig.cfg.schedule.lan_cron, rig.cfg.schedule.timezone)}
    assert view["manifest"] == {"lan_files": 0, "cloud_files": 0}
    assert sorted((r["mode"], r["status"]) for r in view["recent_runs"]) == sorted([
        ("cloud", "CLOUD_FAILED"), ("cloud", "CLOUD_PARTIAL"), ("cloud", "CLOUD_COMPLETE"),
        ("lan", "LAN_FAILED"), ("lan", "LAN_PARTIAL"), ("lan", "LAN_COMPLETE")])


def test_status_without_overlap_new_fields_are_empty(rig):
    """U01 (additive part): overlap.active false and every label field None while the overlap was never used."""
    seed_baseline(rig.db)
    s = rig.status()
    assert s["overlap"]["active"] is False
    assert s["cloud"]["last_run"].get("fy") is None and s["cloud"]["last_success_fy"] is None
    assert s["cloud"]["integrity"].get("fy") is None
    assert all(r.get("fy") is None for r in s["recent_runs"])


def test_status_overlap_off_has_two_cards(rig):
    """U02: record open but overlap_enabled false -> the cards are the baseline cards."""
    seed_baseline(rig.db)
    base = baseline_view(rig.status())
    activate(rig.db)
    rig.use(overlap=False)
    s = rig.status()
    assert "cloud_next" not in s and "lan_next" not in s
    assert s["overlap"]["active"] is False and s["overlap"]["enabled"] is False
    assert baseline_view(s)["cloud"] == base["cloud"] and baseline_view(s)["lan"] == base["lan"]


def test_status_before_activation(rig):
    """U03."""
    rig.use(overlap=True)
    rig.at(at(2026, 12, 30, 12))
    s = rig.status()
    assert "cloud_next" not in s and "lan_next" not in s
    assert "FY27-28 will be added to the backups on 31 Dec 2026" in s["overlap"]["note"]
    assert s["overlap"]["activation_pending"] is False


def test_status_activation_pending(rig):
    """U04."""
    rig.use(overlap=True)
    rig.at(at(2027, 1, 1, 8))
    s = rig.status()
    assert "cloud_next" not in s and "lan_next" not in s
    assert s["overlap"]["activation_pending"] is True
    assert "not added" in s["overlap"]["note"] and "Run FY rollover now" in s["overlap"]["note"]


def test_status_four_cards_each_own_rows(rig):
    """U05."""
    rig.use(overlap=True)
    activate(rig.db)
    seed_both_fys(rig.db)
    s = rig.status()
    assert s["cloud"]["fy"] == "FY26-27" and s["cloud_next"]["fy"] == "FY27-28"
    assert s["lan"]["fy"] == "FY26-27" and s["lan_next"]["fy"] == "FY27-28"
    assert s["cloud"]["last_run"]["status"] == "CLOUD_FAILED"
    assert s["cloud_next"]["last_run"]["status"] == "CLOUD_COMPLETE"
    assert s["cloud"]["last_success"] == iso(at(2027, 1, 8, 18))
    assert s["cloud_next"]["last_success"] == iso(at(2027, 1, 9, 18))
    assert s["cloud"]["integrity"]["status"] == "VERIFICATION_FAILED"
    assert s["cloud_next"]["integrity"]["status"] == "VERIFIED"
    assert s["lan"]["last_run"]["status"] == "LAN_COMPLETE" and s["lan"]["last_success"] == iso(at(2027, 1, 9, 1))
    assert s["lan_next"]["last_run"]["status"] == "LAN_FAILED" and s["lan_next"]["last_success"] is None


def test_next_card_first_backup_pending(rig):
    """U06."""
    rig.use(overlap=True)
    rig.at(at(2026, 12, 31, 12))
    activate(rig.db)
    add_run(rig.db, "x-cloud", "cloud", "CLOUD_COMPLETE", at(2026, 12, 30, 18))
    s = rig.status()
    nxt = s["cloud_next"]
    assert nxt["last_run"] is None and nxt["last_success"] is None
    assert nxt["timing"]["state"] == "waiting"
    assert all(n["state"] == "none" for n in nxt["nights"])


def test_next_card_timing_from_activation(rig):
    """U07."""
    rig.use(overlap=True)
    rig.at(at(2027, 1, 3, 12))
    activate(rig.db)
    add_run(rig.db, "x-cloud", "cloud", "CLOUD_COMPLETE", at(2027, 1, 2, 18))
    s = rig.status()
    assert s["cloud_next"]["timing"]["state"] == "overdue"
    assert s["cloud"]["timing"]["state"] == "on_time"


def test_strip_per_fy(rig):
    """U08 (Q4)."""
    rig.use(overlap=True)
    activate(rig.db)
    add_run(rig.db, "c9-cloud", "cloud", "CLOUD_COMPLETE", at(2027, 1, 9, 18))
    add_run(rig.db, "n9-cloud-FY27-28", "cloud", "CLOUD_FAILED", at(2027, 1, 9, 19), metrics={"fy": "FY27-28"})
    s = rig.status()
    assert s["cloud"]["nights"][-2]["state"] == "ok"
    assert s["cloud_next"]["nights"][-2]["state"] == "bad"


def test_nights_without_card_fy_is_baseline(rig):
    """U09: card_fy=None is today's strip of every row of the leg."""
    seed_baseline(rig.db)
    days = rig.cfg.dashboard.history_nights
    end = pendulum.now("Asia/Kolkata").add(days=1).start_of("day")
    expected = night_strip.build(rig.db.get_runs_between(end.subtract(days=days), end, "cloud"), days=days)
    assert ui._nights(rig.db, "cloud", rig.cfg, card_fy=None) == expected
    assert ui._nights(rig.db, "cloud", rig.cfg) == expected


def test_strip_after_switch_shows_own_year(rig):
    """U10 (Q4, Q14)."""
    rig.use(src=NEXT_SRC, nas=NEXT_NAS, overlap=True)
    rig.at(at(2027, 4, 3, 12))
    seed_switch(rig.db, late_ok=True)
    s = rig.status()
    assert "cloud_next" not in s
    for day in ("2027-03-28", "2027-03-29", "2027-03-30", "2027-03-31"):
        assert night(s["cloud"], day) == "ok", day
    assert night(s["cloud"], "2027-04-01") == "none"
    assert night(s["cloud"], "2027-04-02") == "ok"


def test_last_run_names_its_real_fy_after_switch(rig):
    """U11 (Q5)."""
    rig.use(src=NEXT_SRC, nas=NEXT_NAS, overlap=True)
    rig.at(at(2027, 4, 3, 12))
    seed_switch(rig.db, late_ok=False)
    s = rig.status()
    assert s["cloud"]["last_run"]["status"] == "CLOUD_FAILED" and s["cloud"]["last_run"]["fy"] == "FY26-27"
    rows = [(r["status"], r["fy"]) for r in s["recent_runs"] if r["mode"] == "cloud"]
    assert rows == [("CLOUD_FAILED", "FY26-27"), ("CLOUD_FAILED", "FY26-27"),
                    ("CLOUD_COMPLETE", "FY27-28"), ("CLOUD_FAILED", "FY26-27"),
                    ("CLOUD_COMPLETE", "FY27-28"), ("CLOUD_FAILED", "FY26-27"),
                    ("CLOUD_COMPLETE", "FY27-28"), ("CLOUD_FAILED", "FY26-27"),
                    ("CLOUD_COMPLETE", "FY27-28"), ("CLOUD_FAILED", "FY26-27")][:len(rows)]
    assert len(rows) == 10


def test_row_fy_label_rules(tmp_path):
    """U12 (K4): suffix, metrics fy, guards fy, before the switch, configured, never-overlap None."""
    def fresh(name):
        folder = tmp_path / name
        folder.mkdir()
        return ManifestDB(str(folder / "m.db"))

    # never any overlap: baseline, no labels
    db = fresh("never")
    cfg = _cfg(tmp_path / "never", overlap=False)
    assert attention.row_fy_label(db, cfg, {"run_id": "x-cloud", "started_at": iso(NOW)}) is None
    db.close()
    # overlap on but no record yet: still no labels
    db = fresh("norecord")
    cfg = _cfg(tmp_path / "norecord", overlap=True)
    assert attention.row_fy_label(db, cfg, {"run_id": "x-cloud", "started_at": iso(NOW)}) is None
    # overlap active: the row's own FY, else the configured one
    activate(db)
    plain = {"run_id": "x-cloud", "started_at": iso(NOW)}
    assert attention.row_fy_label(db, cfg, {"run_id": "x-cloud-FY27-28", "started_at": iso(NOW)}) == "FY27-28"
    assert attention.row_fy_label(db, cfg, {**plain, "extended_metrics": json.dumps({"fy": "FY27-28"})}) == "FY27-28"
    assert attention.row_fy_label(db, cfg, {**plain, "extended_metrics": json.dumps({"guards": {"fy": "FY26-27"}})}) == "FY26-27"
    assert attention.row_fy_label(db, cfg, plain) == "FY26-27"
    assert attention.row_fy_label(db, cfg, None) is None
    db.close()
    # after the switch: an unsuffixed row from before ended_at is the previous FY, a later one the configured FY
    db = fresh("switched")
    cfg = _cfg(tmp_path / "switched", src=NEXT_SRC, nas=NEXT_NAS, overlap=True)
    db.set_overlap_record("FY27-28", {"activated_at": ACTIVATED, "from_fy": "FY26-27", "ended_at": SWITCHED})
    assert attention.row_fy_label(db, cfg, {"run_id": "x-cloud", "started_at": iso(at(2027, 4, 1, 18))}) == "FY26-27"
    assert attention.row_fy_label(db, cfg, {"run_id": "y-cloud", "started_at": iso(at(2027, 4, 2, 18))}) == "FY27-28"
    db.close()


def _schedule_record(db, cfg, **flags):
    db.set_schedule_in_effect({"cloud_cron": cfg.schedule.cloud_cron, "lan_cron": cfg.schedule.lan_cron,
                               "timezone": cfg.schedule.timezone, **flags})


def test_manual_only_leg_card(rig):
    """U13 (Q6)."""
    cfg = rig.use(overlap=False, lan_scheduled=False)
    _schedule_record(rig.db, cfg, lan_scheduled=False)
    s = rig.status()
    assert s["lan"]["scheduled"] is False
    assert s["lan"]["schedule_text"] == "Runs only when you press Run backup"
    assert s["lan"]["timing"] == {"state": "manual", "due_at": None, "overdue_at": None}
    assert s["legs"]["lan"] == "manual" and s["legs"]["cloud"] == "scheduled"


def test_scheduled_follows_the_running_scheduler(rig):
    """U14 (Q6): Settings saved, scheduler not restarted yet."""
    cfg = rig.use(overlap=False, lan_scheduled=False)
    _schedule_record(rig.db, cfg)
    add_run(rig.db, "x-lan", "lan", "LAN_COMPLETE", at(2027, 1, 10, 1))
    s = rig.status()
    assert s["lan"]["scheduled"] is True and s["lan"]["scheduled_setting"] is False
    assert s["lan"]["timing"]["state"] != "manual"


def test_schedule_text_matches_template(rig):
    """U15."""
    s = rig.status()
    sched = rig.cfg.schedule
    assert s["cloud"]["schedule_text"] == "Next scheduled backup: " + cron_to_human(sched.cloud_cron, sched.timezone)
    assert s["lan"]["schedule_text"] == "Next scheduled backup: " + cron_to_human(sched.lan_cron, sched.timezone)


def test_legs_plan_texts(tmp_path):
    """U16 (K12): the exact Q7 texts."""
    def plan(cfg, sched=None, active=False, fys=None):
        return ui._legs_plan(cfg, sched, active, fys)

    both = _cfg(tmp_path / "a")
    p = plan(both)
    assert (p["cloud"], p["lan"]) == ("scheduled", "scheduled")
    assert p["summary"] == "Both copies run every night on their schedule."
    p = plan(_cfg(tmp_path / "b", lan=False))
    assert (p["cloud"], p["lan"]) == ("scheduled", "off")
    assert p["summary"] == "Only the online copy is in use (the office NAS copy is switched off)."
    p = plan(_cfg(tmp_path / "c", cloud=False))
    assert (p["cloud"], p["lan"]) == ("off", "scheduled")
    assert p["summary"] == "Only the office NAS copy is in use (the online copy is switched off)."
    p = plan(_cfg(tmp_path / "d", lan_scheduled=False))
    assert (p["cloud"], p["lan"]) == ("scheduled", "manual")
    assert p["summary"] == ("The online copy runs on its schedule; the office NAS copy runs only when you press "
                            "Run backup.")
    p = plan(_cfg(tmp_path / "e", cloud_scheduled=False))
    assert (p["cloud"], p["lan"]) == ("manual", "scheduled")
    assert p["summary"] == ("The office NAS copy runs on its schedule; the online copy runs only when you press "
                            "Run backup.")
    # the scheduler in effect wins over the configured switch
    p = plan(both, {"cloud_scheduled": True, "lan_scheduled": False})
    assert p["lan"] == "manual"
    # one off and the other manual cannot pass the config validator, the text is still defined
    odd = SimpleNamespace(cloud=SimpleNamespace(enabled=True, scheduled=False),
                          lan=SimpleNamespace(enabled=False, scheduled=True))
    assert plan(odd)["summary"] == "Only the online copy is in use, and it runs only when you press Run backup."
    p = plan(both, None, True, ("FY26-27", "FY27-28"))
    assert p["summary"] == ("Both copies run every night on their schedule. "
                            "Until the April switch both FY26-27 and FY27-28 are backed up.")


def test_disabled_leg_has_one_card(rig):
    """U17."""
    rig.use(overlap=True, lan=False)
    activate(rig.db)
    s = rig.status()
    assert "lan_next" not in s and s["lan"]["enabled"] is False
    assert "cloud_next" in s


def test_status_record_error_fails_visible(rig, monkeypatch):
    """U18."""
    rig.use(overlap=True)

    def boom(fy):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rig.db, "overlap_record", boom)
    s = rig.status()
    assert s["overlap"]["active"] is None and s["overlap"]["error"]
    assert "cloud_next" not in s and "lan_next" not in s
    assert any(i["code"] == "overlap:unknown" for i in s["attention"])


def test_status_with_minimal_config(rig):
    """U19: the SimpleNamespace config of tests/test_m3_dashboard_truth.py."""
    rig.cfg = SimpleNamespace(
        cloud=SimpleNamespace(enabled=True), lan=SimpleNamespace(enabled=True),
        dashboard=SimpleNamespace(stale_grace_hours=attention_cfg.GRACE_HOURS, alert_failure_window_days=7,
                                  **ui_login.DASHBOARD, public_url="http://backup-server:8080"),
        paths=SimpleNamespace(source_drive="E:\\DATA\\FY26-27"), firm_name="T",
        notifications=SimpleNamespace(send_on_failure=True),
        schedule=attention_cfg.SCHEDULE, budget=attention_cfg.BUDGET)
    s = rig.status()
    assert s["overlap"]["active"] is False
    assert s["cloud"]["scheduled"] is True and s["lan"]["scheduled"] is True


# ── R: running state per card ─────────────────────────────────────────────

@pytest.mark.parametrize("value, running, run_state", [(True, True, "running"), (False, False, "idle"),
                                                       (None, False, "unknown")])
def test_running_without_overlap_is_baseline(rig, value, running, run_state):
    """R01."""
    rig.is_running.return_value = value
    s = rig.status()
    for leg in ("cloud", "lan"):
        assert s[leg]["running"] is running and s[leg]["run_state"] == run_state
    rig.active_runs.assert_not_called()


T0 = "2027-01-10T11:00:00+05:30"


def test_queued_next_fy_card(rig):
    """R02 (K7 + /status)."""
    cur = ui._card_run_state("FY26-27", False, [{"fy": None, "started_at": T0}],
                             {"status": "CLOUD_RUNNING", "started_at": "2027-01-10T11:00:05+05:30"})
    nxt = ui._card_run_state("FY27-28", True, [{"fy": None, "started_at": T0}],
                             {"status": "CLOUD_COMPLETE", "started_at": "2027-01-09T18:00:00+05:30"})
    assert (cur["run_state"], cur["running"], cur["leg_busy"]) == ("running", True, True)
    assert (nxt["run_state"], nxt["running"], nxt["leg_busy"]) == ("queued", False, True)
    # through /status
    rig.use(overlap=True)
    activate(rig.db)
    add_run(rig.db, "x-cloud", "cloud", "CLOUD_RUNNING", at(2027, 1, 10, 11, 0, 5))
    add_run(rig.db, "n9-cloud-FY27-28", "cloud", "CLOUD_COMPLETE", at(2027, 1, 9, 18), metrics={"fy": "FY27-28"})
    rig.active_runs.side_effect = lambda leg: [{"fy": None, "started_at": T0}] if leg == "cloud" else []
    s = rig.status()
    assert s["cloud"]["run_state"] == "running" and s["cloud"]["leg_busy"] is True
    assert s["cloud_next"]["run_state"] == "queued" and s["cloud_next"]["leg_busy"] is True
    assert s["cloud_next"]["run_note"] == "Waits for the FY26-27 backup to finish"
    assert s["lan"]["run_state"] == "idle" and s["lan"]["leg_busy"] is False


def test_configured_done_next_running():
    """R03."""
    runs = [{"fy": None, "started_at": T0}]
    cur = ui._card_run_state("FY26-27", False, runs,
                             {"status": "CLOUD_COMPLETE", "started_at": "2027-01-10T11:01:00+05:30"})
    nxt = ui._card_run_state("FY27-28", True, runs,
                             {"status": "CLOUD_RUNNING", "started_at": "2027-01-10T11:05:00+05:30"})
    assert (cur["run_state"], cur["leg_busy"]) == ("idle", True)
    assert (nxt["run_state"], nxt["leg_busy"]) == ("running", True)


def test_starting_run_counts_for_configured():
    """R04: the run started, no row of it yet."""
    runs = [{"fy": None, "started_at": T0}]
    cur = ui._card_run_state("FY26-27", False, runs,
                             {"status": "CLOUD_COMPLETE", "started_at": "2027-01-09T18:00:00+05:30"})
    nxt = ui._card_run_state("FY27-28", True, runs, None)
    assert cur["run_state"] == "running" and nxt["run_state"] == "queued"


def test_fy_run_only_marks_its_card():
    """R05 (and no active run -> idle)."""
    runs = [{"fy": "FY27-28", "started_at": T0}]
    nxt = ui._card_run_state("FY27-28", True, runs, None)
    cur = ui._card_run_state("FY26-27", False, runs,
                             {"status": "CLOUD_COMPLETE", "started_at": "2027-01-09T18:00:00+05:30"})
    assert nxt["run_state"] == "running" and nxt["running"] is True
    assert cur["run_state"] == "idle" and cur["leg_busy"] is True
    idle = ui._card_run_state("FY26-27", False, [], None)
    assert idle["run_state"] == "idle" and idle["leg_busy"] is False


def test_unreachable_is_unknown_on_all_cards():
    """R06."""
    for card_fy, extra in (("FY26-27", False), ("FY27-28", True)):
        got = ui._card_run_state(card_fy, extra, None, None)
        assert got["run_state"] == "unknown" and got["running"] is False


class _FakeClient:
    def __init__(self, runs=None, exc=None):
        self._runs, self._exc = runs or [], exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read_flow_runs(self, flow_run_filter=None, limit=None):
        if self._exc is not None:
            raise self._exc
        return self._runs


def _flow_run(state_type, mode, *, fy=None, tags=None, start=None, due=None):
    params = {"mode": mode}
    if fy:
        params["fy"] = fy
    return SimpleNamespace(
        id="11111111-2222-3333-4444-555555555555", tags=tags or [], parameters=params,
        state=SimpleNamespace(type=state_type, name=str(state_type), is_final=lambda: False),
        state_type=state_type, next_scheduled_start_time=due, start_time=start, expected_start_time=start)


def test_active_leg_runs_filter(monkeypatch):
    """R08 (K6)."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    started = datetime(2027, 1, 10, 6, 30, tzinfo=timezone.utc)
    runs = [
        _flow_run(StateType.RUNNING, "cloud", fy="FY27-28", start=started),
        _flow_run(StateType.RUNNING, "all", tags=["maintenance", "integrity"], start=started),
        _flow_run(StateType.SCHEDULED, "cloud", due=datetime.now(timezone.utc) + timedelta(days=1)),
        _flow_run(StateType.RUNNING, "lan", start=started),
    ]
    monkeypatch.setattr(ui, "get_client", lambda: _FakeClient(runs=runs))
    assert asyncio.run(ui._active_leg_runs("cloud")) == [{"fy": "FY27-28", "started_at": started.isoformat()}]
    monkeypatch.setattr(ui, "get_client", lambda: _FakeClient(exc=ConnectionError("api down")))
    assert asyncio.run(ui._active_leg_runs("cloud")) is None


# ── T: triggers with ?fy= ─────────────────────────────────────────────────

@pytest.mark.parametrize("overlap", [False, True])
def test_trigger_without_fy_is_the_baseline_call(rig, overlap):
    """T01."""
    rig.use(overlap=overlap)
    if overlap:
        activate(rig.db)
    for url, name in (("/trigger/cloud?wait=false", "aam-backup/backup-cloud"),
                      ("/trigger/lan?wait=false", "aam-backup/backup-lan")):
        r = rig.post(url)
        assert r.status_code == 202, r.text
        call = rig.calls[-1]
        assert call["args"] == () and call["kwargs"] == {"name": name, "timeout": 0}
    assert rig.db_calls == 0


def test_trigger_next_fy(rig):
    """T02."""
    rig.use(overlap=True)
    activate(rig.db)
    r = rig.post("/trigger/lan?fy=FY27-28&wait=false")
    assert r.status_code == 202, r.text
    assert rig.params() == {"fy": "FY27-28"}
    assert rig.calls[-1]["kwargs"]["name"] == "aam-backup/backup-lan"


@pytest.mark.parametrize("overlap", [False, True])
def test_trigger_configured_fy_upper_cased(rig, overlap):
    """T03."""
    rig.use(overlap=overlap)
    if overlap:
        activate(rig.db)
    r = rig.post("/trigger/cloud?fy=fy26-27&wait=false")
    assert r.status_code == 202, r.text
    assert rig.params() == {"fy": "FY26-27"}


def test_trigger_inactive_fy_is_409(rig):
    """T04."""
    rig.use(overlap=True)
    r = rig.post("/trigger/cloud?fy=FY27-28&wait=false")
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "fy_not_active"
    assert "FY27-28 is not backed up at the moment (backed up: FY26-27)" in body["detail"]
    activate(rig.db)
    r = rig.post("/trigger/cloud?fy=FY28-29&wait=false")
    assert r.status_code == 409 and r.json()["status"] == "fy_not_active"
    assert "FY28-29 is not backed up at the moment" in r.json()["detail"]
    assert "FY26-27" in r.json()["detail"] and "FY27-28" in r.json()["detail"]
    assert rig.calls == []


@pytest.mark.parametrize("bad", ["2027", "FY27-2"])
def test_trigger_bad_fy_is_400(rig, bad):
    """T05."""
    r = rig.post(f"/trigger/lan?fy={bad}&wait=false")
    assert r.status_code == 400 and r.json()["status"] == "bad_fy"
    assert rig.calls == []


def test_trigger_disabled_wins(rig):
    """T06."""
    from core import messages
    rig.use(overlap=True, lan=False)
    activate(rig.db)
    rig.db_calls = 0
    r = rig.post("/trigger/lan?fy=FY27-28&wait=false")
    assert r.status_code == 409 and r.json()["status"] == "disabled"
    assert r.json()["detail"] == messages.leg_off("lan")
    assert rig.db_calls == 0 and rig.calls == []


def test_trigger_fy_running_and_unreachable(rig):
    """T07."""
    rig.use(overlap=True)
    activate(rig.db)
    rig.is_running.return_value = True
    r = rig.post("/trigger/lan?fy=FY27-28&wait=false")
    assert r.status_code == 400 and r.json()["status"] == "already_running"
    rig.is_running.return_value = None
    r = rig.post("/trigger/lan?fy=FY27-28&wait=false")
    assert r.status_code == 503
    assert rig.calls == []


def test_trigger_fy_record_error_fails_closed(rig, monkeypatch):
    """T08."""
    rig.use(overlap=True)

    def boom(fy):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rig.db, "overlap_record", boom)
    r = rig.post("/trigger/cloud?fy=FY27-28&wait=false")
    assert r.status_code == 503 and r.json()["status"] == "fy_unknown"
    assert rig.calls == []


def test_trigger_manual_only_leg_accepted(rig):
    """T09."""
    rig.use(overlap=True, lan_scheduled=False)
    activate(rig.db)
    assert rig.post("/trigger/lan?wait=false").status_code == 202
    r = rig.post("/trigger/lan?fy=FY27-28&wait=false")
    assert r.status_code == 202, r.text
    assert rig.params() == {"fy": "FY27-28"}


def test_verify_next_fy(rig):
    """T10."""
    rig.use(overlap=True)
    activate(rig.db)
    r = rig.post("/trigger/verify/lan?fy=FY27-28&anyway=1&wait=false")
    assert r.status_code == 202, r.text
    assert rig.params() == {"mode": "lan", "fy": "FY27-28"}
    assert rig.calls[-1]["kwargs"]["name"] == "integrity-audit/integrity-audit"
    assert "FY27-28" in r.json()["detail"]


def test_verify_fy_checks(rig):
    """T12."""
    rig.use(overlap=True)
    r = rig.post("/trigger/verify/lan?fy=FY27-28&anyway=1&wait=false")
    assert r.status_code == 409 and r.json()["status"] == "fy_not_active"
    r = rig.post("/trigger/verify/lan?fy=2027&anyway=1&wait=false")
    assert r.status_code == 400 and r.json()["status"] == "bad_fy"
    assert rig.calls == []


def test_backup_first_uses_the_cards_fy(rig):
    """T13 (Q10)."""
    rig.use(overlap=True)
    activate(rig.db)
    add_run(rig.db, "x-cloud", "cloud", "CLOUD_COMPLETE", at(2027, 1, 10, 11, 55))
    r = rig.post("/trigger/verify/cloud?fy=FY27-28&wait=false")
    assert r.status_code == 409 and r.json()["status"] == "backup_first"
    assert "there is no successful cloud backup yet" in r.json()["detail"]
    r = rig.post("/trigger/verify/cloud?wait=false")
    assert r.status_code == 202, r.text


# ── P: approved re-runs ───────────────────────────────────────────────────

GUARDS = [{"guard": "G3_MASS_DELETE", "reason": "This backup would delete 600 of 1,000",
           "metrics": {"planned_deletes": 600, "delete_percent": 60.0}}]


def _request(rig, leg="lan", fy="FY26-27"):
    return rig.db.open_approval_request(
        leg=leg, blocked_run_id="run-b", fy=fy, guards=GUARDS, phrase=ap.request_phrase(GUARDS),
        created_at=pendulum.now("UTC").isoformat())


def _approve(rig, aid):
    return rig.post(f"/approvals/{aid}/approve?wait=false", json={"phrase": "APPROVE"})


def test_approved_rerun_baseline_parameters(rig):
    """P01."""
    aid = _request(rig, fy="FY26-27")
    r = _approve(rig, aid)
    assert r.status_code == 202, r.text
    assert rig.params() == {"approval_id": aid}


@pytest.mark.parametrize("fy", ["FY27-28", "FY26-27"])
def test_approved_rerun_passes_fy(rig, fy):
    """P02."""
    rig.use(overlap=True)
    activate(rig.db)
    aid = _request(rig, fy=fy)
    r = _approve(rig, aid)
    assert r.status_code == 202, r.text
    assert rig.params() == {"approval_id": aid, "fy": fy}


def test_approved_rerun_without_fy(rig):
    """P03."""
    rig.use(overlap=True)
    activate(rig.db)
    aid = _request(rig, fy=None)
    assert _approve(rig, aid).status_code == 202
    assert rig.params() == {"approval_id": aid}


def test_approval_for_inactive_next_fy_refused_before_approving(rig):
    """P04 (Q3)."""
    rig.use(overlap=False)
    aid = _request(rig, fy="FY27-28")
    r = _approve(rig, aid)
    assert r.status_code == 409
    assert "FY27-28, which is not backed up at the moment" in r.text
    assert rig.db.get_approval(aid)["status"] == ap.PENDING
    assert rig.calls == []
    rig.db.approve_request(aid, approved_by="x", approved_at=pendulum.now("UTC").isoformat(),
                           expires_at=ap.expires_at(pendulum.now("UTC").isoformat(), 12))
    r = rig.post(f"/approvals/{aid}/run?wait=false")
    assert r.status_code == 409 and rig.calls == []


def test_approval_record_error_fails_closed(rig, monkeypatch):
    """P05."""
    rig.use(overlap=True)
    aid = _request(rig, fy="FY27-28")

    def boom(fy):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rig.db, "overlap_record", boom)
    r = _approve(rig, aid)
    assert r.status_code == 503
    assert rig.db.get_approval(aid)["status"] == ap.PENDING
    assert rig.calls == []


def test_rollover_approval_unchanged(rig):
    """P06."""
    rig.use(overlap=True)
    activate(rig.db)
    aid = _request(rig, leg="rollover", fy="FY26-27")
    r = _approve(rig, aid)
    assert r.status_code == 202, r.text
    assert rig.params() == {"approval_id": aid}
    assert rig.calls[-1]["kwargs"]["name"] == "rollover-check/rollover-check"


# ── O: /api/rollover ──────────────────────────────────────────────────────

def _rollover(rig):
    r = rig.client.get("/api/rollover")
    assert r.status_code == 200, r.text
    return r.json()


def test_api_rollover_overlap_block(rig):
    """O01: before / pending / active / after the switch."""
    keys = {"enabled", "active", "configured_fy", "next_fy", "activates_on", "activated_at", "ended_at",
            "activation_pending", "note"}
    rig.use(overlap=True)
    rig.at(at(2026, 12, 30, 12))
    o = _rollover(rig)["overlap"]
    assert set(o) == keys and o["active"] is False and o["activation_pending"] is False
    assert o["activates_on"] == "2026-12-31" and "31 Dec 2026" in o["note"]
    rig.at(at(2027, 1, 1, 8))
    o = _rollover(rig)["overlap"]
    assert o["activation_pending"] is True and "not added" in o["note"]
    activate(rig.db)
    rig.at(NOW)
    o = _rollover(rig)["overlap"]
    assert o["active"] is True and o["activation_pending"] is False
    assert (o["configured_fy"], o["next_fy"], o["activated_at"], o["ended_at"]) == (
        "FY26-27", "FY27-28", ACTIVATED, None)
    assert "FY26-27" in o["note"] and "FY27-28" in o["note"]
    rig.use(src=NEXT_SRC, nas=NEXT_NAS, overlap=True)
    rig.db.set_overlap_record("FY27-28", {"activated_at": ACTIVATED, "from_fy": "FY26-27", "ended_at": SWITCHED})
    rig.at(at(2027, 4, 3, 12))
    o = _rollover(rig)["overlap"]
    assert o["active"] is False and o["configured_fy"] == "FY27-28" and o["ended_at"] == SWITCHED
    assert "FY28-29" in o["note"] and "31 Dec 2027" in o["note"]


def test_api_rollover_final_copy_state(rig):
    """O02."""
    rig.use(overlap=True)
    activate(rig.db)
    rig.at(at(2027, 4, 1, 10))
    add_run(rig.db, "x-cloud", "cloud", "CLOUD_COMPLETE", at(2027, 4, 1, 6), metrics={"guards": {"fy": "FY26-27"}})
    d = _rollover(rig)
    assert d["final_copy"]["cloud"]["done"] is True
    assert d["final_copy"]["lan"]["outstanding"] is True and d["final_copy"]["lan"]["hint"]
    assert d["overlap"]["active"] is True


def test_api_rollover_no_final_copy_without_overlap(rig):
    """O03."""
    rig.use(overlap=True)
    assert _rollover(rig)["final_copy"] is None


def test_rollover_run_allowed_when_activation_pending(rig):
    """O04."""
    rig.use(overlap=True)
    rig.at(at(2027, 1, 1, 8))
    d = _rollover(rig)
    assert d["can_run"] is True and d["message"] == d["overlap"]["note"]
    r = rig.post("/api/rollover/run?wait=false")
    assert r.status_code == 202, r.text
    assert rig.calls[-1]["kwargs"]["name"] == "rollover-check/rollover-check"
    rig.calls.clear()
    rig.at(at(2026, 12, 30, 12))
    assert rig.post("/api/rollover/run?wait=false").status_code == 409
    assert rig.calls == []


def test_activation_pending_matches_attention(rig):
    """O05: fy_overlap.activation_pending is the rule of attention._activation_item."""
    import datetime as dt
    cfg = rig.use(overlap=True)
    days = [at(2026, 12, 30, 12), at(2026, 12, 31, 12), at(2027, 1, 1, 12), at(2027, 1, 2, 12),
            at(2027, 1, 15, 12), at(2027, 3, 31, 12)]
    for when in days:
        with pendulum.travel_to(when, freeze=True):
            got = fy_overlap.activation_pending(cfg, rig.db, when.date())
            item = attention._activation_item(rig.db, cfg, when)
        if when.date() < dt.date(2026, 12, 31):
            assert got is None and item is None, when
        else:
            assert got == ("FY27-28", dt.date(2026, 12, 31)), when
            if when.date() > dt.date(2026, 12, 31):
                assert item is not None, when
    activate(rig.db)
    assert fy_overlap.activation_pending(cfg, rig.db, dt.date(2027, 1, 2)) is None
    off = rig.use(overlap=False)
    assert fy_overlap.activation_pending(off, rig.db, dt.date(2027, 1, 2)) is None


def test_k1_active_fys(rig):
    """K1: [] without an FY folder, [configured], [configured, next]."""
    from types import SimpleNamespace
    no_fy = SimpleNamespace(paths=SimpleNamespace(source_drive="E:\\data", lan_destination="\\\\nas\\backup"),
                            rollover=SimpleNamespace(overlap_enabled=True))
    assert fy_overlap.active_fys(no_fy, rig.db) == []
    cfg = rig.use(overlap=True)
    assert fy_overlap.active_fys(cfg, rig.db) == ["FY26-27"]
    activate(rig.db)
    assert fy_overlap.active_fys(cfg, rig.db) == ["FY26-27", "FY27-28"]


# ── J: page and scripts ───────────────────────────────────────────────────

def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _js_func(src, name):
    m = re.search(r"(?:async\s+)?function\s+%s\s*\(" % re.escape(name), src)
    assert m, f"function {name} not found"
    nxt = re.search(r"\n(?:async\s+)?function\s", src[m.end():])
    return src[m.start(): m.end() + (nxt.start() if nxt else len(src))]


def test_js_one_card_renderer():
    """J01."""
    src = _read("static/js/dashboard.js")
    assert "function renderLegCard(key, d)" in src
    assert "badgeCloud" not in src and "badgeLan" not in src
    for name in ("setServerUnreachable", "setSessionExpired", "renderLegVisuals"):
        body = _js_func(src, name)
        assert "_CARD_KEYS" in body or "['cloud', 'lan', 'cloud-next', 'lan-next']" in body, name


def test_js_buttons_send_fy():
    """J02."""
    src = _read("static/js/dashboard.js")
    backup = _js_func(src, "triggerBackup")
    assert "'/trigger/'" in backup and re.search(r"\?fy='\s*\+\s*encodeURIComponent\(fy\)", backup)
    verify = _js_func(src, "triggerVerify")
    assert "anyway=1" in verify and "encodeURIComponent(fy)" in verify
    assert "dataset.fy" in src


def test_template_next_cards_hidden_by_default():
    """J03."""
    tpl = _read("templates/dashboard.html")
    m = re.search(r'<div[^>]*id="dests-next"[^>]*>', tpl)
    assert m and re.search(r"\bhidden\b", m.group(0))
    for key in ("card-cloud-next", "card-lan-next"):
        assert tpl.find(key) > m.start(), key
    assert "data.overlap && data.overlap.active" in _read("static/js/dashboard.js")


def test_js_labels_and_states():
    """J04."""
    src = _read("static/js/dashboard.js")
    assert re.search(r"fy\s*&&\s*fy\s*!==\s*cardFy", src)
    assert re.search(r"!==\s*data\.fy_prefix", src)
    assert re.search(r"run_state\s*===\s*'queued'", src)
    assert "'manual'" in src


def test_settings_js_final_copy():
    """J05."""
    src = _read("static/js/settings.js")
    body = _js_func(src, "loadRollover")
    assert "d.overlap" in body and "note" in body and "d.final_copy" in body
    for word in ("outstanding", "hint", "done_at"):
        assert word in src, word


def test_dashboard_page_baseline_text(rig):
    """J06."""
    rig.use(overlap=False)
    r = rig.client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    for text in ("Online copy (cloud)", "Office copy (NAS)", "Next scheduled backup: "):
        assert text in r.text, text
    rig.use(overlap=False, lan=False)
    r = rig.client.get("/", headers={"Accept": "text/html"})
    assert "LAN backup is disabled in configuration" in r.text


# ── E: real server (extension of tests/test_e8_dashboard_e2e.py) ──────────

@pytest.fixture
def server(tmp_path, monkeypatch):
    d = _config_dict(tmp_path)
    d["dashboard"] = dict(ui_login.DASHBOARD)
    d.setdefault("rollover", {})["overlap_enabled"] = True
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(d), encoding="utf-8")
    import models.config as mconfig
    monkeypatch.setattr(mconfig, "CONFIG_PATH", str(cfg_path))
    db = ManifestDB(str(tmp_path / "m.db"))
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_settings_config_path", lambda: str(cfg_path))
    monkeypatch.setattr(ui, "_settings_lock_busy", lambda: None)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_active_leg_runs", AsyncMock(return_value=[]), raising=False)
    calls = []

    async def fake_arun(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id="flow-run-1", state=None)

    monkeypatch.setattr(ui, "arun_deployment", fake_arun)
    ui._config, ui._config_loaded_at = None, 0.0
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(ui.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield SimpleNamespace(base=f"http://127.0.0.1:{port}", db=db, calls=calls)
    srv.should_exit = True
    t.join(5)
    db.close()
    ui._config, ui._config_loaded_at = None, 0.0


def _signed_in(base):
    c = httpx.Client(base_url=base, follow_redirects=False, headers={"Accept": "application/json"})
    r = c.post("/login", data={"password": ui_login.LOGIN_PASSWORD})
    assert r.status_code == 303, r.text
    return c


@pytest.mark.anonymous
def test_four_cards_with_overlap(server):
    """E01."""
    server.db.set_overlap_record("FY27-28", {"activated_at": ACTIVATED, "from_fy": "FY26-27"})
    c = _signed_in(server.base)
    page = c.get("/", headers={"Accept": "text/html"})
    assert page.status_code == 200
    for key in ("card-cloud", "card-lan", "card-cloud-next", "card-lan-next"):
        assert key in page.text, key
    s = c.get("/status").json()
    assert s["cloud_next"]["fy"] == "FY27-28" and s["lan_next"]["fy"] == "FY27-28"
    assert s["overlap"]["active"] is True


@pytest.mark.anonymous
def test_per_card_buttons_send_their_fy(server):
    """E02."""
    server.db.set_overlap_record("FY27-28", {"activated_at": ACTIVATED, "from_fy": "FY26-27"})
    c = _signed_in(server.base)
    assert c.post("/trigger/cloud?fy=FY27-28&wait=false").status_code == 202
    assert server.calls[-1]["parameters"] == {"fy": "FY27-28"}
    assert c.post("/trigger/lan?fy=FY26-27&wait=false").status_code == 202
    assert server.calls[-1]["parameters"] == {"fy": "FY26-27"}
    assert c.post("/trigger/verify/lan?fy=FY27-28&anyway=1&wait=false").status_code == 202
    assert server.calls[-1]["parameters"] == {"mode": "lan", "fy": "FY27-28"}
    assert c.post("/trigger/cloud?fy=FY29-30&wait=false").status_code == 409
    anon = httpx.Client(base_url=server.base, headers={"Accept": "application/json"})
    assert anon.post("/trigger/cloud?fy=FY27-28&wait=false").status_code == 401


@pytest.mark.anonymous
def test_two_cards_without_overlap(server):
    """E03."""
    c = _signed_in(server.base)
    s = c.get("/status").json()
    assert "cloud_next" not in s and "lan_next" not in s
    assert c.post("/trigger/cloud?wait=false").status_code == 202
    assert "parameters" not in server.calls[-1]
