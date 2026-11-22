"""FY overlap timelines D, E and F plus point cards P-7 (agent_runs/OVERLAP_REVIEW.md, section 2).

  T-d1  activation deferred all quarter (no right to create the folder), then the 1 April rollover
  T-d2  overlap OFF all year, then the 1 April rollover (old behaviour)
  T-e1  activated 1 Jan, overlap switched off 21 Jan (sibling alarm daily, D9)
  T-e2  overlap switched back on 10 Feb
  T-e3  weekly report after the switch-off (D10)
  T-f1  crash after the config write (R3)
  T-f2  archive step fails and is retried (gcloud argv)
  T-f3  close_fy raises once at the switch (D2)
  P-7   final backup cannot get the slot on 1 April (D7)

Rig: one real config, one real manifest DB, real fy_overlap / rollover journal / notify (mail counted at a real
SMTP server). Faked: the pipelines (they record a run row for the FY they were handed), the Google/NTP clock
check, the Prefect backup slot, the config.yaml write, gcloud. The clock is moved with pendulum.travel_to
(in-process only). Oracles are hand-counted dates, folder listings, argv lists, DB rows and SMTP mail counts.
Cards that prove a defect are written for the CORRECT behaviour and marked xfail with the defect id."""

import email
import json
import os
import pathlib
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pendulum
import pytest
from fastapi.testclient import TestClient

import flow
import ui
from core import approvals as ap
from core import fy_overlap as ov
from core import fy_rollover as fr
from core import guards as g
from core import report
from core import rollover_journal as rj
from core.health import CLOCK_OK, ClockResult
from core.manifest import ManifestDB
from core.report_period import ReportPeriod
from core.time_utils import IST
from core.tool_runner import ToolResult
from models.config import (AppConfig, CloudConfig, LanConfig, PathsConfig, RolloverConfig, WolConfig)
from test_support import ui_login
from test_support.smtp_server import smtp_server
from tests.test_m1_notify import _decoded

A, B, C = "FY26-27", "FY27-28", "FY28-29"
BUCKET = "my-bucket"
GUARD = [{"guard": "G3_MASS_DELETE", "reason": "x", "metrics": {"planned_deletes": 500, "delete_percent": 5.0}}]


