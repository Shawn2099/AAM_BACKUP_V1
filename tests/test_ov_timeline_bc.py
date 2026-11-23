"""FY overlap timelines B and C (agent_runs/OVERLAP_REVIEW.md section 2: T-b1, T-b2, T-c1..T-c5).

One rig drives the REAL flows (`backup`, `integrity_audit_flow`, `rollover_check_flow`, `weekly_report_flow`)
and the real DB / notify / approvals / journal / guards code. Only the outside world is faked: the two
pipelines are replaced by a double that writes the run row and fires the alerts exactly as a real pipeline
does at its terminal decision (real `_record_run`, `_open_block_request`, `_send_run_alert`,
`_send_recovery_if_needed`, and for the LAN leg of T-c3 the real `lan_guards_task`); the NAS, the backup
slot, the clock check, the config rewrite and gcloud are doubles. Mail goes to a real local SMTP server.
The clock is `pendulum.travel_to` (never the system clock). Oracles are fixed dates, hand counts, argv
captured by the doubles, DB rows and mails received by the SMTP server.

Every card is ONE ordered test (later steps replay the earlier ones through helper functions), so nothing
depends on the execution order of test functions.
"""
import copy
import email as em
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pendulum
import pytest
from fastapi.testclient import TestClient

import flow
import ui
from core import fy_overlap
from core import guards
from core import rollover_journal as rj
from core import statuses
from core.attention import compute_attention
from core.health import CLOCK_OK, ClockResult
from core.manifest import ManifestDB
from core.time_utils import IST, now_iso
from test_support.smtp_server import smtp_server
from tests.test_ov2_activation import _base_cfg
from tests.test_ov2_fy_overlap import CFG_FY, NEXT

CLOUD, LAN = "cloud", "lan"


def at(month, day, hour=2, minute=1, year=2027):
    return pendulum.datetime(year, month, day, hour, minute, tz="Asia/Kolkata")


class Nas:
    """The NAS as the tests see it: a power state and a call log (the oracle for wake / shutdown counts)."""

    def __init__(self):
        self.on = False
        self.wake_calls = 0       # every ensure_server_online call (one per LAN pipeline)
        self.power_ons = 0        # calls that found the NAS off
        self.shutdowns = 0
        self.shutdown_results = []   # queue of dicts to return, then VERIFIED

    def ensure_online(self, *a, **k):
        self.wake_calls += 1
        if not self.on:
            self.power_ons += 1
            self.on = True

    def shutdown(self, config, artefacts_dir=None):
        self.shutdowns += 1
        self.on = False
        return self.shutdown_results.pop(0) if self.shutdown_results else {"outcome": "VERIFIED"}


