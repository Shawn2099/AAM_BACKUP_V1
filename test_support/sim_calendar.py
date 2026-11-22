"""Calendar simulation rig (plan docs/PLAN_E2E_SIMULATION_2026-11-23.md, card S0).

`Sim` composes the existing, green plumbing and adds no product logic:
  - tests/test_ov_timeline_a.py::Rig (real `_run_cloud_pipeline` / `_run_lan_pipeline`, DEFAULTS stubs,
    `_plain` sync tasks, WoL / shutdown counters, real `smtp_server()`);
  - tests/test_ov_timeline_def.py::Site (config rewrite and archive doubles, `slot_busy`, `point_config_at`);
  - tests/test_ov_timeline_bc.py (audit wiring: artefacts and rclone config doubles).

Faked: rclone / robocopy (one scripted `run_tool` per module: core.cloud_sync, core.lan_sync,
core.integrity), the NAS wake and shutdown, the backup slot, the clock check, the config.yaml write,
the archive step. Everything else is real: flows (called as `.fn`), DB, notify, approvals, journal.

Clock: ONE time_machine traveller is started once per Sim and moved with `move_to(dt, tick=False)`;
`pendulum.travel_to(dt, freeze=True)` is then called after it. pendulum's own traveller is itself a
time_machine traveller, started after ours, so teardown stops it first (`pendulum.travel_back()`) and
then ours. Nothing is entered with `with` inside an event, so a hook may call `jump()` at any time.

Oracles for pass/fail are raw SQL, folder hashes, argv lists, and the SMTP server's own messages.
"""

from __future__ import annotations

import email
import email.header
import hashlib
import json
import re
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pendulum
import time_machine

import flow
import models.config
import ui
from core import fy_overlap as ov
from core.guards import MOUNT_MARKER
from core.health import CLOCK_OK, ClockResult
from core.run_artefacts import RunArtefacts
from core.time_utils import IST
from core.tool_runner import ToolResult
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, RolloverConfig, WolConfig
from test_support import ui_login
from test_support.fake_tools import robocopy_log
from test_support.smtp_server import smtp_server
from tests.test_break_flow import _FLOW_ATTR, DEFAULTS, OK_CLOUD, _NoSleepTime, _plain, _stub