def at(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


class _Crash(BaseException):
    """The process dies (power cut): not an Exception, so no step handler records anything."""


def _files_in(folder) -> int:
    return sum(len(files) for _, _, files in os.walk(folder))


def _mails(srv) -> list[str]:
    """Subject plus decoded body of every mail the SMTP server received."""
    out = []
    for m in srv.messages:
        msg = email.message_from_string(m["data"])
        subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
        out.append(subject + "\n" + _decoded(m["data"]))
    return out


def _count(srv, needle: str) -> int:
    return sum(1 for text in _mails(srv) if needle in text)


class Site:
    """One office: real folders under tmp, real config object, real DB, fake outside world."""

    def __init__(self, tmp_path, monkeypatch, srv, *, fake_pipelines=True):
        self.tmp, self.mp, self.srv = tmp_path, monkeypatch, srv
        self.src_root, self.nas_root = tmp_path / "src", tmp_path / "nas"
        (self.src_root / A).mkdir(parents=True)
        (self.src_root / A / "ledger.xlsx").write_text("FY26-27 books", encoding="utf-8")
        (self.nas_root / A).mkdir(parents=True)
        (self.nas_root / A / g.MOUNT_MARKER).touch()
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}', encoding="utf-8")
        cfg = AppConfig(
            firm_name="AAM",
            paths=PathsConfig(source_drive=str(self.src_root / A), lan_destination=f"\\\\nas\\share\\{A}",
                              gcs_key_path=str(key), runtime_dir=str(tmp_path / "rt")),
            lan=LanConfig(enabled=True), wol=WolConfig(enabled=False),
            cloud=CloudConfig(enabled=True, bucket=BUCKET, project_number="123"),
            rollover=RolloverConfig(), schedule={"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *"},
            dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
            notifications={"smtp_host": "127.0.0.1", "smtp_port": srv.server_address[1],
                           "sender": "b@firm.example", "recipients": ["ops@firm.example"],
                           "smtp_security": "none", "smtp_auth": False, "retry_attempts": 1,
                           "retry_base_seconds": 0.01})
        object.__setattr__(cfg.paths, "lan_destination", str(self.nas_root / A))   # a folder we can create in
        self.cfg = cfg
        self.db_path = cfg.paths.database_path
        self.calls: list[SimpleNamespace] = []     # every pipeline call: leg, fy, when, run_id
        self.outcome = lambda leg, fy, when: f"{leg.upper()}_COMPLETE"
        self.archive_calls: list[dict] = []
        self.archive_ok = True
        self.switch_writes: list[tuple] = []
        self.slot_busy = False
        self._dbs: list[ManifestDB] = []
        self._install(fake_pipelines)

    # -- plumbing ------------------------------------------------------------------------------

    @property
    def db(self) -> ManifestDB:
        if not self._dbs:
            self._dbs.append(ManifestDB(self.db_path))
        return self._dbs[0]

    def close(self):
        for d in self._dbs:
            d.close()

    def _install(self, fake_pipelines):
        mp, site = self.mp, self
        self.real_archive = fr.run_archive_transition
        mp.setattr(flow, "load_config", lambda p: site.cfg)
        mp.setattr(flow, "configure_logging", lambda *a, **k: None)
        mp.setattr(flow, "configure_prefect_bridge", lambda *a, **k: None)
        mp.setattr(flow, "cleanup_orphaned_robocopy_logs", lambda **k: None)
        mp.setattr(flow, "log_retention_task", MagicMock())
        mp.setattr(flow, "check_clock", lambda **k: ClockResult(CLOCK_OK, 1.0, "google", ""))
        mp.setattr(ov, "_default_clock_check", lambda config: ClockResult(CLOCK_OK, 1.0, "google", ""))
        mp.setattr(flow, "ensure_server_online", MagicMock())
        mp.setattr(flow, "lan_shutdown_task", MagicMock(return_value={"outcome": "VERIFIED"}))

        @contextmanager
        def slot(config):
            if site.slot_busy:
                raise TimeoutError("Timed out waiting for the backup slot")
            yield

        mp.setattr(flow, "_backup_slot", slot)
        mp.setattr(flow.fy_rollover, "update_config_yaml", self._write_config)
        mp.setattr(flow.fy_rollover, "run_archive_transition", self._archive)
        if fake_pipelines:
            mp.setattr(flow, "_run_cloud_pipeline", self._pipeline("cloud"))
            mp.setattr(flow, "_run_lan_pipeline", self._pipeline("lan"))

    def _write_config(self, config_path, src_root, lan_root, to_fy, expect_fy=None):
        """What the compare-and-set write of config.yaml does to the loaded settings."""
        self.switch_writes.append((to_fy, expect_fy))
        self.point_config_at(to_fy)
        return "config.yaml.bak"

    def point_config_at(self, fy):
        object.__setattr__(self.cfg.paths, "source_drive", str(self.src_root / fy))
        object.__setattr__(self.cfg.paths, "lan_destination", str(self.nas_root / fy))

    def _archive(self, **kw):
        self.archive_calls.append(kw)
        return self.archive_ok

    def _pipeline(self, leg):
        site = self

        def run(config, run_id, started_at, monotonic_start=None, approval_id=None, **kw):
            fy = flow._effective_fy_prefix(config)
            now = pendulum.now(IST)
            site.calls.append(SimpleNamespace(leg=leg, fy=fy, when=now, run_id=run_id, kw=kw))
            status = site.outcome(leg, fy, now)
            metrics = {"guards": {"fy": fy, "source_file_count": _files_in(config.paths.source_drive),
                                  "destination": config.paths.lan_destination if leg == "lan" else f"gs://{BUCKET}/{fy}"}}
            row = ManifestDB(site.db_path)
            try:
                row.insert_run({"run_id": run_id, "mode": leg, "fy": fy, "started_at": now.isoformat(),
                                "ended_at": now.isoformat(), "status": status, "exit_code": 0,
                                "files_copied": 1, "bytes_copied": 10, "extended_metrics": json.dumps(metrics)})
            finally:
                row.close()
            if not status.endswith("COMPLETE"):
                raise RuntimeError(f"{leg} ended {status}")
            if leg == "lan" and kw.get("shutdown_votes") is not None:
                kw["shutdown_votes"].append((True, "art"))
            return {"status": status}
        return run

    # -- actions (each one on its own frozen instant) ----------------------------------------------

    def backup(self, when, mode="all", **kw):
        with pendulum.travel_to(when, freeze=True):
            return flow.backup.fn("config.yaml", mode, **kw)

    def rollover(self, when, **kw):
        with pendulum.travel_to(when, freeze=True):
            return flow.rollover_check_flow.fn(config_path="config.yaml", **kw)

    def night(self, day, mode="all"):
        """The 21:01 backup of `day` (a pendulum date or datetime)."""
        return self.backup(at(day.year, day.month, day.day, 21, 1), mode)

    def calls_on(self, day):
        return [(c.leg, c.fy) for c in self.calls if c.when.date() == day.date()
                if "-final-" not in c.run_id]

    def activate_early(self, when=None):
        when = when or at(2027, 1, 1, 2, 1)
        self.db.record_activation(B, activated_at=when.isoformat(), source_path=str(self.src_root / B),
                                  created_by_app=True, clock_state="OK")

    def approval(self, fy, leg="lan", approved=False, when=None):
        when = when or at(2027, 3, 30, 3, 0)
        aid = self.db.open_approval_request(leg=leg, blocked_run_id=f"blocked-{fy}-{leg}", fy=fy, guards=GUARD,
                                            phrase="APPROVE", created_at=when.isoformat())
        if approved:
            self.db.approve_request(aid, approved_by="owner", approved_at=when.isoformat(),
                                    expires_at=when.add(days=3).isoformat())
        return aid

    def status_of(self, aid):
        return self.db.get_approval(aid)["status"]

    def steps(self, frm=A, to=B):
        return {k: v["state"] for k, v in self.db.rollover_get(frm, to)["steps"].items()}


