"""FY overlap: the weekly integrity audit covers the next FY as well (plan 2.5, D-13, task B6).

The audit flow runs in ONE backup slot: per leg the next FY first (it is small), then the configured FY, sharing the
one `budget.audit_max_hours` budget; the LAN leg wakes the NAS once and shuts it down once after the last pass. Each
pass writes its own integrity_audits row (suffixed audit id for the next FY). The fakes are the audit engines
(core.integrity.audit_cloud / audit_lan), the NAS wake/shutdown tasks, the slot, the budget clock and email; the
database, the config on disk and the FY maths are real.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pendulum
import pytest
import yaml

import flow
from core import fy_overlap, statuses
from core import fy_rollover as fr
from core.budget import RunBudget
from core.manifest import ManifestDB
from core.time_utils import IST
from models.config import load_config
from test_support.sandbox_config import _config_dict

VERIFIED = statuses.AuditStatus.VERIFIED
MISMATCH = statuses.AuditStatus.VERIFICATION_FAILED
INCOMPLETE = statuses.AuditStatus.AUDIT_INCOMPLETE


class _Clock:
    """Seconds of the audit's run budget already used (the budget reads this, not the real clock)."""
    now = 0.0

    @classmethod
    def spend(cls, seconds: float) -> None:
        cls.now += seconds


class _FakeBudget(RunBudget):
    def cap(self, timeout_s, reserve_s=0.0, clock=None):
        return super().cap(timeout_s, reserve_s, clock=lambda: _Clock.now)


def _fake_budget(leg, seconds, started):
    _Clock.now = 0.0
    return _FakeBudget(leg, seconds, 0.0)


class Audits:
    """Stands in for core.integrity.audit_cloud / audit_lan: records each pass by (leg, FY of its source)."""

    def __init__(self, log: list):
        self.log = log
        self.verdict: dict[tuple[str, str], str] = {}
        self.on_call: dict[tuple[str, str], object] = {}

    def _run(self, leg: str, source_root: str) -> dict:
        fy = fr._fy_name(source_root)
        self.log.append((leg, fy))
        if (leg, fy) in self.on_call:
            self.on_call[(leg, fy)]()
        status = self.verdict.get((leg, fy), VERIFIED)
        return {"status": status, "scope": "full", "files_checked": 3, "bytes_checked": 30,
                "mismatches": 0 if status == VERIFIED else 2, "detail": f"fake {leg} {fy} {status}"}

    def cloud(self, source_root, bucket, prefix, rclone_cfg, *, timeout, artefacts, pending, **kw):
        return self._run("cloud", source_root)

    def lan(self, source_root, dest_root, *, timeout, artefacts, pending, **kw):
        return self._run("lan", source_root)


@contextmanager
def _env(tmp_path: Path, *, lan_scheduled: bool = True, shutdown: bool = False, wol: bool = False):
    d = _config_dict(tmp_path)                       # source tmp/source/FY26-27, NAS on the sandbox share
    d["lan"]["scheduled"] = lan_scheduled
    d["lan"]["shutdown_after_backup"] = shutdown
    if wol:
        d["wol"] = {"enabled": True, "mac_address": "00:11:22:33:44:55", "server_ip": "192.0.2.10"}
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(d), encoding="utf-8")

    log: list = []
    audits = Audits(log)
    wol_task = MagicMock(side_effect=lambda *a, **k: log.append(("wol", None)))
    shutdown_task = MagicMock(side_effect=lambda *a, **k: log.append(("shutdown", None)))
    slot = MagicMock()
    notify = MagicMock()
    auto = MagicMock(return_value=False)
    with patch("core.integrity.audit_cloud", audits.cloud), \
            patch("core.integrity.audit_lan", audits.lan), \
            patch("flow.temp_rclone_config", lambda *a, **k: nullcontext("rclone.conf")), \
            patch("flow._backup_slot", slot), \
            patch("flow.wol_check_task", wol_task), \
            patch("flow.lan_shutdown_task", shutdown_task), \
            patch("flow.notify", notify), \
            patch("flow.RunBudget", _fake_budget), \
            patch("flow._auto_scheduled_run", auto, create=True):   # new helper (B6); absent on the base code
        yield SimpleNamespace(cfg=cfg, log=log, audits=audits, wol=wol_task, shutdown=shutdown_task,
                              slot=slot, notify=notify, auto=auto)