class World:
    """Config, DB, SMTP and every double of one test."""

    def __init__(self, tmp_path, srv):
        self.tmp = tmp_path
        self.srv = srv
        self.src, self.nas_root = tmp_path / "src", tmp_path / "nas"
        for fy in (CFG_FY, NEXT):
            (self.src / fy).mkdir(parents=True)
            (self.src / fy / "a.xlsx").write_text("x", encoding="utf-8")
            (self.nas_root / fy).mkdir(parents=True)
            (self.nas_root / fy / ".AAM_TARGET_MOUNTED").write_text("", encoding="utf-8")
        cfg = _base_cfg(tmp_path, source=str(self.src / CFG_FY), nas=str(self.nas_root / CFG_FY))
        cfg.paths.database_path = str(tmp_path / "m.db")
        cfg.paths.runtime_dir = str(tmp_path / "rt")
        n = cfg.notifications
        n.smtp_host, n.smtp_port = "127.0.0.1", srv.server_address[1]
        n.sender, n.recipients = "b@firm.example", ["o@firm.example"]
        n.smtp_security, n.smtp_auth = "none", False
        n.retry_attempts, n.retry_base_seconds = 1, 0.01
        cfg.dashboard.public_url = "http://backup-server:8080"
        cfg.wol.enabled = True                       # switched on after validation (no MAC needed here)
        cfg.lan.shutdown_after_backup = True
        self.cfg = cfg
        self.nas = Nas()
        self.calls, self.events = [], []
        self.outcome = {}              # (leg, fy) -> "OK" | "BLOCKED" | "PARTIAL"
        self.slot_hooks = []           # callables run when the next slot is taken
        self.lan_guard_numbers = None  # (total files, planned deletes, destination files) -> real guards
        self.flows = 0
        self.rollover_cmds = {"switch": [], "argv": []}
        self.audit_calls = []

    # ---- helpers ----
    def db(self):
        return ManifestDB(self.cfg.paths.database_path)

    def view(self, fy):
        return fy_overlap.folder_view(self.cfg, fy)

    def activate(self, when=None):
        db = self.db()
        db.record_activation(NEXT, activated_at=(when or at(1, 1)).isoformat(), source_path=str(self.src / NEXT),
                             created_by_app=True, clock_state="OK")
        db.close()

    def mails(self):
        return [em.message_from_string(m["data"]) for m in self.srv.messages]

    def subjects(self):
        return [m["Subject"] for m in self.mails()]

    def calls_of(self, leg=None):
        return [(c.leg, c.fy) for c in self.calls if leg in (None, c.leg)]

    def go(self, when):
        pendulum.travel_to(when, freeze=True)

    def tick(self, minutes):
        pendulum.travel_to(pendulum.now(IST).add(minutes=minutes), freeze=True)

    def backup(self, mode, **kw):
        self.flows += 1
        return flow.backup.fn("cfg.yaml", mode, **kw)

    def rollover(self):
        self.flows += 1
        return flow.rollover_check_flow.fn("cfg.yaml")

    # ---- the pipeline double ----
    def pipeline(self, leg):
        def run(config, run_id, started_at, monotonic_start=None, **kw):
            fy = flow._effective_fy_prefix(config)
            self.calls.append(SimpleNamespace(leg=leg, fy=fy, run_id=run_id, kw=kw))
            self.events.append(f"{leg}:{fy}:{'final' if '-final-' in run_id else 'run'}")
            if leg == LAN:
                self.nas.ensure_online()
            started = now_iso()
            self.tick(20)
            outcome = self.outcome.get((leg, fy), "OK")
            record, metrics = None, None
            if leg == LAN and self.lan_guard_numbers and outcome == "OK":
                total, deletes, dest_files = self.lan_guard_numbers
                dry = {"counts": {"total": total, "extras": deletes}, "planned_deletes": deletes,
                       "canary_present": True}
                try:
                    record = flow.lan_guards_task.fn(config, fy, dry, dest_files, run_id=run_id)
                except guards.GuardBlocked as e:
                    outcome, record = "BLOCKED", e.record
            if outcome == "BLOCKED":
                record = record or {"fy": fy, "results": [{
                    "guard": "G3_MASS_DELETE", "verdict": guards.BLOCK, "metrics": {},
                    "reason": "The mirror would delete far more files than allowed."}]}
            status = {"OK": f"{leg.upper()}_COMPLETE", "BLOCKED": f"{leg.upper()}_BLOCKED",
                      "PARTIAL": f"{leg.upper()}_PARTIAL"}[outcome]
            metrics = flow._with_guards(None, record)
            flow._record_run(config, run_id, leg, started, status, 0 if outcome == "OK" else 1,
                             None if outcome == "OK" else "double", extended_metrics=metrics)
            if "shutdown_votes" in kw:
                kw["shutdown_votes"].append((outcome == "OK", None))
            if outcome == "BLOCKED":
                flow._open_block_request(config, leg, run_id, record, started)
                raise guards.GuardBlocked(leg, record)
            if outcome == "PARTIAL":
                flow._send_run_alert(config, leg, status, "Some files were not copied.", 1, started,
                                     run_id=run_id)
                raise flow.PartialRun(f"{leg} partial")
            flow._send_recovery_if_needed(config, leg, run_id)
            return {"status": status}
        return run

    def install(self, mp):
        w = self
        mp.setattr(flow, "load_config", lambda p: copy.deepcopy(w.cfg))
        for name in ("configure_logging", "configure_prefect_bridge"):
            mp.setattr(flow, name, lambda *a, **k: None)
        mp.setattr(flow, "cleanup_orphaned_robocopy_logs", lambda **k: None)
        mp.setattr(flow, "log_retention_task", lambda c: None)
        mp.setattr(flow, "_flow_run_prefix", lambda: f"f{w.flows}")
        mp.setattr(flow, "check_clock", lambda **k: ClockResult(CLOCK_OK, 1.0, "google", ""))
        mp.setattr(fy_overlap, "ensure_overlap_activated", lambda *a, **k: {"state": "already_active"})

        @contextmanager
        def slot(config):
            w.events.append("slot+")
            if w.slot_hooks:
                w.slot_hooks.pop(0)()
            try:
                yield
            finally:
                w.events.append("slot-")

        mp.setattr(flow, "_backup_slot", slot)
        mp.setattr(flow, "_run_cloud_pipeline", self.pipeline(CLOUD))
        mp.setattr(flow, "_run_lan_pipeline", self.pipeline(LAN))
        mp.setattr(flow, "lan_shutdown_task", self.nas.shutdown)
        mp.setattr(flow, "wol_check_task", lambda c: self.nas.ensure_online())
        mp.setattr(flow, "ensure_server_online", self.nas.ensure_online)
        mp.setattr(guards, "resolve_host", lambda host: {"192.0.2.10"})
        mp.setattr(guards, "local_addresses", lambda: set())
        # rollover doubles: the config rewrite (mutates the live config like the real one) and gcloud
        mp.setattr(flow.fy_rollover, "update_config_yaml", self._switch)
        mp.setattr(flow.fy_rollover, "_resolve_gcloud", lambda: "gcloud")
        mp.setattr(flow.fy_rollover, "run_tool", self._run_tool)

    def _switch(self, config_path, src_root, lan_root, new_fy, *, expect_fy=None):
        self.events.append("switch")
        self.rollover_cmds["switch"].append((src_root, lan_root, new_fy, expect_fy))
        self.cfg.paths.source_drive = os.path.join(src_root, new_fy)
        object.__setattr__(self.cfg.paths, "lan_destination", os.path.join(lan_root, new_fy))
        return self.tmp / "config.yaml.bak"

    def _run_tool(self, name, argv, **kw):
        self.events.append(f"tool:{name}")
        self.rollover_cmds["argv"].append(list(argv))
        return SimpleNamespace(completed=True, exit_code=0, describe_failure=lambda: "",
                               stderr_text=lambda: "")