@pytest.fixture
def srv():
    with smtp_server() as server:
        yield server


@pytest.fixture
def site(tmp_path, monkeypatch, srv):
    s = Site(tmp_path, monkeypatch, srv)
    yield s
    s.close()
    pendulum.travel_back()


def _nights(site, first, last, mode="all"):
    d = first
    while d <= last:
        site.night(d, mode)
        d = d.add(days=1)


# ===============================================================================================
# T-d1  activation deferred all quarter, rollover on 1 April: early folder cannot be created
# ===============================================================================================

def test_T_d1_deferred_quarter_then_rollover_blocks_the_switch_until_the_folder_can_be_made(site, srv, monkeypatch):
    real_mkdir = pathlib.Path.mkdir

    def mkdir_without_right(self, *a, **k):          # the backup account may not create FY27-28 next to FY26-27
        if self.name.upper() == B and self.parent == site.src_root:
            raise PermissionError(13, "Access is denied", str(self))
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "mkdir", mkdir_without_right)

    # Jan - Mar: every night is FY26-27 only, nothing is activated, the folder is never invented
    for day in (at(2027, 1, 1), at(2027, 2, 15), at(2027, 3, 31)):
        site.rollover(day.add(hours=6))
        site.night(day)
        assert site.calls_on(day) == [("cloud", A), ("lan", A)], day
    assert site.db.get_activation(B) is None
    assert not (site.src_root / B).exists()

    # 1 April 06:00: the early folder step fails -> WARNING; the final backups of FY26-27 still run;
    # the switch waits (source_folder is a blocking step)
    with pytest.raises(fr.RolloverError):
        site.rollover(at(2027, 4, 1, 6, 0))
    st = site.steps()
    assert st[rj.FINAL_BACKUPS] == rj.DONE
    assert st[rj.SOURCE_FOLDER] == rj.FAILED
    assert st[rj.CONFIG_SWITCH] == rj.PENDING and st[rj.ARCHIVE] == rj.PENDING
    finals = [(c.leg, c.fy) for c in site.calls if "-final-" in c.run_id]
    assert sorted(finals) == [("cloud", A), ("lan", A)]
    assert site.switch_writes == [] and site.archive_calls == []
    assert site.cfg.paths.source_drive.endswith(A)
    # the WARNING mail (early step) and the ACTION NEEDED mail (the blocking retry before the switch)
    assert _count(srv, f"The folder {B} for the new financial year could not be created") == 1
    assert _count(srv, "stopped at step 'source folder'") == 1

    # the same evening FY26-27 is still the one backed up (no activation, switch pending)
    site.night(at(2027, 4, 1))
    assert site.calls_on(at(2027, 4, 1)) == [("cloud", A), ("lan", A)]

    # the right is fixed: the next morning the rollover resumes and completes
    monkeypatch.setattr(pathlib.Path, "mkdir", real_mkdir)
    assert site.rollover(at(2027, 4, 2, 6, 0)) == "ROLLOVER_COMPLETED"
    assert all(s in (rj.DONE, rj.SKIPPED) for s in site.steps().values()), site.steps()
    assert (site.src_root / B / site.cfg.rollover.source_placeholder_name).is_file()
    assert site.switch_writes == [(B, A)]
    assert site.archive_calls[0]["old_fy"] == A


