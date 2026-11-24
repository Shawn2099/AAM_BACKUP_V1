"""FY overlap on a calendar (plan B5): the real daily code paths, night by night, with two FYs active.

Same machinery as tests/test_calendar_simulation.py (WorldClock, cronsim over the real schedule.* crons, the real
config.yaml on disk, real SQLite, real rollover decision and folder creation). The difference: the scheduled cloud
and LAN jobs go through the REAL `flow.backup()` flow, so the FY pass loop (plan 2.2) runs; the pipelines behind it
are fakes that record the FY of every pass they are handed (`fy_pass`) and write a run row for it.

Faked (the outside world): the pipelines, the Google/NTP clock (also in core.fy_overlap), email, the archive step, the
backup slot. Not covered here: the real tools (audit lab, D2) and the drive-root layout `E:\\FY26-27` (the windows
below use sibling folders under a temp root; the root case is covered by the A2 unit tests and the B2 pipeline tests).

An unscheduled leg (`scheduled: false`) has no cron job in this calendar: that is the plan 5 / C1 rule, modelled here
by leaving its job out of the simulated schedule.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from unittest.mock import patch

import pendulum
import pytest
import yaml
from cronsim import CronSim
from test_calendar_simulation import Calendar, _at, _unc

import flow
from core import fy_overlap, overdue, statuses
from core import fy_rollover as fr
from core.attention import compute_attention
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import load_config
from test_support.sandbox_config import _config_dict

pytestmark = pytest.mark.slow

import time_machine  # noqa: E402,F401  hard import: a missing package must fail loudly, never skip the file (pendulum.travel_to needs it)


# ── the simulation ────────────────────────────────────────────────────────

class OverlapCalendar(Calendar):
    """Calendar whose cloud/LAN jobs call the real backup() flow."""

    def __init__(self, cfg_path: Path):
        self.via = "setup"
        super().__init__(cfg_path)
        self._patches.append(patch("core.fy_overlap.check_clock", side_effect=self.clock))
        self._patches.append(patch("core.fy_overlap.notify", self.notify))

    def _pipeline(self, leg):
        def run(config, run_id, started_at, monotonic_start=None, approval_id=None, **kwargs):
            fy_pass = kwargs.get("fy_pass")
            fy = fy_pass.fy if fy_pass is not None else fr._fy_name(config.paths.source_drive)
            extra = fy_pass is not None and fy_pass.extra
            rid = fy_overlap.pass_run_id(run_id, fy) if extra else run_id
            status = self.leg_status[leg]
            ok = status in statuses.SUCCESS_STATUSES
            self.backups.append({"when": pendulum.now(IST), "leg": leg, "fy": fy, "via": self.via,
                                 "run_id": rid, "status": status, "source": config.paths.source_drive,
                                 "lan": config.paths.lan_destination})
            flow._record_run(config, rid, leg, started_at, status, 0 if ok else 1, None,
                             extended_metrics=json.dumps({"fy": fy, "guards": {"fy": fy}}))
            if not ok:
                raise RuntimeError(f"{leg} ended {status}")
            return {"status": status}
        return run

    def events(self, start, end, off=None):
        cfg = load_config(self.cfg_path)
        sched = cfg.schedule
        out = []
        for job in self.JOBS:
            if job in ("cloud", "lan") and not getattr(cfg, job).scheduled:
                continue                                   # plan 5 / C1: no cron job for a manual-only leg
            for when in CronSim(getattr(sched, f"{job}_cron"), start.in_timezone(sched.timezone)):
                when = pendulum.instance(when)
                if when > end:
                    break
                if off and off[0] <= when < off[1]:
                    continue                               # server switched off
                out.append((when, job))
        return sorted(out)

    def fire(self, job):
        if job == "rollover":
            self.via = "rollover"
            return super().fire(job)
        self.via = job
        try:
            flow.backup.fn(config_path=self.cfg_path, mode=job)
            return "OK"
        except Exception:   # noqa: BLE001 - backup() raises an ExceptionGroup when a pass failed
            return "FAILED"

    def manual(self, leg: str, when):
        """A person presses 'Run backup' on the leg's card at `when` (no approval)."""
        with pendulum.travel_to(when, freeze=True):
            self.via = f"manual-{leg}"
            with contextlib.suppress(Exception):   # a failed pass ends the Prefect run FAILED: the calendar goes on
                flow.backup.fn(config_path=self.cfg_path, mode=leg)