@pytest.fixture
def frozen():
    yield
    pendulum.travel_back()


@pytest.fixture
def world(tmp_path, monkeypatch, frozen):
    with smtp_server() as srv:
        w = World(tmp_path, srv)
        w.install(monkeypatch)
        yield w


# ═══════════════════════════════════════════════════════════════════════════════
# T-b1: one FY blocked on one night, the other stays green, recovery is per FY
# ═══════════════════════════════════════════════════════════════════════════════

def test_t_b1_one_fy_blocked_one_night_the_other_stays_green_and_recovery_is_per_fy(world):
    w = world
    w.activate()

    # Jan 5: both folders complete (new FY first), no mail at all
    w.go(at(1, 5))
    w.backup("cloud")
    assert w.calls_of() == [(CLOUD, NEXT), (CLOUD, CFG_FY)]
    assert w.subjects() == []

    # Jan 6: FY26-27 is BLOCKED by G3, FY27-28 completes; the flow FAILS
    w.go(at(1, 6))
    w.outcome[(CLOUD, CFG_FY)] = "BLOCKED"
    with pytest.raises(ExceptionGroup) as ei:
        w.backup("cloud")
    assert [type(e) for e in ei.value.exceptions] == [guards.GuardBlocked]
    assert w.calls_of()[2:] == [(CLOUD, NEXT), (CLOUD, CFG_FY)]
    db = w.db()
    assert db.last_run(CLOUD, fy=NEXT)["status"] == "CLOUD_COMPLETE"
    assert db.last_run(CLOUD, fy=CFG_FY)["status"] == "CLOUD_BLOCKED"
    [req] = db.open_approvals()                                    # exactly one request, for FY26-27 only
    assert (req["fy"], req["leg"], req["status"]) == (CFG_FY, CLOUD, "PENDING")
    assert req["blocked_run_id"] == db.last_run(CLOUD, fy=CFG_FY)["run_id"]
    # exactly one mail, about FY26-27; nothing says FY27-28 had a problem
    [(subject, _)] = [(s, None) for s in w.subjects()]
    assert f"[{CFG_FY}]" in subject and NEXT not in subject
    # the dashboard: a red approval item for FY26-27 only, no cloud item at all for FY27-28
    items = compute_attention(db, w.cfg, now=at(1, 6, 9, 0))
    codes = [i["code"] for i in items if i["code"].startswith(("approval", "stale:", "backup:", "audit:"))
             or CLOUD in i["code"]]
    assert f"approval:{CFG_FY}:{CLOUD}" in codes
    assert not [c for c in codes if NEXT in c and CLOUD in c]
    db.close()

    # Jan 7: FY26-27 completes again -> ONE RECOVERED mail, for FY26-27, in the blocked mail's thread
    w.go(at(1, 7))
    w.outcome.clear()
    w.backup("cloud")
    mails = w.mails()
    assert len(mails) == 2
    first, second = mails
    assert f"[{CFG_FY}]" in second["Subject"] and "RESOLVED" in second["Subject"]
    assert second["In-Reply-To"] == first["Message-ID"]
    db = w.db()
    keys = [r[0] for r in db._get_conn().execute(
        "SELECT event_key FROM notifications WHERE kind = 'RECOVERED'").fetchall()]
    assert keys == [f"recovered:{CFG_FY}:{CLOUD}"]                 # no RECOVERED for FY27-28, ever
    assert db.last_run(CLOUD, fy=NEXT)["status"] == "CLOUD_COMPLETE"
    db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# T-b2: ten LAN nights, one PARTIAL night keeps the NAS on; weekly audit of both FYs