A, B = "FY26-27", "FY27-28"
BUCKET = "my-bucket"
OK = {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES", "LAN_COMPLETE", "LAN_NO_CHANGES"}

SCHEDULE = {
    "cloud_cron": "0 2 * * *",
    "lan_cron": "0 21 * * *",
    "rollover_cron": "0 6 * * *",
    "audit_cron": "0 8 * * SUN",
    "weekly_cron": "0 8 * * MON",
    "monthly_cron": "30 8 1 * *",
}

_MODULE_LEG = {"core.cloud_sync": "cloud", "core.lan_sync": "lan", "core.integrity": "audit"}
_FY_RE = re.compile(r"FY\d\d-\d\d")


class _Crash(BaseException):
    """A power cut: a BaseException, so no step handler records anything (as in test_ov_timeline_def)."""


# ── calendar events ───────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Ev:
    kind: str                    # lan | cloud | rollover | audit | weekly | monthly | status
    at: Any                      # the instant the clock moves to (pendulum DateTime, IST)
    sched: Any                   # the SCHEDULED instant (what flow._scheduled_time reports)


def _day(d) -> pendulum.Date:
    return pendulum.date(d.year, d.month, d.day)


def _at(d, hour: int, minute: int = 0):
    d = _day(d)
    return pendulum.datetime(d.year, d.month, d.day, hour, minute, tz=IST)


def lan(d) -> Ev:
    """LAN backup of run date `d`, scheduled 21:00 IST."""
    t = _at(d, 21)
    return Ev("lan", t, t)


def cloud(d) -> Ev:
    """Cloud backup of run date `d`, scheduled 02:00 IST."""
    t = _at(d, 2)
    return Ev("cloud", t, t)


def rollover(d) -> Ev:
    """Daily rollover check of `d`, scheduled 06:00 IST."""
    t = _at(d, 6)
    return Ev("rollover", t, t)


def audit(d) -> Ev:
    """Weekly integrity audit of `d` (a Sunday), scheduled 08:00 IST."""
    t = _at(d, 8)
    return Ev("audit", t, t)


def weekly(d) -> Ev:
    """Weekly report of `d` (a Monday), scheduled 08:00 IST."""
    t = _at(d, 8)
    return Ev("weekly", t, t)


def monthly(d) -> Ev:
    """Monthly report of `d` (the 1st), scheduled 08:30 IST."""
    t = _at(d, 8, 30)
    return Ev("monthly", t, t)


def status_event(dt) -> Ev:
    """A dashboard read at `dt` (used inside play lists; `Sim.status` reads immediately)."""
    return Ev("status", dt, dt)


def _j(day: int, month: int = 1, year: int = 2027):
    return pendulum.date(year, month, day)


# Section 0 order, Dec 30 21:00 -> Jan 4 08:00 (IST). The daily rollover is included every day.
CAL_J = [
    lan(_j(30, 12, 2026)),
    cloud(_j(31, 12, 2026)), rollover(_j(31, 12, 2026)), lan(_j(31, 12, 2026)),
    cloud(_j(1)), rollover(_j(1)), monthly(_j(1)), lan(_j(1)),
    cloud(_j(2)), rollover(_j(2)), lan(_j(2)),
    cloud(_j(3)), rollover(_j(3)), audit(_j(3)), lan(_j(3)),
    cloud(_j(4)), rollover(_j(4)), weekly(_j(4)),
]

# Section 0 order, Mar 29 08:00 -> Apr 5 08:00 (IST).
CAL_A = [
    weekly(pendulum.date(2027, 3, 29)),
    lan(pendulum.date(2027, 3, 29)),
    cloud(pendulum.date(2027, 3, 30)), rollover(pendulum.date(2027, 3, 30)), lan(pendulum.date(2027, 3, 30)),
    cloud(pendulum.date(2027, 3, 31)), rollover(pendulum.date(2027, 3, 31)), lan(pendulum.date(2027, 3, 31)),
    cloud(pendulum.date(2027, 4, 1)), rollover(pendulum.date(2027, 4, 1)), monthly(pendulum.date(2027, 4, 1)),
    lan(pendulum.date(2027, 4, 1)),
    cloud(pendulum.date(2027, 4, 2)), rollover(pendulum.date(2027, 4, 2)), lan(pendulum.date(2027, 4, 2)),
    cloud(pendulum.date(2027, 4, 3)), rollover(pendulum.date(2027, 4, 3)), lan(pendulum.date(2027, 4, 3)),
    cloud(pendulum.date(2027, 4, 4)), rollover(pendulum.date(2027, 4, 4)), audit(pendulum.date(2027, 4, 4)),
    lan(pendulum.date(2027, 4, 4)),
    cloud(pendulum.date(2027, 4, 5)), rollover(pendulum.date(2027, 4, 5)), weekly(pendulum.date(2027, 4, 5)),
]


# ── default scripted answers (OK logs) ────────────────────────────────────────────────────────

def _default_answer(leg: str) -> dict:
    if leg == "cloud":
        return dict(OK_CLOUD)
    if leg == "lan":
        return {"exit_code": 1, "log": robocopy_log(copied=1), "log_encoding": "utf-16"}
    return {"exit_code": 0, "stderr": "0 differences found\n"}          # audit: a clean rclone check


def _fy_of(argv: list[str]) -> str | None:
    for a in argv:
        m = _FY_RE.search(a)
        if m:
            return m.group(0)
    return None


def _fake_result(name, argv, answer, *, timeout_s, artefacts, log_path):
    """A real ToolResult built from a scripted answer (same fields as test_support.fake_tools)."""
    out = artefacts.new_path(f"{name}.stdout", "txt")
    err = artefacts.new_path(f"{name}.stderr", "txt")
    out.write_text(answer.get("stdout", ""), encoding="utf-8")
    err.write_text(answer.get("stderr", ""), encoding="utf-8")
    timed_out = bool(answer.get("timed_out"))
    spawn_error = answer.get("spawn_error")
    if log_path is not None and not spawn_error:
        log_path.write_text(answer.get("log", ""), encoding=answer.get("log_encoding", "utf-8"))
    return ToolResult(
        name=name, argv=argv,
        exit_code=None if (timed_out or spawn_error) else answer.get("exit_code", 0),
        timed_out=timed_out, spawn_error=spawn_error,
        stdout_path=out, stderr_path=err, log_path=log_path,
        started_at=pendulum.now(IST).isoformat(), ended_at=pendulum.now(IST).isoformat(), duration_s=0.0,
        timeout_s=timeout_s,
    )


# ── the rig ───────────────────────────────────────────────────────────────────────────────────

class Sim:
    """One office on temp folders, one real DB, a real SMTP server, a moved clock, scripted tools."""

    def __init__(self, tmp_path: Path, monkeypatch, srv):
        self.tmp, self.mp, self.srv = tmp_path, monkeypatch, srv
        self.src_root, self.nas_root = tmp_path / "src", tmp_path / "nas"
        (self.src_root / A).mkdir(parents=True)
        (self.src_root / A / "ledger.xlsx").write_text("FY26-27 books", encoding="utf-8")
        (self.nas_root / A).mkdir(parents=True)
        (self.nas_root / A / MOUNT_MARKER).touch()
        (self.nas_root / A / "ledger.xlsx").write_text("FY26-27 books", encoding="utf-8")
        key = tmp_path / "key.json"
        key.write_text('{"type": "service_account"}', encoding="utf-8")
        cfg = AppConfig(
            firm_name="AAM",
            paths=PathsConfig(source_drive=str(self.src_root / A), lan_destination=f"\\\\nas\\share\\{A}",
                              gcs_key_path=str(key), runtime_dir=str(tmp_path / "rt")),
            lan=LanConfig(enabled=True),
            wol=WolConfig(enabled=True, server_ip="192.168.10.10", mac_address="00:11:22:33:44:55"),
            cloud=CloudConfig(enabled=True, bucket=BUCKET, project_number="123"),
            rollover=RolloverConfig(), schedule=dict(SCHEDULE),
            dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
            guards={"forbid_local_destination": False},
            notifications={"smtp_host": "127.0.0.1", "smtp_port": srv.server_address[1],
                           "sender": "b@firm.example", "recipients": ["ops@firm.example"],
                           "smtp_security": "none", "smtp_auth": False, "retry_attempts": 1,
                           "retry_base_seconds": 0.01})
        # the model accepts only a UNC NAS path; the rig needs a real folder it can create folders in
        object.__setattr__(cfg.paths, "lan_destination", str(self.nas_root / A))
        self.cfg = cfg
        self.db_path = Path(cfg.paths.database_path)
        self._smtp_ok_port = srv.server_address[1]
        self._closed_port: int | None = None

        # clock
        self.now = None
        self.sched = None
        self.lateness_s = 0.0
        self._travel = None          # time_machine.travel object (started once)
        self._tm = None              # its Traveller (moved with move_to)
        self.event_seconds: list[float] = []
        self.errors: list[tuple[Ev, BaseException]] = []
        self.status_reads: list[dict] = []

        # the outside world
        self.wakes = 0               # wake calls that found the NAS asleep
        self.wol_calls = 0
        self.wake_ok = True
        self.nas_on = False
        self.shutdowns = 0
        self.slots = 0
        self.slot_busy = False
        self.switch_writes: list[tuple] = []
        self.archive_calls: list[dict] = []
        self.archive_ok = True
        self.calls: list[SimpleNamespace] = []     # every scripted tool call: leg, fy, run_date, argv
        self.script: dict[tuple, dict] = {}        # (leg, fy, run_date_iso) -> answer overrides

        self._install()

    # ── clock ─────────────────────────────────────────────────────────────────────────────────

    def _move_clock(self, dt) -> None:
        if self._travel is None:
            self._travel = time_machine.travel(dt, tick=False)
            self._tm = self._travel.start()
        self._tm.move_to(dt, tick=False)
        pendulum.travel_to(dt, freeze=True)

    def go(self, dt, scheduled=None) -> None:
        """Move the clock to `dt`; `scheduled` is the instant the flow was scheduled for (default `dt`)."""
        self.now = dt
        self.sched = dt if scheduled is None else scheduled
        self._move_clock(dt)

    def jump(self, dt) -> None:
        """Move the clock inside a running event or hook. The scheduled instant does not change."""
        self.now = dt
        self._move_clock(dt)

    def close(self) -> None:
        """Restore the real clock: pendulum's traveller first (it was started after ours), then ours."""
        pendulum.travel_back()
        if self._travel is not None:
            self._travel.stop()
            self._travel = None
            self._tm = None

    # ── wiring (the seams) ────────────────────────────────────────────────────────────────────

    def _install(self) -> None:
        mp, sim = self.mp, self
        mp.setattr(flow, "load_config", lambda p: sim.cfg)
        mp.setattr(flow, "configure_logging", lambda *a, **k: None)
        mp.setattr(flow, "configure_prefect_bridge", lambda *a, **k: None)
        mp.setattr(flow, "cleanup_orphaned_robocopy_logs", lambda **k: None)
        mp.setattr(flow, "log_retention_task", MagicMock())
        mp.setattr(flow, "time", _NoSleepTime())
        mp.setattr(flow, "_scheduled_time", lambda: sim.sched)
        mp.setattr(flow, "_scheduled_lateness_seconds", lambda: sim.lateness_s)
        mp.setattr(flow, "_flow_run_prefix", lambda: f"f{sim.now.format('YYYYMMDDHHmm')}")
        mp.setattr(flow, "check_clock", lambda **k: ClockResult(CLOCK_OK, 1.0, "google", ""))
        mp.setattr(ov, "_default_clock_check", lambda config: ClockResult(CLOCK_OK, 1.0, "google", ""))

        # the NAS
        mp.setattr(flow, "ensure_server_online", self._wake)
        mp.setattr(flow, "lan_shutdown_task", self._shutdown)

        # the backup slot
        @contextmanager
        def slot(config):
            if sim.slot_busy:
                raise TimeoutError("Timed out waiting for the backup slot")
            sim.slots += 1
            yield

        mp.setattr(flow, "_backup_slot", slot)

        # the real Prefect tasks; the DEFAULTS stubs for every step that is not the tool itself
        stubs = {name: _stub(default) for name, default in DEFAULTS.items()}
        for name, attr in _FLOW_ATTR.items():
            if name != "lan_shutdown":
                mp.setattr(flow, attr, stubs[name])
        mp.setattr(flow, "cloud_sync_task", _plain(flow.cloud_sync_task))
        mp.setattr(flow, "lan_sync_task", _plain(flow.lan_sync_task))
        mp.setattr(flow, "wol_check_task", _plain(flow.wol_check_task))

        # audit wiring (test_ov_timeline_bc): no rclone config file on disk
        mp.setattr(flow, "temp_rclone_config", lambda *a, **k: _NullCtx())

        # the 1 April changeover (test_ov_timeline_def.Site)
        mp.setattr(flow.fy_rollover, "update_config_yaml", self._write_config)
        mp.setattr(flow.fy_rollover, "run_archive_transition", self._archive)

        # the scripted tools: one patch per module
        for module, leg in _MODULE_LEG.items():
            mp.setattr(f"{module}.run_tool", self._scripted_tool(leg))

        # the dashboard reads (status) use the same doubles as test_ov_timeline_a's dash fixture
        mp.setattr(ui, "_cfg", lambda: sim.cfg)
        mp.setattr(ui, "_is_running", AsyncMock(return_value=False))
        mp.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
        mp.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
        ui._RATE_LIMITS.clear()

    def _wake(self, config):
        self.wol_calls += 1
        if not self.nas_on:                 # a real wake only when the NAS is asleep
            self.wakes += 1
            self.nas_on = True
        return self.wake_ok

    def _shutdown(self, config, artefacts_dir=None):
        self.shutdowns += 1
        self.nas_on = False
        return {"outcome": "VERIFIED"}

    def _write_config(self, config_path, src_root, lan_root, to_fy, expect_fy=None, **kw):
        """What the compare-and-set write of config.yaml does to the loaded settings."""
        self.switch_writes.append((to_fy, expect_fy))
        self.point_config_at(to_fy)
        return "config.yaml.bak"

    def point_config_at(self, fy: str) -> None:
        object.__setattr__(self.cfg.paths, "source_drive", str(self.src_root / fy))
        object.__setattr__(self.cfg.paths, "lan_destination", str(self.nas_root / fy))

    def _archive(self, **kw):
        self.archive_calls.append(kw)
        return self.archive_ok

    def _scripted_tool(self, leg: str):
        sim = self

        def run_tool(name, argv, *, timeout_s=None, artefacts=None, log_path=None, env=None, cwd=None, **_):
            argv = [str(a) for a in argv]
            fy = _fy_of(argv)
            run_date = sim.sched.date().isoformat()
            sim.calls.append(SimpleNamespace(leg=leg, fy=fy, run_date=run_date, when=sim.now, argv=argv))
            answer = {**_default_answer(leg), **sim.script.get((leg, fy, run_date), {})}
            artefacts = artefacts or RunArtefacts.ephemeral(f"sim-{leg}")
            if leg == "audit" and "--combined" in argv:
                diff = Path(argv[argv.index("--combined") + 1])
                diff.write_text(answer.get("combined", ""), encoding="utf-8")
            hook = answer.get("hook")
            if hook is not None:
                hook()                      # may move the clock with jump(), or raise _Crash
            return _fake_result(name, argv, answer, timeout_s=timeout_s, artefacts=artefacts, log_path=log_path)

        return run_tool

    def set_answer(self, leg: str, fy: str, run_date, **answer) -> None:
        """Script one tool call: (leg, FY, run date). Keys: exit_code, log, log_encoding, stdout, stderr,
        combined, timed_out, spawn_error, hook (callable run before the answer is returned)."""
        key = (leg, fy, run_date.isoformat() if hasattr(run_date, "isoformat") else str(run_date))
        self.script[key] = answer

    # ── events ────────────────────────────────────────────────────────────────────────────────

    def play(self, events) -> Sim:
        """Run the events in order, each at its own instant. A flow failure (Exception) is recorded in
        `self.errors` and the play goes on; a BaseException (a power cut) propagates."""
        for ev in events:
            if self.now is not None and ev.at < self.now:
                raise ValueError(f"events out of order: {ev.kind} at {ev.at} after {self.now}")
            t0 = time.perf_counter()
            try:
                self._run_event(ev)
            except Exception as e:      # noqa: BLE001 - a failed flow is an outcome, recorded for the test
                self.errors.append((ev, e))
            finally:
                self.event_seconds.append(time.perf_counter() - t0)
        return self

    def _run_event(self, ev: Ev) -> None:
        self.go(ev.at, scheduled=ev.sched)
        if ev.kind == "lan":
            flow.backup.fn("config.yaml", "lan")
        elif ev.kind == "cloud":
            flow.backup.fn("config.yaml", "cloud")
        elif ev.kind == "rollover":
            flow.rollover_check_flow.fn(config_path="config.yaml")
        elif ev.kind == "audit":
            flow.integrity_audit_flow.fn("config.yaml", "all")
        elif ev.kind == "weekly":
            flow.weekly_report_flow.fn("config.yaml")
        elif ev.kind == "monthly":
            flow.monthly_report_flow.fn("config.yaml")
        elif ev.kind == "status":
            self.status(ev.at)
        else:
            raise ValueError(f"unknown event {ev.kind}")

    def status(self, dt) -> dict:
        """Move to `dt` and read GET /status (the dashboard JSON) as a signed-in user. product-view."""
        self.go(dt)
        from core.manifest import ManifestDB
        ui_login.register(ui)
        db = ManifestDB.from_config(self.cfg)
        self.mp.setattr(ui, "get_db", lambda: db)
        try:
            from fastapi.testclient import TestClient
            resp = TestClient(ui.app, raise_server_exceptions=False).get("/status", headers=ui_login.HEADERS)
        finally:
            db.close()
        assert resp.status_code == 200, f"/status answered {resp.status_code}: {resp.text[:300]}"
        data = resp.json()
        self.status_reads.append(data)
        return data

    # ── faults ────────────────────────────────────────────────────────────────────────────────

    def smtp_down(self) -> None:
        """Point notifications at a port nothing listens on (connection refused)."""
        if self._closed_port is None:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            self._closed_port = s.getsockname()[1]
            s.close()
        self.cfg.notifications.smtp_port = self._closed_port

    def smtp_up(self) -> None:
        self.cfg.notifications.smtp_port = self._smtp_ok_port

    def power_cut(self) -> None:
        """Copy manifest.db at this instant (a power cut keeps what is on disk), then die."""
        snap = self.tmp / "snap.db"
        src = sqlite3.connect(self.db_path)
        dst = sqlite3.connect(snap)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        raise _Crash("power cut")

    def reboot(self, dt) -> None:
        """Restore the snapshot over manifest.db, move to `dt`, then the agent's start-up sweep.

        A power cut kills the process that owned each RUNNING row. In this one process the owner
        token would still look alive, so the restored rows get the PID of a process that has already
        exited (psutil: provably gone), which is what the reboot did to them."""
        snap = sqlite3.connect(self.tmp / "snap.db")
        dst = sqlite3.connect(self.db_path)
        try:
            snap.backup(dst)
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            token = f"{dead.pid}:0"
            for run_id, metrics in dst.execute(
                    "SELECT run_id, extended_metrics FROM run_history WHERE status LIKE '%_RUNNING'").fetchall():
                record = json.loads(metrics) if metrics else {}
                record["owner"] = token
                dst.execute("UPDATE run_history SET extended_metrics = ? WHERE run_id = ?",
                            (json.dumps(record), run_id))
            dst.commit()
        finally:
            dst.close()
            snap.close()
        self.go(dt)
        from launch import _mark_abandoned_runs
        self.mp.setattr(models.config, "load_config", lambda *a, **k: self.cfg)
        _mark_abandoned_runs()

    def seed_to_mar28(self) -> int:
        """CAL_J, then copy the Jan 3/4 night (lan Jan 3 21:00 x2, cloud Jan 4 02:00 x2) onto every
        night of Jan 5 .. Mar 28 (shift = that day minus Jan 3). Returns the number of rows added."""
        self.play(CAL_J)
        con = sqlite3.connect(self.db_path)
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(run_history)")]
            rows = con.execute(
                "SELECT " + ", ".join(cols) + " FROM run_history WHERE mode IN ('lan','cloud') "
                "AND started_at BETWEEN '2027-01-03T21:00' AND '2027-01-04T02:59' ORDER BY id").fetchall()
            assert len(rows) == 4, f"the Jan 3/4 night has {len(rows)} rows, expected 4"
            added = 0
            day = pendulum.date(2027, 1, 5)
            while day <= pendulum.date(2027, 3, 28):
                shift = (day - pendulum.date(2027, 1, 3)).days
                for row in rows:
                    rec = dict(zip(cols, row, strict=True))
                    del rec["id"]
                    rec["run_id"] = f"{rec['run_id']}-seed-{day.isoformat()}"
                    for key in ("started_at", "ended_at"):
                        if rec.get(key):
                            rec[key] = pendulum.parse(rec[key]).add(days=shift).isoformat()
                    names = ", ".join(rec)
                    marks = ", ".join("?" for _ in rec)
                    con.execute(f"INSERT INTO run_history ({names}) VALUES ({marks})", tuple(rec.values()))
                    added += 1
                day = day.add(days=1)
            con.commit()
            return added
        finally:
            con.close()

    # ── oracles (raw SQL / files / captured argv / the SMTP server; never ManifestDB getters) ──

    def sql(self, query: str, *args) -> list[tuple]:
        con = sqlite3.connect(self.db_path)
        try:
            return [tuple(r) for r in con.execute(query, args).fetchall()]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return []
            raise
        finally:
            con.close()

    def runs(self) -> list[dict]:
        """Every run_history row in id order: id, run_id, mode, fy, status, started_at, ended_at."""
        rows = self.sql("SELECT id, run_id, mode, fy, status, started_at, ended_at FROM run_history ORDER BY id")
        keys = ("id", "run_id", "mode", "fy", "status", "started_at", "ended_at")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def acts(self) -> list[tuple]:
        """overlap_activations: (fy, created_by_app, activated_at) in fy order."""
        return self.sql("SELECT fy, created_by_app, activated_at FROM overlap_activations ORDER BY fy")

    def approvals(self) -> list[tuple]:
        return self.sql("SELECT leg, fy, status FROM approvals ORDER BY id")

    def subjects(self) -> list[str]:
        """The Subject of every mail the SMTP server received, decoded, in arrival order."""
        out = []
        for m in self.srv.messages:
            msg = email.message_from_string(m["data"])
            out.append(str(email.header.make_header(email.header.decode_header(msg.get("Subject", "")))))
        return out

    def argv(self, leg: str) -> list[list[str]]:
        """The argv of every scripted tool call of `leg` ("cloud", "lan" or "audit"), in call order."""
        return [c.argv for c in self.calls if c.leg == leg]

    def tree(self, path) -> dict[str, str]:
        """relative posix path -> sha256 hex of every file under `path` (empty dict if missing)."""
        root = Path(path)
        if not root.exists():
            return {}
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file()}


class _NullCtx:
    def __enter__(self):
        return "rclone.conf"

    def __exit__(self, *a):
        return False


@contextmanager
def sim_world(tmp_path: Path, monkeypatch):
    """A Sim with its SMTP server; the clock is restored and the server stopped on exit."""
    with smtp_server() as srv:
        sim = Sim(tmp_path, monkeypatch, srv)
        try:
            yield sim
        finally:
            sim.close()