def _activate_next(cfg: Path, fy: str = "FY27-28") -> None:
    """The activation record the daily check writes on 31 Dec (plan 3, step 4)."""
    config = load_config(str(cfg))
    db = ManifestDB.from_config(config)
    try:
        db.set_overlap_record(fy, {"activated_at": pendulum.now(IST).isoformat(), "from_fy": "FY26-27",
                                   "source_folder": str(Path(config.paths.source_drive).parent / fy),
                                   "clock": {}})
    finally:
        db.close()


def _run(cfg: Path, mode: str = "all", fy: str | None = None) -> None:
    if fy is None:
        flow.integrity_audit_flow.fn(config_path=str(cfg), mode=mode)     # the call the schedule makes
    else:
        flow.integrity_audit_flow.fn(config_path=str(cfg), mode=mode, fy=fy)


def _rows(cfg: Path) -> list[tuple[str, str | None, str]]:
    """(leg, FY of the row or None for the configured FY, status) of every audit row, oldest first."""
    con = sqlite3.connect(load_config(str(cfg)).paths.database_path)
    try:
        found = con.execute("SELECT audit_id, mode, status FROM integrity_audits ORDER BY started_at").fetchall()
    finally:
        con.close()
    return [(mode, fy_overlap.fy_of_id(audit_id), status) for audit_id, mode, status in found]


def _audit_ids(cfg: Path) -> list[str]:
    con = sqlite3.connect(load_config(str(cfg)).paths.database_path)
    try:
        return [r[0] for r in con.execute("SELECT audit_id FROM integrity_audits ORDER BY started_at")]
    finally:
        con.close()


# ── the pass order, the slot, the rows ───────────────────────────────────

def test_next_fy_then_configured_fy_per_leg_in_one_slot(tmp_path):
    with _env(tmp_path) as env:
        _activate_next(env.cfg)
        _run(env.cfg)
        # the LAN leg calls its wake-up task once (the task itself does nothing while WoL is off)
        assert env.log == [("cloud", "FY27-28"), ("cloud", "FY26-27"), ("wol", None),
                           ("lan", "FY27-28"), ("lan", "FY26-27")]
        assert env.slot.call_count == 1


def test_each_pass_has_its_own_row_with_a_suffixed_id_for_the_next_fy(tmp_path):
    with _env(tmp_path) as env:
        _activate_next(env.cfg)
        _run(env.cfg)
        assert sorted(_rows(env.cfg), key=lambda r: (r[0], r[1] or "")) == [
            ("cloud", None, VERIFIED), ("cloud", "FY27-28", VERIFIED),
            ("lan", None, VERIFIED), ("lan", "FY27-28", VERIFIED)]
        ids = _audit_ids(env.cfg)
        assert all(i.endswith("-full") or i.endswith("-full-FY27-28") for i in ids), ids
        db = ManifestDB.from_config(load_config(str(env.cfg)))
        try:
            assert db.latest_audit("cloud", fy="FY27-28")["audit_id"].endswith("-cloud-full-FY27-28")
            assert db.latest_audit("cloud")["audit_id"].endswith("-cloud-full")
            assert db.latest_audit("lan", fy="FY27-28")["audit_id"].endswith("-lan-full-FY27-28")
        finally:
            db.close()


def test_a_mismatch_in_the_next_fy_names_the_fy_in_the_alert_key(tmp_path):
    with _env(tmp_path) as env:
        _activate_next(env.cfg)
        env.audits.verdict[("cloud", "FY27-28")] = MISMATCH
        with pytest.raises(ExceptionGroup):
            _run(env.cfg)
        key = env.notify.call_args.args[1].key
        assert key == "audit:all:cloud@FY27-28=VERIFICATION_FAILED", key
        assert ("lan", "FY26-27") in env.log           # the other passes still ran


# ── one NAS wake and one shutdown for both folders ───────────────────────

def test_one_nas_wake_and_one_shutdown_for_both_fys(tmp_path):
    with _env(tmp_path, shutdown=True, wol=True) as env:
        _activate_next(env.cfg)
        _run(env.cfg)
        assert env.log == [("cloud", "FY27-28"), ("cloud", "FY26-27"), ("wol", None),
                           ("lan", "FY27-28"), ("lan", "FY26-27"), ("shutdown", None)]


def test_a_failed_nas_wake_fails_both_lan_passes_and_shuts_nothing_down(tmp_path):
    with _env(tmp_path, shutdown=True, wol=True) as env:
        env.wol.side_effect = RuntimeError("NAS did not answer the wake-up packet")
        _activate_next(env.cfg)
        with pytest.raises(ExceptionGroup):
            _run(env.cfg)
        assert env.wol.call_count == 1                  # not retried for the second pass
        assert [c for c in env.log if c[0] == "lan"] == []
        assert [r for r in _rows(env.cfg) if r[0] == "lan"] == \
            [("lan", "FY27-28", INCOMPLETE), ("lan", None, INCOMPLETE)]    # next FY first
        assert env.shutdown.call_count == 0