# ═══════════════════════════════════════════════════════════════════════════════

def test_t_b2_ten_lan_nights_one_partial_keeps_nas_on_and_the_sunday_audit_covers_both_fys(world, monkeypatch):
    w = world
    w.activate()
    nas = w.nas
    nas.shutdown_results = [None] * 0
    nights = [at(1, 5 + i) for i in range(10)]            # Tue 5 Jan .. Thu 14 Jan 2027
    partial_night = 3                                     # 0-based: the 4th night
    unconfirmed_night = 6                                 # shutdown not confirmed on the 7th night
    expected_after = [False, False, False, True, False, False, False, False, False, False]
    audit_deltas = None
    before_power_ons = before_calls = before_shutdowns = 0
    for i, when in enumerate(nights):
        w.go(when)
        w.outcome.clear()
        if i == partial_night:
            w.outcome[(LAN, NEXT)] = "PARTIAL"
        if i == unconfirmed_night:
            nas.shutdown_results = [{"outcome": "STILL_ON", "detail": "the NAS still answered ping"}]
        calls_before, ons_before, sd_before = nas.wake_calls, nas.power_ons, nas.shutdowns
        if i == partial_night:
            with pytest.raises(ExceptionGroup):
                w.backup(LAN)
        else:
            w.backup(LAN)
        assert nas.wake_calls - calls_before == 2, i              # one wake per folder pipeline, two folders
        assert nas.shutdowns - sd_before == (0 if i == partial_night else 1), i
        assert nas.on is expected_after[i], i
        # run order every night: new FY first
        assert w.calls_of(LAN)[-2:] == [(LAN, NEXT), (LAN, CFG_FY)]
        if i == unconfirmed_night:
            db = w.db()
            codes = [x["code"] for x in compute_attention(db, w.cfg, now=when.add(hours=8))]
            assert "nas_shutdown" in codes
            db.close()
        if i == unconfirmed_night + 1:
            db = w.db()
            codes = [x["code"] for x in compute_attention(db, w.cfg, now=when.add(hours=8))]
            assert "nas_shutdown" not in codes                      # the item follows the LAST run
            db.close()
        if when.date() == pendulum.date(2027, 1, 10):                # Sunday: the weekly audit, after the night
            audit_deltas = _sunday_audit(w, monkeypatch, at(1, 10, 10, 0))
        if i == 0:
            before_calls, before_power_ons, before_shutdowns = 0, 0, 0

    # hand counts over the ten NIGHTS (the Sunday audit is counted apart below)
    nights_wake_calls = 2 * 10
    nights_shutdowns = 9
    nights_power_ons = 9          # off at the start of every night except the 5th (night 4 left it on)
    assert nas.wake_calls == nights_wake_calls + audit_deltas["wake_calls"]
    assert nas.shutdowns == nights_shutdowns + audit_deltas["shutdowns"]
    assert nas.power_ons == nights_power_ons + audit_deltas["power_ons"]
    # the Sunday audit: both FYs, both legs, ONE NAS shutdown for the whole audit
    assert audit_deltas["rows"] == [(CLOUD, NEXT), (CLOUD, CFG_FY), (LAN, NEXT), (LAN, CFG_FY)]
    assert audit_deltas["shutdowns"] == 1
    # LAN rows: 20 (2 per night); the PARTIAL one belongs to FY27-28 and night 4 only
    db = w.db()
    rows = db._get_conn().execute("SELECT fy, status FROM run_history WHERE mode = 'lan'").fetchall()
    assert len(rows) == 20
    assert [r[1] for r in rows if r[0] == NEXT].count("LAN_PARTIAL") == 1
    assert [r[1] for r in rows if r[0] == CFG_FY].count("LAN_COMPLETE") == 10
    db.close()