# ===============================================================================================
# T-d2  overlap OFF all year: the old behaviour
# ===============================================================================================

def test_T_d2_overlap_off_all_year_old_rollover_behaviour_and_no_false_report(site, srv):
    site.cfg.rollover.overlap_enabled = False
    for day in (at(2027, 1, 1), at(2027, 2, 20)):
        site.night(day)
        assert site.calls_on(day) == [("cloud", A), ("lan", A)]
    assert not (site.src_root / B).exists() and site.db.get_activation(B) is None
    _nights(site, at(2027, 3, 29), at(2027, 3, 31), mode="lan")

    assert site.rollover(at(2027, 4, 1, 6, 0)) == "ROLLOVER_COMPLETED"
    assert (site.src_root / B / site.cfg.rollover.source_placeholder_name).is_file()     # folder + note
    assert (site.nas_root / B / g.MOUNT_MARKER).is_file()                                  # NAS folder + marker
    assert site.switch_writes == [(B, A)]
    assert site.cfg.paths.source_drive.endswith(B)
    assert site.db.get_activation(B) is None

    # first FY27-28 night: one folder only, G1 sees the note, no baseline yet so the first-run rule applies,
    # and an empty cloud prefix (0 planned deletes) passes G3
    _nights(site, at(2027, 4, 1), at(2027, 4, 4), mode="lan")
    assert [(c.leg, c.fy) for c in site.calls if c.when.date() >= at(2027, 4, 1).date() and "-final-" not in c.run_id] \
        == [("lan", B)] * 4
    assert g.first_regular_file(str(site.src_root / B), g.SOURCE_IGNORE_NAMES)
    assert site.db.guard_baseline("cloud", B) is None
    assert g.g3_mass_delete(0, 0, 100, 10.0, first_run=True).passed

    # the week 29 Mar - 4 Apr: no false "no backup ran" for either FY
    rp = ReportPeriod.previous_week(at(2027, 4, 5, 8))
    with pendulum.travel_to(at(2027, 4, 5, 8), freeze=True):
        m = report.build_report_model(site.db, rp, legs=("lan",), schedule=site.cfg.schedule, config=site.cfg)
    assert m["act"] is False, m["headline"]
    assert "no backup ran" not in m["headline"].lower()


# ===============================================================================================
# T-e (shared replay): activated 1 Jan, 20 nights, staff save files, overlap off on 21 Jan
# ===============================================================================================

STAFF = {at(2027, 1, 3): "a.xlsx", at(2027, 1, 10): "b.xlsx", at(2027, 1, 20): "c.xlsx"}


@pytest.fixture
def jan20(site):
    """1-20 January replayed: FY27-28 activated on the first night, three staff files saved, one PENDING
    approval for FY27-28 and one for FY26-27, 20 nights of rows for both."""
    d = at(2027, 1, 1)
    while d <= at(2027, 1, 20):
        for day, name in STAFF.items():
            if day.date() == d.date():
                (site.src_root / B / name).write_text(f"staff file {name}", encoding="utf-8")
        site.night(d)
        d = d.add(days=1)
    site.early_approval = site.approval(B, when=at(2027, 1, 20, 22, 0))
    site.current_approval = site.approval(A, when=at(2027, 1, 20, 22, 0))
    return site