def _make_site(tmp_path: Path, fy: str, *, lan_scheduled: bool = True) -> Path:
    """A real config.yaml whose configured FY is `fy`; the NAS is a folder on the loopback admin share."""
    if tmp_path.drive.upper() != "C:":
        pytest.skip("loopback admin-share mapping only implemented for C:")
    d = _config_dict(tmp_path)
    src = tmp_path / "source" / fy
    src.mkdir(parents=True, exist_ok=True)
    (src / "client_ledger.xlsx").write_bytes(f"{fy} books".encode())
    nas = tmp_path / "nas" / fy
    nas.mkdir(parents=True, exist_ok=True)
    try:
        if not Path(_unc(nas)).is_dir():
            pytest.skip(f"loopback UNC not reachable: {_unc(nas)}")
    except OSError as e:
        pytest.skip(f"loopback UNC unreachable: {e}")
    d["paths"]["source_drive"] = str(src)
    d["paths"]["lan_destination"] = _unc(nas)
    if not lan_scheduled:
        d["lan"]["scheduled"] = False
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(d), encoding="utf-8")
    return cfg


def _start_year(fy: str) -> int:
    return 2000 + int(fy[2:4])


def _record_of(cfg_path: Path, fy: str):
    db = ManifestDB.from_config(load_config(str(cfg_path)))
    try:
        return db.overlap_record(fy)
    finally:
        db.close()


def _passes_on(cal: OverlapCalendar, via: str, day: str) -> list[str]:
    """The FYs of every pass of a scheduled leg run on `day` (YYYY-MM-DD), in the order they ran."""
    return [b["fy"] for b in cal.backups if b["via"] == via and b["when"].format("YYYY-MM-DD") == day]


def _expected(cur: str, nxt: str, act, sw):
    """The FY passes a scheduled run must make at `when` (activation at `act`, switch at `sw`)."""
    def expect(when) -> list[str]:
        if when < act:
            return [cur]
        if when < sw:
            return [cur, nxt]
        return [nxt]
    return expect


def _check_every_run(cal: OverlapCalendar, expect) -> int:
    """Every scheduled cloud/LAN run of the calendar made exactly the expected passes, in order."""
    got: dict[tuple[int, str], list[str]] = {}
    for b in cal.backups:
        if b["via"] in ("cloud", "lan"):
            got.setdefault((int(b["when"].timestamp()), b["via"]), []).append(b["fy"])
    runs = 0
    for when, job, _ in cal.log:
        if job == "rollover":
            continue
        want = expect(when)
        assert got.get((int(when.timestamp()), job), []) == want, f"{job} at {when}"
        runs += 1
    return runs


def _codes(cfg_path: Path, when) -> set[str]:
    cfg = load_config(str(cfg_path))
    db = ManifestDB.from_config(cfg)
    try:
        with pendulum.travel_to(when, freeze=True):
            return {i["code"] for i in compute_attention(db, cfg)}
    finally:
        db.close()


def _mails(cal: OverlapCalendar):
    return [c.args[1] for c in cal.notify.call_args_list]


def _deadline(cfg_path: Path, started):
    cfg = load_config(str(cfg_path))
    return pendulum.instance(overdue.deadline(
        cfg.schedule.cloud_cron, cfg.schedule.timezone, started,
        cfg.budget.cloud_max_hours, cfg.dashboard.stale_grace_hours))


# ── one pass per leg, then two, then one ────────────────────────────────