def _sunday_audit(w, monkeypatch, when):
    """The weekly integrity audit on the rig; returns what it did (rows, NAS call deltas)."""
    def audit(source, *args, **kw):
        w.audit_calls.append(os.path.basename(str(source).rstrip("\\/")))
        return {"status": "VERIFIED", "scope": "full", "files_checked": 1, "bytes_checked": None,
                "mismatches": 0, "detail": "verified clean"}

    monkeypatch.setattr("core.integrity.audit_cloud", audit)
    monkeypatch.setattr("core.integrity.audit_lan", audit)
    monkeypatch.setattr(flow, "_open_run_artefacts", lambda c, i: None)
    monkeypatch.setattr(flow, "temp_rclone_config", lambda *a, **k: _NullCtx())
    w.go(when)
    calls, ons, sd = w.nas.wake_calls, w.nas.power_ons, w.nas.shutdowns
    w.flows += 1
    flow.integrity_audit_flow.fn("cfg.yaml", "all")
    db = w.db()
    rows = [(r[0], r[1]) for r in db._get_conn().execute(
        "SELECT mode, fy FROM integrity_audits ORDER BY rowid").fetchall()]
    db.close()
    return {"rows": rows, "wake_calls": w.nas.wake_calls - calls, "power_ons": w.nas.power_ons - ons,
            "shutdowns": w.nas.shutdowns - sd}


class _NullCtx:
    def __enter__(self):
        return "rclone.conf"

    def __exit__(self, *a):
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Timeline C: the 1 April changeover, replayed step by step on one rig
# ═══════════════════════════════════════════════════════════════════════════════

def _guard_dict(fy, files, nas_root):
    return {"fy": fy, "source_file_count": files, "destination": str(nas_root / fy)}


def seed_c(w):
    """Before 1 April: FY27-28 active since 1 Jan with 90 nights of successes, FY26-27 alongside; one PENDING
    approval for each FY; one OPEN incident of FY27-28 (its last cloud night, 31 March, failed)."""
    w.activate(at(1, 1))
    db = w.db()
    day0 = pendulum.date(2027, 1, 1)
    for n in range(1, 91):                                         # 1 Jan .. 31 Mar = 90 nights
        d = day0.add(days=n - 1)
        iso = lambda h, m=1: pendulum.datetime(d.year, d.month, d.day, h, m, tz=IST).isoformat()  # noqa: E731
        failed = n == 90
        for fy, files in ((NEXT, 100 + n), (CFG_FY, 5000)):
            db.insert_run({
                "run_id": f"seed-{fy}-cloud-{n}", "mode": CLOUD, "fy": fy, "started_at": iso(2),
                "ended_at": iso(3), "status": "CLOUD_FAILED" if failed and fy == NEXT else "CLOUD_COMPLETE",
                "extended_metrics": json.dumps({"fault_code": "AAM-CLD-01"}) if failed and fy == NEXT else None})
            db.insert_run({
                "run_id": f"seed-{fy}-lan-{n}", "mode": LAN, "fy": fy, "started_at": iso(21),
                "ended_at": iso(22), "status": "LAN_COMPLETE",
                "extended_metrics": json.dumps({"guards": _guard_dict(fy, files, w.nas_root)})})
    # the open FY27-28 incident: the real failure alert of its last cloud night
    w.go(at(3, 31, 2, 30))
    flow._send_run_alert(w.view(NEXT), CLOUD, "CLOUD_FAILED", "a file could not be read", 1,
                         at(3, 31).isoformat(), run_id=f"seed-{NEXT}-cloud-90", fault_code="AAM-CLD-01")
    w.ap_next = db.open_approval_request(
        leg=CLOUD, blocked_run_id="seed-blocked-next", fy=NEXT, created_at=at(3, 30).isoformat(), phrase="APPROVE",
        guards=[{"guard": "G3_MASS_DELETE", "reason": "r", "metrics": {}}])
    w.ap_cfg = db.open_approval_request(
        leg=LAN, blocked_run_id="seed-blocked-cfg", fy=CFG_FY, created_at=at(3, 30).isoformat(), phrase="APPROVE",
        guards=[{"guard": "G2_SOURCE_SHRINK", "reason": "r", "metrics": {}}])
    db.close()
    assert len(w.mails()) == 1
    w.seed_message_id = w.mails()[0]["Message-ID"]


def step_c1(w):
    """Cloud 2027-04-01 02:01: the config still says FY26-27, the calendar says FY27-28: both are backed up."""
    w.go(at(4, 1, 2, 1))
    w.backup(CLOUD)