def test_the_replay_itself_is_what_the_timeline_assumes(jan20):
    """Guards the fixture: 20 nights, both FYs, activation on 1 Jan, 4 files (note + 3) in the early folder."""
    assert jan20.db.get_activation(B)["activated_at"].startswith("2027-01-01")
    assert sorted(set(jan20.calls_on(at(2027, 1, 20)))) == [("cloud", B), ("cloud", A), ("lan", B), ("lan", A)] \
        or sorted(jan20.calls_on(at(2027, 1, 20))) == sorted([("cloud", B), ("cloud", A), ("lan", B), ("lan", A)])
    assert len([c for c in jan20.calls if c.fy == B and c.leg == "lan"]) == 20
    assert jan20.db.guard_baseline("lan", B)["source_file_count"] == 4
    assert len(os.listdir(jan20.src_root / B)) == 4


def _overlap_off(site, when=at(2027, 1, 21, 5, 0)):
    site.cfg.rollover.overlap_enabled = False


# ===============================================================================================
# T-e1  overlap off on 21 Jan
# ===============================================================================================

def test_T_e1_overlap_off_runs_current_fy_only_and_the_sibling_alarm_repeats_daily(jan20, srv):
    _overlap_off(jan20)
    for day in (at(2027, 1, 21), at(2027, 1, 22), at(2027, 1, 23)):
        jan20.rollover(day.add(hours=6))
        jan20.night(day)
        assert jan20.calls_on(day) == [("cloud", A), ("lan", A)], day          # FY26-27 only
    assert jan20.db.get_activation(B) is not None                              # the row is kept
    last_early = jan20.db.get_recent_runs(1, fy=B)[0]["started_at"]
    assert last_early < at(2027, 1, 21).isoformat()                            # nothing for FY27-28 after 20 Jan
    # one sibling alarm per day (21, 22, 23 Jan), each naming the 4 files... the note and 3 staff files
    alarm = f"file(s) were saved in {B}, but they are NOT backed up yet"
    assert _count(srv, alarm) == 3
    text = [t for t in _mails(srv) if alarm in t][-1]
    assert "switched off in the settings" in text and "3 file(s)" in text


def test_T_e1_d9_approving_the_early_fy_request_after_overlap_off_is_refused(jan20, monkeypatch):
    _overlap_off(jan20)
    calls = []

    async def fake_run(name=None, parameters=None, timeout=None):
        calls.append((name, parameters))
        return SimpleNamespace(id="flow-run-1", state=None)

    monkeypatch.setattr(ui, "_cfg", lambda: jan20.cfg)
    monkeypatch.setattr(ui, "get_db", lambda: jan20.db)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "arun_deployment", fake_run)
    with pendulum.travel_to(at(2027, 1, 22, 9, 0), freeze=True):
        ui._sessions[ui_login.SESSION_TOKEN]["created_at"] = time.time()          # the session is "new" on the fake date
        assert ov.active_folders(jan20.cfg, jan20.db, ov.run_date()) == [A]     # the dashboard has no FY27-28 card
        resp = TestClient(ui.app, raise_server_exceptions=False).post(
            f"/approvals/{jan20.early_approval}/approve?wait=false", json={"phrase": "APPROVE"})
    assert resp.status_code == 409, resp.text
    assert calls == []                                                         # nothing was started


# ===============================================================================================
# T-e2  overlap back on 10 Feb
# ===============================================================================================

