"""FY overlap, timeline A (activation on 1 January) and single-point cards P-3, P-4, P-12.
Source: agent_runs/OVERLAP_REVIEW.md section 2 (cards T-a1..T-a6, P-3, P-4, P-12).

Every test builds its own world on a fresh rig (real flow.backup / rollover_check_flow / ensure_overlap_activated,
real SQLite, real SMTP server on loopback, real temp folders; only rclone/robocopy, the NAS wake/shutdown and the
backup slot are faked) and replays the earlier steps of its timeline itself. The clock is `pendulum.travel_to`
(never the system clock). The oracles are fixed dates, hand counts, folder listings, raw SQL and the SMTP server's
own message list - never the code under test."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pendulum
import pytest
from fastapi.testclient import TestClient

import flow
import ui
from core import attention as attn
from core import fy_overlap as ov
from core.guards import MOUNT_MARKER
from core.health import CLOCK_OK, CLOCK_UNVERIFIED
from core.time_utils import IST
from models.config import AppConfig
from test_support import ui_login
from test_support.fake_tools import fake_run_tool, robocopy_log
from test_support.smtp_server import smtp_server
from tests.test_break_flow import (
    DEFAULTS, OK_CLOUD, _FLOW_ATTR, _NoSleepTime, _plain, _stub,
)
from tests.test_ov2_activation import Clock

A, B = "FY26-27", "FY27-28"
BUCKET = "my-bucket"


def at(y, mo, d, h=0, mi=0):
    return pendulum.datetime(y, mo, d, h, mi, tz=IST)


# ── the rig ───────────────────────────────────────────────────────────────────────────────────

class Rig:
    """One per test: a config on temp folders, a real SMTP server, a controllable clock and fake NAS power."""

    def __init__(self, tmp: Path, smtp_port: int, srv):
        self.tmp, self.srv = tmp, srv
        self.src = tmp / "src"
        self.nas = tmp / "nas"
        self.src.mkdir()
        self.nas.mkdir()
        (self.src / A).mkdir()
        (self.src / A / "ledger.xlsx").write_text("this year")
        key = tmp / "key.json"
        key.write_text('{"type": "service_account"}')
        cfg = AppConfig(
            firm_name="AAM",
            paths={"source_drive": str(self.src / A), "lan_destination": "\\\\nas\\share\\" + A,
                   "runtime_dir": str(tmp / "rt"), "gcs_key_path": str(key)},
            wol={"enabled": True, "server_ip": "192.168.10.10", "mac_address": "00:11:22:33:44:55"},
            cloud={"enabled": True, "bucket": BUCKET, "project_number": "123"},
            lan={"enabled": True},
            schedule={"cloud_cron": "0 2 * * *", "lan_cron": "0 21 * * *"},
            dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
            guards={"forbid_local_destination": False},
            notifications={"smtp_host": "127.0.0.1", "smtp_port": smtp_port, "sender": "b@firm.example",
                           "recipients": ["ops@firm.example"], "smtp_security": "none", "smtp_auth": False,
                           "retry_attempts": 1, "retry_base_seconds": 0.01})
        # the model only accepts a UNC NAS path; the test needs a real folder it can create folders in
        object.__setattr__(cfg.paths, "lan_destination", str(self.nas / A))
        # this year's NAS folder as a healthy night leaves it: the mount marker and the mirrored file
        (self.nas / A).mkdir()
        (self.nas / A / MOUNT_MARKER).touch()
        (self.nas / A / "ledger.xlsx").write_text("this year")
        self.cfg = cfg
        self.placeholder = cfg.rollover.source_placeholder_name
        self.db_path = cfg.paths.database_path
        self.clock = Clock(CLOCK_OK)
        self.now = at(2026, 12, 31, 21, 1)
        self.sched = self.now
        # fake NAS power
        self.nas_on = False
        self.wol_calls = 0
        self.wakes = 0
        self.shutdowns: list = []
        # what the faked tools were asked
        self.cloud_argvs: list[list[str]] = []
        self.pipeline_stubs: dict = {}
        self.slots = 0

    # -- clock ----
    def go(self, when, scheduled=None):
        self.now = when
        self.sched = scheduled or when
        pendulum.travel_to(when, freeze=True)

    # -- oracles (raw SQL, never the code under test) ----
    def sql(self, query, *args):
        con = sqlite3.connect(self.db_path)
        try:
            return con.execute(query, args).fetchall()
        finally:
            con.close()

    def runs(self, mode=None):
        q = "SELECT mode, fy, status FROM run_history"
        rows = self.sql(q + (" WHERE mode = ?" if mode else "") + " ORDER BY id", *([mode] if mode else []))
        return [tuple(r) for r in rows]

    def activations(self):
        return self.sql("SELECT fy, created_by_app, activated_at FROM overlap_activations ORDER BY fy")

    def notif(self, key_prefix=""):
        return self.sql("SELECT event_key, kind, status FROM notifications WHERE event_key LIKE ? ORDER BY id",
                        key_prefix + "%")

    def mails(self):
        """What the SMTP server really received: the Subject lines."""
        out = []
        for m in self.srv.messages:
            for line in m["data"].splitlines():
                if line.lower().startswith("subject:"):
                    out.append(line)
                    break
        return out

    def approvals(self):
        return self.sql("SELECT leg, fy, status FROM approvals ORDER BY id")

    # -- flows ----
    def backup(self, mode="all", **kw):
        return flow.backup.fn("config.yaml", mode, **kw)

    def rollover_check(self):
        return flow.rollover_check_flow.fn("config.yaml")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    with smtp_server() as srv:
        r = Rig(tmp_path, srv.server_address[1], srv)
        cfg = r.cfg
        monkeypatch.setattr(flow, "load_config", lambda p: cfg)
        monkeypatch.setattr(flow, "configure_logging", lambda *a, **k: None)
        monkeypatch.setattr(flow, "configure_prefect_bridge", lambda *a, **k: None)
        monkeypatch.setattr(flow, "cleanup_orphaned_robocopy_logs", lambda **k: None)
        monkeypatch.setattr(flow, "_scheduled_time", lambda: r.sched)
        monkeypatch.setattr(flow, "_scheduled_lateness_seconds", lambda: 0.0)
        monkeypatch.setattr(flow, "log_retention_task", MagicMock())
        monkeypatch.setattr(flow, "_flow_run_prefix", lambda: f"f{r.now.format('YYYYMMDDHHmm')}")
        monkeypatch.setattr(flow, "time", _NoSleepTime())
        monkeypatch.setattr(ov, "_default_clock_check", lambda config: r.clock())

        @contextmanager
        def slot(config):
            r.slots += 1
            yield

        monkeypatch.setattr(flow, "_backup_slot", slot)

        def wake(config):
            r.wol_calls += 1
            if not r.nas_on:                 # a real wake only when the NAS is asleep (ensure_server_online probes first)
                r.wakes += 1
                r.nas_on = True
            return True

        def shutdown(config, artefacts_dir=None):
            r.shutdowns.append(artefacts_dir)
            r.nas_on = False
            return {"outcome": "VERIFIED"}

        monkeypatch.setattr(flow, "ensure_server_online", wake)
        monkeypatch.setattr(flow, "lan_shutdown_task", shutdown)
        r.real_tasks = {attr: getattr(flow, attr) for attr in _FLOW_ATTR.values()}      # the real Prefect tasks
        stubs = {name: _stub(default) for name, default in DEFAULTS.items()}
        stubs.update(r.pipeline_stubs)
        for name, attr in _FLOW_ATTR.items():
            if name != "lan_shutdown":
                monkeypatch.setattr(flow, attr, stubs[name])
        monkeypatch.setattr(flow, "cloud_sync_task", _plain(flow.cloud_sync_task))
        monkeypatch.setattr(flow, "lan_sync_task", _plain(flow.lan_sync_task))
        monkeypatch.setattr(flow, "wol_check_task", _plain(flow.wol_check_task))
        r.stubs = stubs
        lan_ok = {"exit_code": 1, "log": robocopy_log(copied=1), "log_encoding": "utf-16"}
        with fake_run_tool("core.cloud_sync", exit_code=OK_CLOUD["exit_code"], log=OK_CLOUD["log"]) as cloud_calls, \
                fake_run_tool("core.lan_sync", **lan_ok) as lan_calls:
            r.cloud_calls, r.lan_calls = cloud_calls, lan_calls
            try:
                yield r
            finally:
                pendulum.travel_back()


def prefixes_of(argv: list[str]) -> list[str]:
    """The FY prefix(es) an rclone argv names (any argument that ends in `<bucket>/<FY>` or contains it)."""
    found = []
    for a in argv:
        for fy in (A, B):
            if f"{BUCKET}/{fy}" in a.replace("\\", "/"):
                found.append(fy)
    return found


def cloud_fys(r: Rig) -> list[str]:
    """The FY of every rclone sync, in call order."""
    out = []
    for c in r.cloud_calls:
        p = prefixes_of(c["argv"])
        if p:
            out.append(p[-1])
    return out


# ── timeline A ────────────────────────────────────────────────────────────────────────────────

def step_a1(r: Rig):
    """T-a1: the LAN flow scheduled on 31 Dec 21:01 runs past midnight."""
    r.go(at(2027, 1, 1, 0, 40), scheduled=at(2026, 12, 31, 21, 1))
    r.backup("lan")


def step_a2(r: Rig):
    """T-a2: the cloud flow scheduled 1 Jan 02:01."""
    r.go(at(2027, 1, 1, 2, 1))
    r.backup("cloud")


def test_a1_a_run_scheduled_on_31_dec_stays_in_december_even_after_midnight(rig):
    step_a1(rig)
    assert rig.runs() == [("lan", A, "LAN_COMPLETE")]             # only the configured FY
    assert rig.activations() == []                                # scheduled date 31 Dec is out of the window
    assert not (rig.src / B).exists()                             # no D:\FY27-28
    assert sorted(os.listdir(rig.src)) == [A]
    assert not (rig.nas / B).exists()
    assert rig.mails() == []
    assert rig.notif("overlap:") == []


def test_a2_first_cloud_run_of_january_activates_and_backs_up_new_fy_first(rig):
    step_a1(rig)
    step_a2(rig)
    # activation row, created by the app, stamped with the (frozen) activation time
    acts = rig.activations()
    assert [(a[0], a[1]) for a in acts] == [(B, 1)]
    assert acts[0][2].startswith("2027-01-01T02:01:00")
    # the folder and its placeholder, nothing else
    assert sorted(os.listdir(rig.src)) == [A, B]
    assert sorted(os.listdir(rig.src / B)) == [rig.placeholder]
    # exactly one INFO mail, in the table and at the SMTP server
    assert rig.notif("overlap:") == [(f"overlap:start:{B}", "INFO", "SENT")]
    assert len(rig.srv.messages) == 1
    # one cloud row per FY, new FY first (the LAN row of T-a1 is first)
    assert rig.runs() == [("lan", A, "LAN_COMPLETE"), ("cloud", B, "CLOUD_COMPLETE"), ("cloud", A, "CLOUD_COMPLETE")]
    # rclone was pointed at FY27-28 first, then FY26-27
    assert cloud_fys(rig) == [B, A]
    assert len(rig.cloud_calls) == 2


def test_a3_the_rollover_check_at_06_00_adds_no_second_activation_or_mail(rig):
    step_a1(rig)
    step_a2(rig)
    mails_before, notes_before = len(rig.srv.messages), rig.notif()
    acts_before = rig.activations()
    rig.go(at(2027, 1, 1, 6, 0))
    assert rig.rollover_check() == "NO_ROLLOVER_NEEDED"
    assert rig.activations() == acts_before                       # no second activation row
    assert len(rig.srv.messages) == mails_before
    assert rig.notif() == notes_before                            # the notifications table is unchanged
    assert [n for n in rig.notif("overlap:")] == [(f"overlap:start:{B}", "INFO", "SENT")]   # and no sibling mail
    assert rig.runs("rollover") == []


def step_a3(r: Rig):
    """T-a3: the daily rollover check at 06:00."""
    r.go(at(2027, 1, 1, 6, 0))
    r.rollover_check()


def step_a4(r: Rig):
    """T-a4: the LAN flow at 21:01 on 1 January."""
    r.go(at(2027, 1, 1, 21, 1))
    r.backup("lan")


def test_a4_first_lan_night_creates_the_early_nas_folder_and_powers_the_nas_once(rig):
    step_a1(rig)
    step_a2(rig)
    step_a3(rig)
    lan_rows_before = rig.runs("lan")
    wakes_before, shutdowns_before = rig.wakes, len(rig.shutdowns)
    step_a4(rig)
    # the NAS folder of FY27-28 and its mount marker (nothing else) exist
    assert sorted(os.listdir(rig.nas)) == [A, B]
    assert sorted(os.listdir(rig.nas / B)) == [MOUNT_MARKER]
    # two LAN rows this night, the early FY first
    assert rig.runs("lan")[len(lan_rows_before):] == [("lan", B, "LAN_COMPLETE"), ("lan", A, "LAN_COMPLETE")]
    # ONE wake (the NAS was asleep once) and ONE shutdown decision for the two folders
    assert rig.wakes - wakes_before == 1
    assert len(rig.shutdowns) - shutdowns_before == 1
    assert rig.nas_on is False
    assert rig.slots == 3                                    # one hold per leg run (T-a1, T-a2, T-a4), not per folder
    assert len(rig.lan_calls) == 1 + 2                       # robocopy ran once on 31 Dec and once per folder tonight


# ── T-a5: the dashboard ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def dash(rig, monkeypatch):
    from unittest.mock import AsyncMock
    from core.manifest import ManifestDB

    db = ManifestDB.from_config(rig.cfg)
    monkeypatch.setattr(ui, "_cfg", lambda: rig.cfg)
    monkeypatch.setattr(ui, "get_db", lambda: db)
    monkeypatch.setattr(ui, "_is_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_is_verify_running", AsyncMock(return_value=False))
    monkeypatch.setattr(ui, "_require_auth", lambda request: None)
    monkeypatch.setattr(ui, "_check_rate_limit", lambda key, max_attempts: True)
    ui._RATE_LIMITS.clear()
    yield TestClient(ui.app, raise_server_exceptions=False)
    db.close()


def test_a5_status_shows_the_early_fy_card_first_and_both_green(rig, dash):
    step_a1(rig)
    step_a2(rig)
    step_a3(rig)
    step_a4(rig)
    rig.go(at(2027, 1, 2, 2, 1))          # the next night's cloud run, so the cloud card is not waiting for it
    rig.backup("cloud")
    rig.go(at(2027, 1, 2, 9, 0))
    data = dash.get("/status").json()
    folders = data["folders"]
    assert [f["fy"] for f in folders] == [B, A]
    early, current = folders
    assert early["role"] == "early" and current["role"] == "current"
    assert early["active_since"].startswith("2027-01-01T02:01")
    assert current["active_since"] is None
    for f in folders:
        for leg, ok in (("cloud", "CLOUD_COMPLETE"), ("lan", "LAN_COMPLETE")):
            assert f[leg]["last_run"]["status"] == ok, (f["fy"], leg)
            assert f[leg]["last_success"], (f["fy"], leg)
            assert f[leg]["timing"]["state"] == "on_time", (f["fy"], leg, f[leg]["timing"])
    assert data["fy_prefix"] == A                                  # the top level is still the configured FY
    assert [i for i in data["attention"] if i["level"] == attn.RED] == []


# ── T-a6: the clock cannot be proven on 1-2 January ───────────────────────────────────────────

def mail_text(message: dict) -> str:
    """Decoded text of a message the SMTP server received."""
    import email

    msg = email.message_from_string(message["data"])
    return " ".join(p.get_payload(decode=True).decode("utf-8", "replace")
                    for p in msg.walk() if p.get_content_type() == "text/plain")


def names_fy(items: list[dict], fy: str) -> list[dict]:
    """Attention items (red or amber) that name `fy` in their code or text."""
    return [i for i in items if fy in i["code"] or fy in i["message"]]


def _deferred_days(r: Rig):
    """1-2 January with an unverifiable clock: the 02:01 cloud and 21:01 LAN runs of both days."""
    r.clock.state, r.clock.reason = CLOCK_UNVERIFIED, "no time server answered"
    r.go(at(2027, 1, 1, 2, 1))
    r.backup("cloud")
    r.go(at(2027, 1, 1, 21, 1))
    r.backup("lan")
    r.go(at(2027, 1, 2, 2, 1))
    r.backup("cloud")
    mails_at_24h = len(rig_mails(r))
    r.go(at(2027, 1, 2, 21, 1))
    r.backup("lan")
    return mails_at_24h


def rig_mails(r: Rig):
    return r.srv.messages


def test_a6_unproven_clock_defers_then_one_mail_after_24h_then_activates_on_3_january(rig):
    mails_at_24h = _deferred_days(rig)
    # nothing was activated, nothing created, only the configured FY ran (4 runs)
    assert rig.activations() == []
    assert sorted(os.listdir(rig.src)) == [A]
    assert rig.runs() == [("cloud", A, "CLOUD_COMPLETE"), ("lan", A, "LAN_COMPLETE"),
                          ("cloud", A, "CLOUD_COMPLETE"), ("lan", A, "LAN_COMPLETE")]
    # exactly 24 h after the first deferral (Jan 2 02:01) nothing was sent yet; the 21:01 run is the first past 24 h
    assert mails_at_24h == 0
    assert len(rig.srv.messages) == 1
    assert rig.notif("overlap:") == [(f"overlap:activation:{B}", "ACTION_REQUIRED", "SENT")]
    # one more deferred run later the same day: still ONE mail
    rig.go(at(2027, 1, 2, 22, 30))
    rig.backup("lan")
    assert len(rig.srv.messages) == 1
    # 3 January: the clock is proven, the next run activates on its own
    rig.clock.state, rig.clock.reason = CLOCK_OK, ""
    rig.go(at(2027, 1, 3, 2, 1))
    rig.backup("cloud")
    acts = rig.activations()
    assert [(a[0], a[1]) for a in acts] == [(B, 1)] and acts[0][2].startswith("2027-01-03T02:01")
    assert rig.runs("cloud")[-2:] == [("cloud", B, "CLOUD_COMPLETE"), ("cloud", A, "CLOUD_COMPLETE")]
    assert [n[0] for n in rig.notif("overlap:")] == [f"overlap:activation:{B}", f"overlap:start:{B}"]
    assert len(rig.srv.messages) == 3          # + the ONE RESOLVED mail (overlap-review D3 c)


def test_a6_d3_dashboard_shows_an_item_while_the_activation_is_deferred(rig):
    from core.manifest import ManifestDB

    _deferred_days(rig)
    db = ManifestDB.from_config(rig.cfg)
    try:
        items = attn.compute_attention(db, rig.cfg, now=at(2027, 1, 2, 22, 0))
    finally:
        db.close()
    assert names_fy(items, B), f"no attention item names {B}: {[(i['level'], i['code']) for i in items]}"


# ── P-12: a junction where next year's folder should be ───────────────────────────────────────

def _junction(link: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if done.returncode != 0:
        pytest.skip(f"cannot create a junction here: {done.stdout} {done.stderr}")


def _month_of_junction(r: Rig):
    """30 days (1-30 January) of the daily rollover check with a provable clock and a junction named FY27-28."""
    elsewhere = r.tmp / "other_disk" / "stuff"
    _junction(r.src / B, elsewhere)
    for n in range(10):
        (elsewhere / f"f{n}.xlsx").write_text("staff work")
    first_alert = None
    for day in range(1, 31):
        r.go(at(2027, 1, day, 6, 0))
        r.rollover_check()
        if first_alert is None and r.notif("overlap:activation:"):
            first_alert = day
    return elsewhere, first_alert


def test_p12_a_junction_is_never_used_and_the_owner_hears_at_once(rig):
    elsewhere, first_alert = _month_of_junction(rig)
    assert rig.activations() == []                                  # never activated
    assert sorted(os.listdir(elsewhere)) == sorted(f"f{n}.xlsx" for n in range(10))   # nothing touched
    assert os.path.isjunction(rig.src / B)
    assert first_alert == 1                                         # LC2a: a link needs a person, so the first refusal (1 Jan 06:00) is mailed, no 24 h wait
    alerts = rig.notif("overlap:activation:")
    assert alerts and set(alerts) == {(f"overlap:activation:{B}", "ACTION_REQUIRED", "SENT")}
    assert "shortcut" in mail_text(rig.srv.messages[0])             # the mail says why


def test_p12_d3_deferred_junction_shows_an_attention_item(rig):
    from core.manifest import ManifestDB

    _month_of_junction(rig)
    db = ManifestDB.from_config(rig.cfg)
    try:
        items = attn.compute_attention(db, rig.cfg, now=at(2027, 1, 30, 7, 0))
    finally:
        db.close()
    assert names_fy(items, B), f"no attention item names {B}: {[(i['level'], i['code']) for i in items]}"


def test_p12_d3_deferred_junction_is_mailed_again_every_day(rig):
    _month_of_junction(rig)
    last_week = rig.sql("SELECT event_key FROM notifications WHERE event_key = ? AND created_at >= ?",
                        f"overlap:activation:{B}", at(2027, 1, 24).isoformat())
    assert len(last_week) >= 6, f"expected a daily repeat, got {len(last_week)} mail(s) in the last 7 days"


# ── P-3 / P-4: the NAS folder of the early FY and guard G5 ────────────────────────────────────

@contextmanager
def real_lan_guards(r: Rig, monkeypatch):
    """The LAN pipeline with the REAL preflight, destination walk and guards (G1-G5); only robocopy /L is a
    fake that reports an in-sync mirror (nothing to delete). Everything the card is about - the mount marker
    and the files on the 'NAS' - is read from the real temp folders."""
    for name in ("lan_preflight_task", "lan_snapshot_before_task", "lan_guards_task"):
        monkeypatch.setattr(flow, name, _plain(r.real_tasks[name]))
    with fake_run_tool("core.lan_preflight", exit_code=0, log=robocopy_log(total=1, copied=0, skipped=1),
                       log_encoding="utf-16"):
        yield


def _activated_with_files(r: Rig):
    """1 January 02:01 (cloud run activates), then the office saves a file in next year's folder."""
    step_a2(r)
    (r.src / B / "invoice_jan.xlsx").write_text("saved in january")