def request_next_after_c1(w):
    """A new FY27-28 cloud approval request AFTER the 1 April cloud night (the 30 March one was superseded by
    that night's success), so it stays PENDING until a later cloud success of FY27-28 (2 April in T-c4)."""
    db = w.db()
    w.ap_next = db.open_approval_request(
        leg=CLOUD, blocked_run_id="seed-blocked-next-2", fy=NEXT, created_at=at(4, 1, 3, 0).isoformat(),
        phrase="APPROVE", guards=[{"guard": "G3_MASS_DELETE", "reason": "r", "metrics": {}}])
    db.close()


def step_c2(w):
    """Rollover check 06:00 on 1 April."""
    w.go(at(4, 1, 6, 0))
    return w.rollover()


def step_c3(w):
    """LAN 21:01 on 1 April, through the REAL guards (baseline from the 31 March FY27-28 row)."""
    w.go(at(4, 1, 21, 1))
    w.lan_guard_numbers = (188, 2, 188)      # 188 source files (baseline 190), 2 planned deletes of 188
    w.backup(LAN)


def test_t_c1_both_fys_are_backed_up_on_1_april_while_the_switch_is_pending(world):
    w = world
    seed_c(w)
    db = w.db()
    assert [a["fy"] for a in db.open_approvals()] == [NEXT, CFG_FY]
    assert len(db._get_conn().execute("SELECT 1 FROM run_history WHERE fy = ? AND mode = 'lan'", (NEXT,)).fetchall()) == 90
    db.close()
    step_c1(w)
    # both folders, new FY first, on the pending-switch branch (no journal, calendar FY == next FY)
    assert w.calls_of() == [(CLOUD, NEXT), (CLOUD, CFG_FY)]
    db = w.db()
    assert fy_overlap.switch_pending(w.cfg, db, pendulum.date(2027, 4, 1)) is True
    for fy in (NEXT, CFG_FY):
        row = db.last_run(CLOUD, fy=fy)
        assert row["status"] == "CLOUD_COMPLETE" and row["started_at"].startswith("2027-04-01"), fy
    # the cloud night of FY27-28 succeeded after its cloud approval was requested (30 Mar): that request is
    # SUPERSEDED (02ddc18: the cause is fixed). The FY26-27 request is a LAN one: a cloud run does not touch it.
    assert db.get_approval(w.ap_next)["status"] == "SUPERSEDED"
    assert sorted((a["fy"], a["status"]) for a in db.open_approvals()) == [(CFG_FY, "PENDING")]
    assert w.cfg.paths.source_drive.endswith(CFG_FY)
    # nothing else moved: the config still says FY26-27
    assert w.cfg.paths.source_drive.endswith(CFG_FY)
    db.close()
    # the FY27-28 incident recovered inside its OWN thread; FY26-27 (no incident) said nothing
    mails = w.mails()
    assert len(mails) == 2
    assert f"[{NEXT}]" in mails[1]["Subject"] and "RESOLVED" in mails[1]["Subject"]
    assert mails[1]["In-Reply-To"] == w.seed_message_id


def test_t_c2_the_rollover_runs_final_backups_of_fy26_27_only_then_switches_and_archives_it(world):
    w = world
    seed_c(w)
    step_c1(w)
    request_next_after_c1(w)
    n_calls, n_mails = len(w.calls), len(w.mails())
    assert step_c2(w) == "ROLLOVER_COMPLETED"
    final = w.calls[n_calls:]
    # final backup: FY26-27 only, one cloud + one LAN, ids carry -final-, the LAN one leaves the NAS on
    assert [(c.leg, c.fy) for c in final] == [(CLOUD, CFG_FY), (LAN, CFG_FY)]
    assert all("-final-" in c.run_id for c in final)
    assert final[1].kw["allow_shutdown"] is False
    db = w.db()
    finals = db._get_conn().execute(
        "SELECT mode, fy FROM run_history WHERE run_id LIKE '%-final-%' ORDER BY mode").fetchall()
    assert [tuple(r) for r in finals] == [(CLOUD, CFG_FY), (LAN, CFG_FY)]
    # the order of the steps: final backups, then the switch, then the archive
    order = [e for e in w.events if e.startswith(("cloud:", "lan:", "switch", "tool:"))]
    assert order[-5:] == ["cloud:FY26-27:final", "lan:FY26-27:final", "switch",
                          "tool:gcloud-check", "tool:gcloud-archive"], order
    # the switch: from FY26-27 (compare-and-set), against the real parents, to FY27-28
    [(src_root, lan_root, to_fy, expect)] = w.rollover_cmds["switch"]
    assert (os.path.normpath(src_root), os.path.normpath(lan_root), to_fy, expect) == (
        os.path.normpath(str(w.src)), os.path.normpath(str(w.nas_root)), NEXT, CFG_FY)
    # the archive: only the closed year's prefix, never the live one
    archive = [a for a in w.rollover_cmds["argv"] if "update" in a]
    assert len(archive) == 1 and f"gs://my-bucket/{CFG_FY}/" in archive[0]
    assert not any(NEXT in part for argv in w.rollover_cmds["argv"] for part in argv)
    # the journal is finished. The old year's LAN request (30 Mar) was SUPERSEDED by the final LAN backup's
    # success (a terminal status close_fy leaves alone); the new year keeps its PENDING request
    steps = db.rollover_get(CFG_FY, NEXT)["steps"]
    assert {k: v["state"] for k, v in steps.items()} == {s: rj.DONE for s in rj.STEPS}
    assert db.get_approval(w.ap_cfg)["status"] == "SUPERSEDED"
    assert db.get_approval(w.ap_next)["status"] == "PENDING"
    assert [a["approval_id"] for a in db.open_approvals()] == [w.ap_next]
    db.close()
    # the NAS is powered off once, at the end of the rollover; one INFO mail says the year started
    assert w.nas.shutdowns == 1
    new_mails = w.subjects()[n_mails:]
    assert len(new_mails) == 1 and "New financial year FY27-28 started" in new_mails[0]
    # ... and the FY27-28 approval can still be RUN, for FY27-28
    n = len(w.calls)
    w.go(at(4, 1, 7, 0))
    w.backup(CLOUD, approval_id=w.ap_next, fy=NEXT)
    [call] = w.calls[n:]
    assert (call.leg, call.fy, call.kw["approval_id"]) == (CLOUD, NEXT, w.ap_next)
    # while a run of the CLOSED year is refused
    with pytest.raises(ValueError, match="not being backed up"):
        w.backup(CLOUD, fy=CFG_FY)