def test_T_e2_overlap_back_on_resumes_the_same_activation_without_a_second_info_mail(jan20, srv):
    start_mail = f"{B} is now backed up as well"
    assert _count(srv, start_mail) == 1
    _overlap_off(jan20)
    d = at(2027, 1, 21)
    while d <= at(2027, 2, 9):
        jan20.rollover(d.add(hours=6))
        jan20.night(d)
        d = d.add(days=1)
    jan20.cfg.rollover.overlap_enabled = True
    # before anything runs again: the newest FY27-28 run is still 20 Jan and its file count is the G2 baseline
    assert jan20.db.get_recent_runs(1, fy=B)[0]["started_at"].startswith("2027-01-20")
    assert jan20.db.guard_baseline("lan", B)["source_file_count"] == 4          # the 20 Jan figure (note + 3 files)
    (jan20.src_root / B / "d.xlsx").write_text("saved while it was off", encoding="utf-8")    # 5 files now

    resolved = f"{B} is part of the backups now"
    jan20.rollover(at(2027, 2, 10, 6, 0))
    assert ov.active_folders(jan20.cfg, jan20.db, pendulum.date(2027, 2, 10)) == [B, A]
    assert jan20.db.get_activation(B)["activated_at"].startswith("2027-01-01")           # the same row
    assert _count(srv, resolved) == 1
    jan20.night(at(2027, 2, 10))
    assert jan20.calls_on(at(2027, 2, 10)) == [("cloud", B), ("cloud", A), ("lan", B), ("lan", A)]
    for day in (at(2027, 2, 11), at(2027, 2, 12)):
        jan20.rollover(day.add(hours=6))
        jan20.night(day)
    assert _count(srv, start_mail) == 1                                                  # no second activation mail
    assert _count(srv, resolved) == 1                                                    # RESOLVED exactly once
    assert jan20.db.get_activation(B) is not None
    assert jan20.db.guard_baseline("lan", B)["source_file_count"] == 5                   # new baseline from 10 Feb


# ===============================================================================================
# T-e3  report after the switch-off (D10)
# ===============================================================================================

def _week_report(site, sent, legs=("lan",)):
    rp = ReportPeriod.previous_week(sent)
    with pendulum.travel_to(sent, freeze=True):
        return report.build_report_model(site.db, rp, legs=legs, schedule=site.cfg.schedule, config=site.cfg)


def _run_off_period(site, last_day):
    _overlap_off(site)
    for aid in (site.early_approval, site.current_approval):      # handled by the office: no "decision waiting" headline
        assert site.db.dismiss_request(aid, dismissed_by="office", dismissed_at=at(2027, 1, 21, 5, 30).isoformat())
    d = at(2027, 1, 21)
    while d <= last_day:
        site.night(d)
        d = d.add(days=1)


def test_T_e3_week_after_the_switch_off_has_no_early_fy_section_and_no_missing_backup(jan20):
    _run_off_period(jan20, at(2027, 1, 31))
    m = _week_report(jan20, at(2027, 2, 1, 8))                                 # Mon 25 - Sun 31 Jan
    assert [s["fy"] for s in m["sections"]] == [A]
    assert m["act"] is False and "no backup ran" not in m["headline"].lower(), (m["headline"], [s["summaries"] for s in m["sections"]])
    assert B not in report.render_report_html(m, "Firm")


@pytest.mark.xfail(strict=False, reason="DEFECT D10: report.fy_window ends an FY's window only at a config_switch, "
                   "so FY27-28 (left the active set on 21 Jan without a switch) is planned for the whole week "
                   "18-24 Jan: 7 nights scheduled, 3 run. The dashboard shows no FY27-28 card, no overdue.")
def test_T_e3_d10_report_and_dashboard_agree_about_an_fy_that_left_the_active_set(jan20):
    _run_off_period(jan20, at(2027, 1, 24))
    # dashboard side (hand rule): only FY26-27 is active on 25 Jan
    assert ov.active_folders(jan20.cfg, jan20.db, pendulum.date(2027, 1, 25)) == [A]
    m = _week_report(jan20, at(2027, 1, 25, 8))                                # Mon 18 - Sun 24 Jan
    early = [s for s in m["sections"] if s["fy"] == B]
    # whichever rule the owner picks, the report must not plan nights for FY27-28 that no one backs up:
    # the 18, 19, 20 Jan nights ran (3 of 3 planned), 21-24 Jan were never planned
    assert m["act"] is False, m["headline"]
    assert early and early[0]["summaries"][0]["scheduled"] == 3, [s["summaries"][0].get("scheduled") for s in early]


# ===============================================================================================
# T-f1  crash after the config write, before the journal update (R3)
# ===============================================================================================