# ── the shared budget ────────────────────────────────────────────────────

def test_budget_used_up_by_the_next_fy_leaves_the_configured_fy_incomplete(tmp_path):
    with _env(tmp_path, shutdown=True, wol=True) as env:
        _activate_next(env.cfg)
        hours = load_config(str(env.cfg)).budget.audit_max_hours
        env.audits.on_call[("cloud", "FY27-28")] = lambda: _Clock.spend(hours * 3600)
        with pytest.raises(ExceptionGroup):
            _run(env.cfg)
        # the next FY's cloud pass is verified; the configured FY gets no time at all, nor does any LAN pass
        assert env.log == [("cloud", "FY27-28")]
        assert _rows(env.cfg) == [("cloud", "FY27-28", VERIFIED), ("cloud", None, INCOMPLETE),
                                  ("lan", "FY27-28", INCOMPLETE), ("lan", None, INCOMPLETE)]
        assert env.wol.call_count == 0                  # the NAS is never woken without budget


# ── scheduled versus manual, and the fy argument ─────────────────────────

def test_scheduled_audit_skips_an_unscheduled_leg_and_a_manual_one_does_not(tmp_path):
    with _env(tmp_path, lan_scheduled=False, shutdown=True, wol=True) as env:
        _activate_next(env.cfg)
        env.auto.return_value = True                    # the cron run of the weekly audit
        _run(env.cfg)
        assert [c for c in env.log if c[0] == "lan"] == []
        assert env.wol.call_count == 0
        assert env.log == [("cloud", "FY27-28"), ("cloud", "FY26-27")]

        env.auto.return_value = False                   # 'Check now' on the dashboard audits it
        _run(env.cfg)
        assert [c for c in env.log if c[0] == "lan"] == [("lan", "FY27-28"), ("lan", "FY26-27")]


def test_fy_argument_selects_one_fy_and_an_inactive_fy_is_refused(tmp_path):
    with _env(tmp_path) as env:
        _activate_next(env.cfg)
        _run(env.cfg, fy="FY27-28")
        assert [c for c in env.log if c[0] != "wol"] == [("cloud", "FY27-28"), ("lan", "FY27-28")]
        first = len(env.log)
        _run(env.cfg, fy="FY26-27")
        assert [c for c in env.log[first:] if c[0] != "wol"] == [("cloud", "FY26-27"), ("lan", "FY26-27")]
        before = len(env.log)
        with pytest.raises(ValueError):
            _run(env.cfg, fy="FY28-29")
        assert len(env.log) == before                    # refused before anything runs


# ── the safety check's reference: each FY's own last success ────────────

def test_pending_reference_of_each_pass_is_the_last_success_of_its_own_fy(tmp_path):
    from core import audit_pending
    with _env(tmp_path) as env:
        _activate_next(env.cfg)
        cfg = load_config(str(env.cfg))
        nxt_cfg = fy_overlap.config_for_fy(cfg, "FY27-28")
        flow._record_run(cfg, "run-old-cloud", "cloud", "2026-12-01T18:00:00+05:30", "CLOUD_COMPLETE", 0, None)
        flow._record_run(nxt_cfg, "run-new-cloud-FY27-28", "cloud", "2027-01-05T18:00:00+05:30",
                         "CLOUD_COMPLETE", 0, None)
        db = ManifestDB.from_config(cfg)
        try:
            configured = flow._pending_context(cfg, db, "cloud").reference
            nxt = flow._pending_context(nxt_cfg, db, "cloud", "FY27-28").reference
        finally:
            db.close()
        assert configured == audit_pending.reference_epoch("2026-12-01T18:00:00+05:30")
        assert nxt == audit_pending.reference_epoch("2027-01-05T18:00:00+05:30")


# ── no overlap record: the baseline audit ────────────────────────────────

def test_without_an_overlap_record_the_audit_is_the_baseline_audit(tmp_path):
    with _env(tmp_path) as env:
        env.audits.verdict[("cloud", "FY26-27")] = MISMATCH
        with pytest.raises(ExceptionGroup):
            _run(env.cfg)
        assert [c for c in env.log if c[0] != "wol"] == [("cloud", "FY26-27"), ("lan", "FY26-27")]
        assert all(i.endswith("-full") for i in _audit_ids(env.cfg))
        assert env.notify.call_args.args[1].key == "audit:all:cloud=VERIFICATION_FAILED"
        assert env.slot.call_count == 1