def test_t_c3_the_first_lan_night_after_the_switch_backs_up_fy27_28_with_its_own_baseline(world):
    w = world
    seed_c(w)
    step_c1(w)
    request_next_after_c1(w)
    step_c2(w)
    # the early folder's destination was, and the new configured one is, the same string (hand-built)
    assert w.view(NEXT).paths.lan_destination == str(w.nas_root / NEXT)   # the early view, before the switch
    assert w.cfg.paths.lan_destination == str(w.nas_root / NEXT)          # the configured one, after it
    db = w.db()
    baseline = db.guard_baseline(LAN, NEXT)
    assert baseline["source_file_count"] == 190                           # the 31 March row (100 + 90)
    assert baseline["destination"] == str(w.nas_root / NEXT)
    assert db.guard_baseline(LAN, CFG_FY)["source_file_count"] == 5000    # FY26-27's own, untouched
    db.close()
    n = len(w.calls)
    step_c3(w)
    [call] = w.calls[n:]
    assert (call.leg, call.fy) == (LAN, NEXT)
    assert call.kw["early_nas_folder"] is False                           # it is the configured folder now
    db = w.db()
    row = db.last_run(LAN, fy=NEXT)
    assert row["status"] == "LAN_COMPLETE" and row["started_at"].startswith("2027-04-01T21")
    record = json.loads(row["extended_metrics"])["guards"]
    verdicts = {r["guard"]: r["verdict"] for r in record["results"]}
    assert set(verdicts.values()) == {guards.PASS}, verdicts              # G1..G5 incl. G4 (dest) and G3 (not a first run)
    assert {"G2_SOURCE_SHRINK", "G3_MASS_DELETE", "G4_DESTINATION_IDENTITY"} <= set(verdicts)
    assert record["fy"] == NEXT and record["source_file_count"] == 188
    assert db.open_approvals()[0]["approval_id"] == w.ap_next             # no new request: not blocked
    assert len(db.open_approvals()) == 1
    db.close()