def test_one_pass_per_leg_until_31_dec_then_two_then_one_after_the_switch(tmp_path):
    cfg = _make_site(tmp_path, "FY26-27")
    records = []
    with OverlapCalendar(cfg) as cal:
        cal.run(_at(2026, 12, 30), _at(2027, 4, 3, 23, 59),
                after_each=lambda when, job: records.append((when, job, _record_of(cfg, "FY27-28") is not None)))

        # the activation record first exists after the 31 Dec 06:00 check, and nowhere earlier
        first = next(i for i, (w, j, has) in enumerate(records) if has)
        assert records[first][:2] == (_at(2026, 12, 31, 6), "rollover")
        assert not any(has for w, j, has in records[:first])

        # the day table: passes per leg per day
        assert _passes_on(cal, "cloud", "2026-12-30") == ["FY26-27"]
        assert _passes_on(cal, "lan", "2026-12-31") == ["FY26-27"]     # 01:00, before the 06:00 activation
        assert _passes_on(cal, "cloud", "2026-12-31") == ["FY26-27", "FY27-28"]
        assert _passes_on(cal, "lan", "2027-01-01") == ["FY26-27", "FY27-28"]
        assert _passes_on(cal, "cloud", "2027-03-31") == ["FY26-27", "FY27-28"]
        assert _passes_on(cal, "lan", "2027-04-01") == ["FY26-27", "FY27-28"]   # 01:00, before the 06:00 switch
        assert _passes_on(cal, "cloud", "2027-04-01") == ["FY27-28"]          # 18:00, after the switch
        assert _passes_on(cal, "lan", "2027-04-02") == ["FY27-28"]
        assert _passes_on(cal, "cloud", "2027-04-03") == ["FY27-28"]

        # every scheduled run in the window, not only the sampled days: 95 days x (cloud 18:00 + LAN 01:00)
        assert _check_every_run(cal, _expected("FY26-27", "FY27-28", _at(2026, 12, 31, 6), _at(2027, 4, 1, 6))) == 190


def test_every_run_gets_the_passes_the_calendar_says_across_three_cycles(tmp_path):
    """FY26-27 -> FY27-28 (2027), FY27-28 -> FY28-29 (contains 29 Feb 2028), FY28-29 -> FY29-30 (2028-29)."""
    windows = [
        ("FY26-27", _at(2026, 12, 30), _at(2027, 4, 3, 23, 59)),
        ("FY27-28", _at(2027, 12, 30), _at(2028, 4, 3, 23, 59)),
        ("FY28-29", _at(2028, 12, 30), _at(2029, 4, 3, 23, 59)),
    ]
    for cur, start, end in windows:
        root = tmp_path / cur
        root.mkdir()
        cfg = _make_site(root, cur)
        nxt = fy_overlap.next_fy(cur)
        act = _at(_start_year(cur), 12, 31, 6)
        sw = _at(_start_year(cur) + 1, 4, 1, 6)
        with OverlapCalendar(cfg) as cal:
            records = []

            def note_activation(when, job, cfg=cfg, nxt=nxt, records=records):
                records.append((when, job, _record_of(cfg, nxt) is not None))
            cal.run(start, end, after_each=note_activation)

            first = next(w for w, j, has in records if has)
            assert first == act, (cur, first)
            rollovers = {w: r for w, j, r in cal.log if j == "rollover"}
            assert [w for w, r in rollovers.items() if r == "ROLLOVER_COMPLETED"] == [sw], cur
            assert {r for w, r in rollovers.items() if w != sw} == {"NO_ROLLOVER_NEEDED"}, cur
            days = (end - start).days + 1
            assert _check_every_run(cal, _expected(cur, nxt, act, sw)) == 2 * days, cur   # cloud + LAN per day
            assert fr._fy_name(load_config(str(cfg)).paths.source_drive) == nxt
            cal.archive.assert_called_once()
            assert cal.archive.call_args.kwargs["old_fy"] == cur

            if cur == "FY27-28":   # 29 Feb 2028 sits inside the FY27-28 -> FY28-29 overlap
                assert _passes_on(cal, "cloud", "2028-02-29") == ["FY27-28", "FY28-29"]
                assert _passes_on(cal, "lan", "2028-02-29") == ["FY27-28", "FY28-29"]


