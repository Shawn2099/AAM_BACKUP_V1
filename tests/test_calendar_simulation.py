"""Calendar simulation: the real daily code paths, walked night by night.

time-machine (through pendulum.travel_to) moves the in-process clock, so
pendulum.now / now_iso / get_fy_prefix / time.time see the simulated date.
The scheduler is simulated with cronsim over the REAL schedule.* crons of
the config, so the jobs fire in the order they would in production.

Real: config.yaml on disk (load + atomic rewrite), FY maths, rollover
decision and folder creation, config backup, run_history in SQLite, the
attention/stale evaluation, run-log day folders and retention.
Faked (the outside world): the cloud/LAN pipelines (they record a run row
for the FY they were configured with), the Google/NTP clock check (a
"world clock"), the Prefect backup slot, email, the rclone archive step.

time.monotonic and child processes keep real time - the audit lab covers
the real tools; this file covers the calendar.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pendulum
import pytest
import yaml
from cronsim import CronSim

import flow
from core import fy_rollover as fr
from core import statuses
from core.attention import compute_attention
from core.health import CLOCK_OK, CLOCK_SKEWED, ClockResult
from core.manifest import ManifestDB
from core.run_artefacts import RunArtefacts
from core.time_utils import IST
from models.config import load_config
from test_support.sandbox_config import _config_dict

pytest.importorskip("time_machine")   # pendulum.travel_to needs it


def _at(y, m, d, h=0, mi=0, s=0):
    return pendulum.datetime(y, m, d, h, mi, s, tz=IST)


def _unc(p: Path) -> str:
    return str(p).replace("C:\\", "\\\\127.0.0.1\\C$\\", 1)


@pytest.fixture()
def site(tmp_path):
    """A real config.yaml for FY26-27; the NAS is a folder on the loopback share."""
    if tmp_path.drive.upper() != "C:":
        pytest.skip("loopback admin-share mapping only implemented for C:")
    d = _config_dict(tmp_path)
    nas = tmp_path / "nas" / "FY26-27"
    nas.mkdir(parents=True)
    try:
        if not Path(_unc(nas)).is_dir():
            pytest.skip(f"loopback UNC not reachable: {_unc(nas)}")
    except OSError as e:
        pytest.skip(f"loopback UNC unreachable: {e}")
    d["paths"]["lan_destination"] = _unc(nas)
    (Path(d["paths"]["source_drive"]) / "client_ledger.xlsx").write_bytes(b"FY26-27 books")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(d), encoding="utf-8")
    return cfg


class WorldClock:
    """Fake check_clock: compares the local (simulated) clock with true time.
    true_now=None means the local clock is right."""

    def __init__(self):
        self.true_now = None
        self.calls = 0

    def __call__(self, *, max_skew_seconds, **_):
        self.calls += 1
        if self.true_now is None:
            return ClockResult(CLOCK_OK, 0.0, "google", "")
        skew = abs((pendulum.now(IST) - self.true_now).total_seconds())
        if skew > max_skew_seconds:
            return ClockResult(CLOCK_SKEWED, skew, "google",
                               f"System clock skew detected: {skew:.0f}s difference from google")
        return ClockResult(CLOCK_OK, skew, "google", "")


class Calendar:
    """Fires the configured cron jobs in time order, each at its own instant."""

    JOBS = ("rollover", "cloud", "lan")

    def __init__(self, cfg_path: Path):
        self.cfg_path = str(cfg_path)
        self.clock = WorldClock()
        self.leg_status = {"cloud": "CLOUD_COMPLETE", "lan": "LAN_COMPLETE"}
        self.backups: list[dict] = []     # every pipeline call (nightly + final)
        self.log: list[tuple] = []        # (when, job, result)
        self.notify = MagicMock()
        self.archive = MagicMock(return_value=True)
        self._patches = [
            patch("flow.check_clock", side_effect=self.clock),
            patch("flow._run_cloud_pipeline", side_effect=self._pipeline("cloud")),
            patch("flow._run_lan_pipeline", side_effect=self._pipeline("lan")),
            patch("flow._backup_slot"),
            patch("flow.notify", self.notify),
            patch("flow.fy_rollover.run_archive_transition", self.archive),
            patch("flow.log_retention_task", MagicMock(side_effect=flow.log_retention_task.fn)),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()

    def _pipeline(self, leg):
        def run(config, run_id, started_at, monotonic_start=None, approval_id=None):
            status = self.leg_status[leg]
            self.backups.append({"when": pendulum.now(IST), "leg": leg, "run_id": run_id,
                                 "source": config.paths.source_drive,
                                 "lan": config.paths.lan_destination, "status": status})
            flow._record_run(config, run_id, leg, started_at, status,
                             0 if status in statuses.SUCCESS_STATUSES else 1, None)
            if status not in statuses.SUCCESS_STATUSES:
                raise RuntimeError(f"{leg} ended {status}")
            return {"status": status}
        return run

    def events(self, start, end, off=None):
        sched = load_config(self.cfg_path).schedule
        out = []
        for job in self.JOBS:
            for when in CronSim(getattr(sched, f"{job}_cron"), start.in_timezone(sched.timezone)):
                when = pendulum.instance(when)
                if when > end:
                    break
                if off and off[0] <= when < off[1]:
                    continue                              # server switched off
                out.append((when, job))
        return sorted(out)

    def run(self, start, end, off=None, after_each=None):
        for when, job in self.events(start, end, off):
            with pendulum.travel_to(when, freeze=True):
                self.log.append((when, job, self.fire(job)))
                if after_each:
                    after_each(when, job)

    def fire(self, job):
        if job == "rollover":
            try:
                return flow.rollover_check_flow.fn(config_path=self.cfg_path)
            except fr.RolloverError:
                return "BLOCKED"
        config = load_config(self.cfg_path)
        pipeline = flow._run_cloud_pipeline if job == "cloud" else flow._run_lan_pipeline
        try:
            return pipeline(config, f"night-{job}-{pendulum.now(IST).isoformat()}",
                            flow.now_iso(), time.monotonic())["status"]
        except RuntimeError:
            return "FAILED"

    def rows(self, mode=None):
        con = sqlite3.connect(load_config(self.cfg_path).paths.database_path)
        try:
            q = "SELECT mode, status, started_at FROM run_history"
            q += " WHERE mode = ?" if mode else ""
            return con.execute(q + " ORDER BY started_at", (mode,) if mode else ()).fetchall()
        finally:
            con.close()


def _fy(path: str) -> str:
    return fr._fy_name(path)


# ── FY rollover across April 1 ───────────────────────────────────────────

def test_walk_across_april_1_rolls_over_exactly_once_and_switches_every_backup(site):
    with Calendar(site) as cal:
        cal.run(_at(2027, 3, 29), _at(2027, 4, 4))

        rollovers = [(w, r) for w, j, r in cal.log if j == "rollover"]
        done = [w for w, r in rollovers if r == "ROLLOVER_COMPLETED"]
        assert len(done) == 1 and done[0].date() == pendulum.date(2027, 4, 1)
        assert all(r == "NO_ROLLOVER_NEEDED" for w, r in rollovers if w != done[0])
        assert [s for _, s, _ in cal.rows("rollover")] == ["ROLLOVER_COMPLETE"]

        # the final backup of the closing FY ran on both legs, against FY26-27
        final = [b for b in cal.backups if "-final-" in b["run_id"]]
        assert sorted(b["leg"] for b in final) == ["cloud", "lan"]
        assert {(_fy(b["source"]), _fy(b["lan"])) for b in final} == {("FY26-27", "FY26-27")}

        # every nightly backup used the FY the calendar says, never a mix
        for b in cal.backups:
            if "-final-" in b["run_id"]:
                continue
            want = "FY26-27" if b["when"] < done[0] else "FY27-28"
            assert (_fy(b["source"]), _fy(b["lan"])) == (want, want), b
        assert any(b["when"] > done[0] for b in cal.backups)   # the new FY was used

        # on disk: new FY folders, config switched + backed up, old FY untouched
        cfg = load_config(str(site))
        assert _fy(cfg.paths.source_drive) == _fy(cfg.paths.lan_destination) == "FY27-28"
        src_root = Path(cfg.paths.source_drive).parent
        assert (src_root / "FY27-28" / cfg.rollover.source_placeholder_name).is_file()
        assert (src_root / "FY26-27" / "client_ledger.xlsx").read_bytes() == b"FY26-27 books"
        assert (site.parent / "nas" / "FY27-28" / ".AAM_TARGET_MOUNTED").is_file()
        assert len(list(site.parent.glob("config.yaml.bak-*"))) == 1
        cal.archive.assert_called_once()
        assert cal.archive.call_args.kwargs["old_fy"] == "FY26-27"
        assert cal.clock.calls == 1        # Google/NTP asked only on the rollover day


@pytest.mark.parametrize("when,expected", [
    (_at(2027, 3, 31, 23, 59, 59), "NO_ROLLOVER_NEEDED"),
    (_at(2027, 4, 1, 0, 0, 5), "ROLLOVER_COMPLETED"),    # UTC is still March 31
])
def test_rollover_check_uses_the_ist_date_at_midnight(site, when, expected):
    # The default 06:00 IST check is the same date in UTC; a rollover_cron
    # between 00:00 and 05:30 IST is not, so the FY must come from the IST date.
    with Calendar(site) as cal, pendulum.travel_to(when, freeze=True):
        assert cal.fire("rollover") == expected


def test_server_off_over_april_1_rolls_over_at_the_first_check_after_boot(site):
    with Calendar(site) as cal:
        cal.run(_at(2027, 3, 28), _at(2027, 4, 8), off=(_at(2027, 3, 30, 12), _at(2027, 4, 5)))

        done = [w for w, j, r in cal.log if r == "ROLLOVER_COMPLETED"]
        first_check = min(w for w, j, _ in cal.log if j == "rollover" and w >= _at(2027, 4, 5))
        assert done == [first_check]
        final = [b for b in cal.backups if "-final-" in b["run_id"]]
        assert {_fy(b["source"]) for b in final} == {"FY26-27"}
        assert _fy(load_config(str(site)).paths.source_drive) == "FY27-28"


# ── The clock-proof rule (WP11/WP12) on a real calendar ──────────────────

def test_local_clock_wrongly_in_april_never_switches_the_fy(site):
    before = site.read_bytes()
    with Calendar(site) as cal:
        cal.clock.true_now = _at(2026, 10, 16, 9)        # real world: October
        cal.run(_at(2027, 4, 1), _at(2027, 4, 3, 23))    # local clock: April

        rollovers = [r for _, j, r in cal.log if j == "rollover"]
        assert rollovers and set(rollovers) == {"BLOCKED"}      # refused every morning
        assert [s for _, s, _ in cal.rows("rollover")] == ["ROLLOVER_BLOCKED"] * len(rollovers)
        assert not [b for b in cal.backups if "-final-" in b["run_id"]]
        assert {_fy(b["source"]) for b in cal.backups} == {"FY26-27"}  # backups keep going
        event = cal.notify.call_args_list[-1].args[1]
        assert event.kind == "ACTION_REQUIRED" and event.status == "ROLLOVER_BLOCKED"
    assert site.read_bytes() == before                           # config untouched
    assert not (site.parent / "source" / "FY27-28").exists()
    assert not (site.parent / "nas" / "FY27-28").exists()
    assert not list(site.parent.glob("config.yaml.bak-*"))


def test_clock_falling_back_after_the_rollover_never_rolls_back(site):
    with Calendar(site) as cal:
        cal.run(_at(2027, 3, 31), _at(2027, 4, 2))
        assert _fy(load_config(str(site)).paths.source_drive) == "FY27-28"
        switched = site.read_bytes()

        cal.run(_at(2027, 3, 20), _at(2027, 3, 21, 23))   # e.g. a dead CMOS battery
        assert {r for w, j, r in cal.log if j == "rollover" and w.month == 3
                and w.day in (20, 21)} == {"BLOCKED"}
        assert site.read_bytes() == switched
        assert {_fy(b["source"]) for b in cal.backups if b["when"].day in (20, 21)} \
            == {"FY27-28"}


# ── Stale ("backup overdue") evaluation across nights ────────────────────

def test_overdue_is_raised_exactly_when_the_last_success_is_older_than_the_limit(site):
    seen = []

    with Calendar(site) as cal:
        cfg = load_config(str(site))
        limit_h = cfg.dashboard.stale_after_hours

        def check(when, job):
            db = ManifestDB.from_config(cfg)
            try:
                codes = {i["code"] for i in compute_attention(db, cfg)}
            finally:
                db.close()
            seen.append((when, "stale:cloud" in codes))

        cal.run(_at(2026, 6, 1), _at(2026, 6, 3, 23), after_each=check)
        assert not any(stale for w, stale in seen if w > _at(2026, 6, 1, 23))
        last_ok = max(b["when"] for b in cal.backups
                      if b["leg"] == "cloud" and b["status"] == "CLOUD_COMPLETE")

        cal.leg_status["cloud"] = "CLOUD_FAILED"            # every cloud night now fails
        cal.run(_at(2026, 6, 4), _at(2026, 6, 9), after_each=check)

        deadline = last_ok.add(hours=limit_h)
        for when, stale in seen:
            if when > _at(2026, 6, 1, 23):
                assert stale == (when > deadline), (when, deadline)
        db = ManifestDB.from_config(cfg)
        try:
            for offset, expected in ((-60, False), (60, True)):
                with pendulum.travel_to(deadline.add(seconds=offset), freeze=True):
                    codes = {i["code"] for i in compute_attention(db, cfg)}
                assert ("stale:cloud" in codes) is expected, offset
        finally:
            db.close()


# ── Run-log day folders and retention over a whole retention window ─────

def test_run_logs_are_filed_under_the_ist_day_and_kept_exactly_retention_days(site):
    cfg = load_config(str(site))
    keep = cfg.run_logs.retention_days
    nights = [_at(2026, 7, 1, 1, 30).add(days=i) for i in range(keep + 15)]  # 01:30 IST = UTC day before
    for when in nights:
        with pendulum.travel_to(when, freeze=True):
            art = RunArtefacts.for_config(cfg, f"run-{when.format('YYYYMMDD')}")
            assert art.dir.parent.name == when.format("YYYY-MM-DD")   # IST day, not UTC
            os.utime(art.dir.parent, (time.time(), time.time()))      # disk mtime = sim time
            flow.log_retention_task.fn(cfg)

    kept = sorted(p.name for p in Path(cfg.run_logs_dir).iterdir())
    assert kept == [w.format("YYYY-MM-DD") for w in nights[-(keep + 1):]]