def test_t_c4_one_card_on_the_dashboard_and_the_weekly_report_counts_fy26_27_only_up_to_the_switch(
        world, monkeypatch):
    w = world
    seed_c(w)
    step_c1(w)
    request_next_after_c1(w)
    step_c2(w)
    step_c3(w)
    for day in (2, 3, 4):                                                  # Fri 2 .. Sun 4 April: normal nights
        w.go(at(4, day, 2, 1))
        w.backup("all")
    n_mails = len(w.mails())

    # /status on Monday morning: ONE card, the configured year, both legs shown with FY27-28 runs
    w.go(at(4, 5, 8, 0))
    cfg = w.cfg
    db = w.db()
    monkeypatch.setattr(ui, "_cfg", lambda: cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_require_auth", lambda request: None)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    ui._RATE_LIMITS.clear()
    data = TestClient(ui.app, raise_server_exceptions=False).get("/status").json()
    assert [f["fy"] for f in data["folders"]] == [NEXT]
    card = data["folders"][0]
    assert card["role"] == "current"
    for leg in (CLOUD, LAN):
        assert card[leg]["last_run"]["status"] in statuses.SUCCESS_STATUSES
        assert card[leg]["last_run"]["started_at"].startswith("2027-04-04"), leg
    db.close()

    # weekly report of the week 29 Mar - 4 Apr (sent Monday 08:00)
    flow.weekly_report_flow.fn("cfg.yaml")
    mails = w.mails()
    assert len(mails) == n_mails + 1
    body = _text(mails[-1])
    db = w.db()
    week = db._get_conn().execute(
        "SELECT fy, mode, COUNT(*) FROM run_history WHERE started_at >= '2027-03-29' AND started_at < '2027-04-05' "
        "GROUP BY fy, mode ORDER BY fy, mode").fetchall()
    db.close()
    # hand count of the week: FY26-27 cloud 3 (29-31 Mar) + 1 (1 Apr 02:01) + 1 final; LAN 3 + 1 final
    #                         FY27-28 cloud 3 + 1 (1 Apr) + 3 (2-4 Apr); LAN 3 + 1 + 3
    assert [tuple(r) for r in week] == [
        (CFG_FY, CLOUD, 5), (CFG_FY, LAN, 4), (CFG_FY, "rollover", 1), (NEXT, CLOUD, 7), (NEXT, LAN, 7)]
    # the report text (tags stripped): one section per year, new year first, hand counts per leg
    sec_new, sec_old = body.index("FY27-28"), body.index("FY26-27")
    assert sec_new < sec_old
    head = body[:body.index("Summary")]
    new_part, old_part = head[sec_new:sec_old], head[sec_old:]
    assert "6 of 7 runs OK" in new_part and "7 of 7 runs OK" in new_part       # cloud: 31 Mar failed, resolved
    assert "5 of 5 runs OK" in old_part and "4 of 4 runs OK" in old_part       # FY26-27 ends at the switch
    assert "resolved" in new_part
    # no "nothing ran" gap is invented for FY26-27 for 2-4 April (it was closed on 1 April)
    assert "no backup ran" not in old_part.lower() and "missed" not in old_part.lower()
    # no open decision is left: FY26-27's LAN request was superseded by its final backup, and FY27-28's cloud
    # request (1 Apr 03:00) by the successful cloud night of 2 April
    assert "waiting for your decision" not in body
    db = w.db()
    assert db.get_approval(w.ap_next)["status"] == "SUPERSEDED" and db.open_approvals() == []
    db.close()


def _text(msg):
    parts = [p for p in msg.walk() if p.get_content_type() in ("text/plain", "text/html")]
    text = "\n".join(p.get_payload(decode=True).decode("utf-8", "replace") for p in parts)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))


# ═══════════════════════════════════════════════════════════════════════════════
# T-c5 (defect D1): a backup flow that waited for the slot while the rollover switched the year
# ═══════════════════════════════════════════════════════════════════════════════

def test_t_c5_a_flow_that_waited_through_the_switch_does_not_back_up_the_closed_year(world):
    w = world
    seed_c(w)
    db = w.db()

    def rollover_finishes_while_we_waited():
        w.cfg.paths.source_drive = os.path.join(str(w.src), NEXT)          # the config switch ...
        object.__setattr__(w.cfg.paths, "lan_destination", os.path.join(str(w.nas_root), NEXT))
        d = w.db()
        d.rollover_open(CFG_FY, NEXT, at(4, 1, 6, 0).isoformat())
        for step in rj.STEPS:
            d.rollover_step_done(CFG_FY, NEXT, step, at(4, 1, 6, 5).isoformat())
        d.close_fy(CFG_FY, now=at(4, 1, 6, 5).isoformat())                 # ... and the old year is closed
        from core import fy_overlap                                         # (settled: every leg had copied it)
        fy_overlap.mark_year_closed(d, CFG_FY, at(4, 1, 6, 5).isoformat())
        d.close()

    w.slot_hooks.append(rollover_finishes_while_we_waited)
    w.go(at(4, 1, 6, 5))
    w.backup(CLOUD)                                                        # started (and sized up) before the switch
    assert w.calls_of() == [(CLOUD, NEXT)]
    assert db.last_run(CLOUD, fy=CFG_FY)["run_id"] == f"seed-{CFG_FY}-cloud-90"   # no new row for FY26-27
    db.close()