def test_p4_nas_folder_with_files_and_no_marker_blocks_only_the_early_fy(rig, monkeypatch):
    _activated_with_files(rig)
    (rig.nas / B).mkdir()
    (rig.nas / B / "somebody_elses.xlsx").write_text("already on the share")       # files, no marker
    rig.nas_on = True
    with real_lan_guards(rig, monkeypatch):
        rig.go(at(2027, 1, 1, 21, 1))
        with pytest.raises(ExceptionGroup):                       # the blocked folder makes the flow fail
            rig.backup("lan")
    assert rig.runs("lan") == [("lan", B, "LAN_BLOCKED"), ("lan", A, "LAN_COMPLETE")]
    # an approval request for the early FY only, blocked by G5
    assert rig.approvals() == [("lan", B, "PENDING")]
    guards_json = rig.sql("SELECT guards FROM approvals")[0][0]
    assert [g["guard"] for g in json.loads(guards_json)] == ["G5_CANARY"]
    codes = rig.sql("SELECT fault_code FROM notifications WHERE event_key = ?", f"blocked:{B}:lan")
    assert codes == [("AAM-NAS-10",)]
    # nothing was written to the unmarked folder: the marker was NOT created, the foreign file is untouched
    assert sorted(os.listdir(rig.nas / B)) == ["somebody_elses.xlsx"]
    # one NAS decision for the night and it is "stay on": the blocked folder voted no
    assert rig.shutdowns == [] and rig.nas_on is True