# ── activation edge cases on the calendar ────────────────────────────────

def test_clock_a_day_behind_on_31_dec_waits_then_activates_on_1_jan(tmp_path):
    cfg = _make_site(tmp_path, "FY26-27")
    with OverlapCalendar(cfg) as cal:
        cal.clock.true_now = _at(2027, 1, 1, 6)           # the local clock says 31 Dec: a day behind
        cal.run(_at(2026, 12, 31), _at(2026, 12, 31, 23, 59))
        assert _record_of(cfg, "FY27-28") is None
        assert not (tmp_path / "source" / "FY27-28").exists()
        waiting = [m for m in _mails(cal) if m.key == "rollover:activation:FY27-28"]
        assert waiting and waiting[0].kind == "WARNING"            # the activation day
        assert _passes_on(cal, "cloud", "2026-12-31") == ["FY26-27"]

        cal.clock.true_now = None                          # the clock is right again
        cal.run(_at(2027, 1, 1), _at(2027, 1, 1, 23, 59))
        assert _record_of(cfg, "FY27-28") is not None
        assert (tmp_path / "source" / "FY27-28").is_dir()
        assert _passes_on(cal, "lan", "2027-01-01") == ["FY26-27"]          # 01:00, before the 06:00 check
        assert _passes_on(cal, "cloud", "2027-01-01") == ["FY26-27", "FY27-28"]
        assert [m.key for m in _mails(cal) if m.key.startswith("rollover:activat")] \
            == ["rollover:activation:FY27-28", "rollover:activated:FY27-28"]


def test_server_off_all_of_31_dec_activates_at_the_first_check_on_1_jan(tmp_path):
    cfg = _make_site(tmp_path, "FY26-27")
    records = []
    with OverlapCalendar(cfg) as cal:
        cal.run(_at(2026, 12, 30), _at(2027, 1, 1, 23, 59), off=(_at(2026, 12, 31), _at(2027, 1, 1)),
                after_each=lambda w, j: records.append((w, j, _record_of(cfg, "FY27-28") is not None)))
        assert not [w for w, j, _ in cal.log if _at(2026, 12, 31) <= w < _at(2027, 1, 1)]
        first = next(i for i, (w, j, has) in enumerate(records) if has)
        assert records[first][:2] == (_at(2027, 1, 1, 6), "rollover")
        assert _passes_on(cal, "lan", "2027-01-01") == ["FY26-27"]
        assert _passes_on(cal, "cloud", "2027-01-01") == ["FY26-27", "FY27-28"]


# ── the April switch: a blocked final copy, a manual copy, the switch ────

def test_failed_lan_final_blocks_the_switch_until_a_manual_copy_then_switches_on_3_apr(tmp_path):
    cfg = _make_site(tmp_path, "FY26-27")
    with OverlapCalendar(cfg) as cal:
        cal.run(_at(2026, 12, 30), _at(2027, 3, 31, 23, 59))
        cal.leg_status["lan"] = "LAN_FAILED"                       # the NAS is not reachable from 1 Apr
        cal.run(_at(2027, 4, 1), _at(2027, 4, 2, 13, 59))
        cal.leg_status["lan"] = "LAN_COMPLETE"                     # 2 Apr 14:00: someone runs the NAS card
        cal.manual("lan", _at(2027, 4, 2, 14))
        assert [b["fy"] for b in cal.backups if b["via"] == "manual-lan"] == ["FY26-27", "FY27-28"]
        cal.leg_status["lan"] = "LAN_FAILED"                       # the next nights fail again
        cal.run(_at(2027, 4, 2, 14, 1), _at(2027, 4, 3, 23, 59))

        rollovers = [(w, r) for w, j, r in cal.log if j == "rollover" and r != "NO_ROLLOVER_NEEDED"]
        assert [(w, r) for w, r in rollovers] == [
            (_at(2027, 4, 1, 6), "BLOCKED"), (_at(2027, 4, 2, 6), "BLOCKED"),
            (_at(2027, 4, 3, 6), "ROLLOVER_COMPLETED")]
        # the switch's own final copy failed, so the 2 Apr manual copy is what counted
        assert [b["status"] for b in cal.backups
                if b["via"] == "rollover" and b["leg"] == "lan" and b["when"].format("YYYY-MM-DD") == "2027-04-03"] \
            == ["LAN_FAILED"]
        assert fr._fy_name(load_config(str(cfg)).paths.source_drive) == "FY27-28"   # switched, no approval
        assert _check_every_run(cal, _expected("FY26-27", "FY27-28", _at(2026, 12, 31, 6), _at(2027, 4, 3, 6))) > 0
        assert [s for _, s, _ in cal.rows("rollover")] == \
            ["ROLLOVER_BLOCKED", "ROLLOVER_BLOCKED", "ROLLOVER_COMPLETE"]