def test_T_f1_crash_after_config_write_one_folder_that_night_and_everything_closed_next_morning(site, srv):
    site.activate_early()
    old_pending = site.approval(A, when=at(2027, 3, 30, 3, 0))
    old_approved = site.approval(A, leg="cloud", approved=True)
    early_pending = site.approval(B, when=at(2027, 3, 30, 3, 0))

    def write_then_die(config_path, src_root, lan_root, to_fy, expect_fy=None):
        site.point_config_at(to_fy)                                            # config.yaml is written ...
        raise _Crash("power cut")                                              # ... then the process dies

    site.mp.setattr(flow.fy_rollover, "update_config_yaml", write_then_die)
    with pytest.raises(_Crash):
        site.rollover(at(2027, 4, 1, 6, 0))
    st = site.steps()
    assert st[rj.FINAL_BACKUPS] == rj.DONE and st[rj.CONFIG_SWITCH] not in (rj.DONE, rj.SKIPPED)
    assert site.archive_calls == []
    assert site.status_of(old_pending) == ap.PENDING                           # nothing closed yet
    finals_before = [c for c in site.calls if "-final-" in c.run_id]
    assert sorted((c.leg, c.fy) for c in finals_before) == [("cloud", A), ("lan", A)]

    # 1 April 21:01: the config already says FY27-28: ONE folder, FY27-28, and not the closed year
    site.night(at(2027, 4, 1), mode="lan")
    assert site.calls_on(at(2027, 4, 1)) == [("lan", B)]

    # 2 April 06:00: the daily check closes the switch, closes FY26-27 and archives it
    assert site.rollover(at(2027, 4, 2, 6, 0)) == "ROLLOVER_COMPLETED"
    assert all(s in (rj.DONE, rj.SKIPPED) for s in site.steps().values()), site.steps()
    assert [c["old_fy"] for c in site.archive_calls] == [A]
    assert site.status_of(old_pending) == ap.CLOSED and site.status_of(old_approved) == ap.CLOSED
    assert site.status_of(early_pending) == ap.PENDING                         # the new year's request is kept
    assert [c for c in site.calls if "-final-" in c.run_id] == finals_before    # no second final backup


# ===============================================================================================
# T-f2  archive fails on 1 April, retried on 2 and 3 April
# ===============================================================================================

def _gcloud(site, archive_exit):
    argvs = []

    def fake(name, argv, *, timeout_s, artefacts, log_path=None, env=None, cwd=None):
        argvs.append((name, [str(a) for a in argv]))
        out, err = artefacts.new_path(f"{name}.stdout", "txt"), artefacts.new_path(f"{name}.stderr", "txt")
        out.write_text("", encoding="utf-8")
        err.write_text("ERROR: (gcloud.storage.objects.update) denied", encoding="utf-8")
        code = archive_exit() if name == "gcloud-archive" else 0
        return ToolResult(name=name, argv=[str(a) for a in argv], exit_code=code, timed_out=False,
                          spawn_error=None, stdout_path=out, stderr_path=err, log_path=log_path,
                          started_at="2027-04-01T06:00:00+05:30", ended_at="2027-04-01T06:00:01+05:30",
                          duration_s=1.0, timeout_s=timeout_s)

    site.mp.setattr(fr, "run_tool", fake)
    site.mp.setattr(fr, "_resolve_gcloud", lambda: "gcloud")
    site.mp.setattr(flow.fy_rollover, "run_archive_transition", site.real_archive)   # the real one
    return argvs


def test_T_f2_archive_failure_warns_and_retries_only_the_old_prefix(site, srv):
    site.activate_early()
    exits = iter([1, 1, 1])
    argvs = _gcloud(site, lambda: next(exits))
    assert site.rollover(at(2027, 4, 1, 6, 0)) == "ROLLOVER_COMPLETED"        # the archive never blocks the switch
    assert site.steps()[rj.ARCHIVE] == rj.FAILED and site.steps()[rj.CONFIG_SWITCH] == rj.DONE
    assert _count(srv, "Old year not moved to cheaper storage yet") == 1       # WARNING on day 1
    assert "ACTION_REQUIRED" not in [t for t in _mails(srv) if "cheaper storage" in t][0].split("\n")[0]
    for day in (at(2027, 4, 2), at(2027, 4, 3)):
        site.rollover(day.add(hours=6))
        assert ov.active_folders(site.cfg, site.db, day.date()) == [B]         # overlap is inactive after the switch
    archive = [a for n, a in argvs if n == "gcloud-archive"]
    assert len(archive) == 3                                                   # 1, 2, 3 April
    want = ["gcloud", "storage", "objects", "update", f"gs://{BUCKET}/{A}/", "--storage-class=ARCHIVE", "--recursive"]
    assert all(a == want for a in archive), archive
    assert not any(B in " ".join(a) for _, a in argvs)                         # never the new year's prefix
    assert site.db.rollover_get(A, B)["steps"][rj.ARCHIVE]["attempts"] == 3