def test_p3_a_vanished_early_nas_folder_is_not_silently_recreated(rig, monkeypatch):
    import shutil

    _activated_with_files(rig)
    with real_lan_guards(rig, monkeypatch):
        rig.go(at(2027, 1, 1, 21, 1))
        rig.backup("lan")                                         # the first LAN night creates folder + marker (OV-2.5)
        assert rig.runs("lan")[-2:] == [("lan", B, "LAN_COMPLETE"), ("lan", A, "LAN_COMPLETE")]   # real G1-G5 pass
        assert os.path.isfile(rig.nas / B / MOUNT_MARKER)
        shutil.rmtree(rig.nas / B)                                # the NAS was reset / another volume is mounted
        rig.go(at(2027, 1, 2, 21, 1))
        try:
            rig.backup("lan")
        except ExceptionGroup:
            pass
    assert not os.path.exists(rig.nas / B / MOUNT_MARKER), "the marker of a vanished folder was written again"
    last_b = [r for r in rig.runs("lan") if r[1] == B][-1]
    assert last_b[2] != "LAN_COMPLETE", "the early FY reported a complete mirror onto a folder that vanished"
    assert [r for r in rig.runs("lan") if r[1] == A][-1][2] == "LAN_COMPLETE"      # the configured FY is unaffected