# ── a manual-only NAS leg (plan 5, C1/C2) ────────────────────────────────

def test_unscheduled_lan_leg_is_never_run_by_the_check_and_blocks_the_switch_until_a_manual_copy(tmp_path):
    cfg = _make_site(tmp_path, "FY26-27", lan_scheduled=False)
    with OverlapCalendar(cfg) as cal:
        cal.run(_at(2026, 12, 30), _at(2027, 4, 3, 23, 59))
        assert not [b for b in cal.backups if b["via"] == "lan"]             # no scheduled LAN pass at all
        assert [r for w, j, r in cal.log if j == "rollover" and r != "NO_ROLLOVER_NEEDED"] == ["BLOCKED"] * 3
        assert fr._fy_name(load_config(str(cfg)).paths.source_drive) == "FY26-27"   # still waiting


def test_unscheduled_lan_leg_is_never_overdue_in_the_whole_window(tmp_path):
    cfg = _make_site(tmp_path, "FY26-27", lan_scheduled=False)
    lan_items: list[tuple[str, str]] = []
    with OverlapCalendar(cfg) as cal:
        def check(when, job):
            for code in _codes(cfg, when):
                if re.search(r"(^|:)lan(:|$)", code):
                    lan_items.append((when.format("YYYY-MM-DD"), code))
        cal.run(_at(2026, 12, 30), _at(2027, 4, 3, 23, 59), after_each=check)
    assert lan_items == [], f"manual-only LAN leg raised items: {lan_items[:3]}"


# ── overdue of the next FY (plan 2.5, task B7) ───────────────────────────

def _cloud_failing_site(tmp_path):
    cfg = _make_site(tmp_path, "FY26-27")
    with OverlapCalendar(cfg) as cal:
        cal.leg_status["cloud"] = "CLOUD_FAILED"                     # no cloud night succeeds
        cal.run(_at(2026, 12, 30), _at(2026, 12, 31, 23, 59))
    return cfg


def test_next_fy_gets_no_overdue_item_before_its_deadline(tmp_path):
    # B7 (plan 2.5, Q1): before its deadline the next FY is never overdue (no `stale:` code - this keeps a
    # false alarm out, for example at 1 Jan 00:30 when the next year has had no run yet). It is waiting for
    # its first backup instead (AMBER `first_run:`). A RED `last_run:` for the failing site is allowed.
    cfg = _cloud_failing_site(tmp_path)
    deadline = _deadline(cfg, _at(2026, 12, 31, 6))             # no success yet: counted from activation
    for when in (_at(2027, 1, 1, 0, 30), deadline.subtract(seconds=60)):
        codes = _codes(cfg, when)
        assert not [c for c in codes if c.startswith("stale:") and "FY27-28" in c], when
        assert "first_run:cloud:FY27-28" in codes, when


def test_next_fy_overdue_is_counted_from_activation(tmp_path):
    cfg = _cloud_failing_site(tmp_path)
    deadline = _deadline(cfg, _at(2026, 12, 31, 6))
    codes = _codes(cfg, deadline.add(seconds=60))
    assert "stale:cloud:FY27-28" in codes, sorted(codes)