# ===============================================================================================
# T-f3  close_fy raises once at the switch (D2)
# ===============================================================================================

def _fail_close_fy_once(site, monkeypatch):
    calls = []
    real = ManifestDB.close_fy

    def flaky(self, fy, *, now):
        calls.append(fy)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real(self, fy, now=now)

    monkeypatch.setattr(ManifestDB, "close_fy", flaky)
    return calls


def test_T_f3_control_without_a_failure_the_switch_closes_the_old_fy_approvals(site):
    site.activate_early()
    pending, approved = site.approval(A), site.approval(A, leg="cloud", approved=True)
    assert site.rollover(at(2027, 4, 1, 6, 0)) == "ROLLOVER_COMPLETED"
    assert site.status_of(pending) == ap.CLOSED and site.status_of(approved) == ap.CLOSED


def test_T_f3_d2_a_failed_close_is_repeated_on_the_next_morning(site, monkeypatch):
    site.activate_early()
    pending, approved = site.approval(A), site.approval(A, leg="cloud", approved=True)
    calls = _fail_close_fy_once(site, monkeypatch)
    assert site.rollover(at(2027, 4, 1, 6, 0)) == "ROLLOVER_COMPLETED"
    assert calls == [A]                                                        # the one failing call happened
    assert site.status_of(pending) == ap.PENDING                               # as the defect leaves it on 1 April
    site.rollover(at(2027, 4, 2, 6, 0))
    assert site.status_of(pending) == ap.CLOSED, "FY26-27 approvals still open on 2 April"
    assert site.status_of(approved) == ap.CLOSED


# ===============================================================================================
# P-7  the final backup cannot get the slot on 1 April (D7)
# ===============================================================================================

@pytest.fixture
def busy_site(tmp_path, monkeypatch, srv):
    """1 April 06:00 with the 31 Mar LAN loop still holding the slot: the REAL pipelines run (they take the slot
    themselves, so the slot timeout is produced by production code), only the slot itself is faked."""
    s = Site(tmp_path, monkeypatch, srv, fake_pipelines=False)
    s.activate_early()
    s.slot_busy = True
    yield s
    s.close()
    pendulum.travel_back()


def _rollover_while_busy(site):
    try:
        site.rollover(at(2027, 4, 1, 6, 0))
    except fr.RolloverError:
        pass


def test_P7_control_slot_timeout_stops_the_switch_and_the_next_morning_completes(busy_site):
    site = busy_site
    _rollover_while_busy(site)
    assert site.steps()[rj.CONFIG_SWITCH] == rj.PENDING and site.switch_writes == [] and site.archive_calls == []
    assert site.steps()[rj.FINAL_BACKUPS] != rj.DONE
    rows = [r for r in site.db.get_recent_runs(10, fy=A) if "-final-" in r["run_id"]]
    assert {r["status"] for r in rows} == {"CLOUD_SLOT_TIMEOUT", "LAN_SLOT_TIMEOUT"}
    site.slot_busy = False                                                     # the loop has ended
    site.mp.setattr(flow, "_run_cloud_pipeline", site._pipeline("cloud"))
    site.mp.setattr(flow, "_run_lan_pipeline", site._pipeline("lan"))
    assert site.rollover(at(2027, 4, 2, 6, 0)) == "ROLLOVER_COMPLETED"
    assert site.switch_writes == [(B, A)]


@pytest.mark.xfail(strict=False, reason="DEFECT D7: flow._run_final_backups turns a slot timeout into "
                   "<LEG>_NOT_COMPLETE and G6 then opens an approval request ('approve to roll over anyway') "
                   "for something a retry tomorrow fixes (flow.py:2960-2974, guards.py:290-300).")
def test_P7_d7_final_backup_slot_timeout_is_a_retry_not_an_approval_request(busy_site):
    _rollover_while_busy(busy_site)
    # the proposal: a human is not asked to "approve anyway" a final backup that only waited for the slot
    assert busy_site.db.open_approvals() == [], [a["guards"] for a in busy_site.db.open_approvals()]
