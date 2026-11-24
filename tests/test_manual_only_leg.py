"""A6 - the per-leg `scheduled` switch (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, sections 5 and 7, task A6).

`enabled: false` still means off everywhere. `enabled: true, scheduled: false` = manual only: the leg keeps its
cron in the config but no schedule runs it. Part 1 (this file) covers the config model and validation; the
registration and behaviour of a manual-only leg are tasks C1-C2 (more tests in this file later).
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from core import config_edit, config_help
from core import statuses
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, ScheduleConfig, WolConfig

REPO = Path(__file__).resolve().parents[1]


def _config(cloud=True, lan=True, cloud_scheduled=True, lan_scheduled=True, schedule=None):
    return AppConfig(
        paths=PathsConfig(source_drive=r"E:\FY26-27", lan_destination=r"\\nas\backup\FY26-27",
                          gcs_key_path=r"C:\keys\aam-key.json"),
        lan=LanConfig(enabled=lan, scheduled=lan_scheduled),
        cloud=CloudConfig(enabled=cloud, scheduled=cloud_scheduled, bucket="aam-test-bucket",
                          project_number="123456789012"),
        wol=WolConfig(enabled=False),
        schedule=schedule or ScheduleConfig(),
    )


# --- defaults and the keys ----------------------------------------------------------------------------

def test_scheduled_defaults_to_true_for_both_legs():
    assert LanConfig().scheduled is True
    assert CloudConfig(bucket="aam-test-bucket", project_number="123456789012").scheduled is True


def test_a_config_without_the_keys_is_scheduled():
    raw = {"paths": {"source_drive": r"E:\FY26-27", "lan_destination": r"\\nas\backup\FY26-27",
                     "gcs_key_path": r"C:\keys\aam-key.json"},
           "cloud": {"bucket": "aam-test-bucket", "project_number": "123456789012"},
           "lan": {}, "wol": {"enabled": False}}
    cfg = AppConfig.model_validate(raw)
    assert cfg.cloud.scheduled is True and cfg.lan.scheduled is True


def test_scheduled_must_be_a_boolean():
    with pytest.raises(ValidationError):
        LanConfig(scheduled="sometimes")


# --- at least one enabled leg must be scheduled -------------------------------------------------------

def test_both_legs_manual_only_is_refused():
    with pytest.raises(ValidationError) as err:
        _config(cloud_scheduled=False, lan_scheduled=False)
    assert "scheduled" in str(err.value)


def test_the_only_enabled_leg_manual_only_is_refused():
    with pytest.raises(ValidationError):
        _config(cloud=True, lan=False, cloud_scheduled=False)


def test_one_scheduled_leg_is_enough_next_to_a_manual_one():
    cfg = _config(cloud=True, lan=True, cloud_scheduled=True, lan_scheduled=False)
    assert cfg.lan.scheduled is False and cfg.cloud.scheduled is True


def test_a_disabled_scheduled_leg_does_not_count():
    with pytest.raises(ValidationError):
        _config(cloud=False, lan=True, lan_scheduled=False)


# --- the budget validator skips an unscheduled leg ----------------------------------------------------

def test_schedule_that_only_fits_without_the_manual_leg_is_accepted():
    # both legs at the same start time collide when both are scheduled
    collide = ScheduleConfig(cloud_cron="0 2 * * *", lan_cron="0 2 * * *")
    with pytest.raises(ValidationError):
        _config(schedule=collide)
    cfg = _config(lan_scheduled=False, schedule=collide)
    assert cfg.lan.scheduled is False


# --- help, risky keys, shipped configs ----------------------------------------------------------------

@pytest.mark.parametrize("key", ["cloud.scheduled", "lan.scheduled"])
def test_help_and_risky_lists_know_the_switch(key):
    assert key in config_help.HELP
    assert config_help.HELP[key][0]
    assert key in config_edit._RISKY_KEYS


def test_example_config_writes_both_switches_explicitly():
    raw = yaml.safe_load((REPO / "config.example.yaml").read_text(encoding="utf-8"))
    assert raw["cloud"]["scheduled"] is True
    assert raw["lan"]["scheduled"] is True


def test_install_config_writes_both_switches_explicitly():
    raw = yaml.safe_load((REPO / "deploy" / "config.install.yaml").read_text(encoding="utf-8"))
    assert raw["cloud"]["scheduled"] is True
    assert raw["lan"]["scheduled"] is True


# --- the final-copy status set (defined with the A4 lookup; re-checked here as the A6 item) -----------

def test_final_copy_statuses_exclude_legacy_and_partial_values():
    assert statuses.FINAL_COPY_STATUSES == {"CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE", "LAN_COMPLETE"}
    assert not (statuses.FINAL_COPY_STATUSES & {"SUCCESS", "LAN_NO_CHANGES_COMPLETE",
                                                 "CLOUD_PARTIAL", "LAN_PARTIAL", "CLOUD_VERIFY_FAILED"})


# =====================================================================================================
# Part 2 - C1 Registration (docs/TESTMATRIX_C_2026-11-24.md, rows C1-01..C1-18) and C3 shipped configs
# =====================================================================================================

import importlib.util
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pendulum

from core import attention, overdue
from core.manifest import ManifestDB
from core.time_utils import cron_to_human
from test_support.sandbox_config import _config_dict

TZ = "Asia/Kolkata"
CLOUD_CRON_DEFAULT = ("0 18 * * *", TZ)
LAN_CRON_DEFAULT = ("0 1 * * *", TZ)


def _cfg_rt(tmp_path, cloud=True, lan=True, cloud_scheduled=True, lan_scheduled=True) -> AppConfig:
    """Like _config(), but the runtime dir (and so the schedule record DB) is in tmp_path."""
    (tmp_path / "rt").mkdir(parents=True, exist_ok=True)
    return AppConfig(
        paths=PathsConfig(source_drive=r"E:\FY26-27", lan_destination=r"\\nas\backup\FY26-27",
                          gcs_key_path=r"C:\keys\aam-key.json", runtime_dir=str(tmp_path / "rt")),
        lan=LanConfig(enabled=lan, scheduled=lan_scheduled),
        cloud=CloudConfig(enabled=cloud, scheduled=cloud_scheduled, bucket="aam-test-bucket",
                          project_number="123456789012"),
        wol=WolConfig(enabled=False),
    )


def _serve_deployments(tmp_path, monkeypatch, cloud=True, lan=True, cloud_scheduled=True, lan_scheduled=True):
    import serve

    d = _config_dict(tmp_path)
    d["cloud"].update({"enabled": cloud, "scheduled": cloud_scheduled})
    d["lan"].update({"enabled": lan, "scheduled": lan_scheduled})
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(d), encoding="utf-8")
    monkeypatch.setattr(serve, "CONFIG_PATH", str(path))
    deps = serve._deployments()
    return str(path), {x.name: x for x in deps}


def _crons(dep):
    return [(s.schedule.cron, s.schedule.timezone) for s in dep.schedules]


# --- C1 registration (layer S) ------------------------------------------------------------------------

def test_default_config_registers_both_crons(tmp_path, monkeypatch):
    path, deps = _serve_deployments(tmp_path, monkeypatch)
    assert list(deps) == ["backup-cloud", "backup-lan", "weekly-report", "monthly-report",
                          "rollover-check", "integrity-audit"]
    assert _crons(deps["backup-cloud"]) == [CLOUD_CRON_DEFAULT]
    assert _crons(deps["backup-lan"]) == [LAN_CRON_DEFAULT]
    assert deps["backup-cloud"].parameters == {"config_path": path, "mode": "cloud"}
    assert deps["backup-lan"].parameters == {"config_path": path, "mode": "lan"}
    assert deps["backup-cloud"].tags == ["production", "cloud"]
    assert deps["backup-lan"].tags == ["production", "lan"]


def test_manual_lan_registered_without_schedule_cloud_keeps_cron(tmp_path, monkeypatch):
    path, deps = _serve_deployments(tmp_path, monkeypatch, lan_scheduled=False)
    assert "backup-lan" in deps
    assert deps["backup-lan"].schedules == []
    assert deps["backup-lan"].parameters == {"config_path": path, "mode": "lan"}
    assert deps["backup-lan"].name == "backup-lan"
    assert deps["backup-lan"].tags == ["production", "lan"]
    assert _crons(deps["backup-cloud"]) == [CLOUD_CRON_DEFAULT]


def test_manual_cloud_registered_without_schedule(tmp_path, monkeypatch):
    path, deps = _serve_deployments(tmp_path, monkeypatch, cloud_scheduled=False)
    assert "backup-cloud" in deps
    assert deps["backup-cloud"].schedules == []
    assert deps["backup-cloud"].parameters == {"config_path": path, "mode": "cloud"}
    assert deps["backup-cloud"].name == "backup-cloud"
    assert deps["backup-cloud"].tags == ["production", "cloud"]
    assert _crons(deps["backup-lan"]) == [LAN_CRON_DEFAULT]


@pytest.mark.parametrize("lan_scheduled", [True, False])
def test_disabled_lan_not_registered_cloud_keeps_cron(tmp_path, monkeypatch, lan_scheduled):
    _path, deps = _serve_deployments(tmp_path, monkeypatch, lan=False, lan_scheduled=lan_scheduled)
    assert "backup-lan" not in deps
    assert _crons(deps["backup-cloud"]) == [CLOUD_CRON_DEFAULT]


def test_manual_leg_leaves_other_deployments_alone(tmp_path, monkeypatch):
    _p1, base = _serve_deployments(tmp_path / "a", monkeypatch)
    _p2, manual = _serve_deployments(tmp_path / "b", monkeypatch, lan_scheduled=False)
    for name in ("weekly-report", "monthly-report", "rollover-check", "integrity-audit"):
        assert _crons(manual[name]) == _crons(base[name])
        assert len(_crons(manual[name])) == 1


def test_manual_leg_description(tmp_path, monkeypatch):
    _p1, base = _serve_deployments(tmp_path / "a", monkeypatch)
    _p2, manual = _serve_deployments(tmp_path / "b", monkeypatch, lan_scheduled=False)
    assert manual["backup-lan"].description == "Manual-only lan backup - runs when started from the dashboard"
    assert manual["backup-cloud"].description == base["backup-cloud"].description


def test_serve_records_the_scheduled_flags(tmp_path):
    """F-C1-1: the record written by serve never carried the flags (every leg was stored as scheduled)."""
    import serve

    cfg = _cfg_rt(tmp_path, lan_scheduled=False)
    serve._record_schedule_in_effect(cfg)
    db = ManifestDB.from_config(cfg)
    try:
        rec = db.get_schedule_in_effect()
    finally:
        db.close()
    assert rec is not None
    assert rec["lan_scheduled"] is False
    assert rec["cloud_scheduled"] is True


# --- C1 record (layer M) ------------------------------------------------------------------------------

_BASE_SCHED = {"cloud_cron": "0 18 * * *", "lan_cron": "0 1 * * *", "timezone": TZ}


@pytest.fixture
def mdb(tmp_path):
    d = ManifestDB(tmp_path / "m.db")
    d.set_meta_created_at("2026-01-01T00:00:00+05:30")
    yield d
    d.close()


def test_flag_flip_stamps_since(mdb):
    mdb.set_schedule_in_effect({**_BASE_SCHED, "cloud_scheduled": True, "lan_scheduled": True})
    mdb.set_schedule_in_effect({**_BASE_SCHED, "cloud_scheduled": True, "lan_scheduled": False},
                               since="2027-01-10T09:00:00+05:30")
    rec = mdb.get_schedule_in_effect()
    assert rec["lan_since"] == "2027-01-10T09:00:00+05:30"
    assert rec["cloud_since"] is None
    assert rec["lan_scheduled"] is False


def test_flag_back_to_scheduled_stamps_since(mdb):
    mdb.set_schedule_in_effect({**_BASE_SCHED, "cloud_scheduled": True, "lan_scheduled": False})
    mdb.set_schedule_in_effect({**_BASE_SCHED, "cloud_scheduled": True, "lan_scheduled": True},
                               since="2027-01-10T09:00:00+05:30")
    rec = mdb.get_schedule_in_effect()
    assert rec["lan_since"] == "2027-01-10T09:00:00+05:30"
    assert rec["lan_scheduled"] is True


def test_same_flags_keep_since(mdb):
    mdb.set_schedule_in_effect({**_BASE_SCHED, "cloud_scheduled": True, "lan_scheduled": False})
    mdb.set_schedule_in_effect({**_BASE_SCHED, "cloud_scheduled": True, "lan_scheduled": False},
                               since="2027-01-10T09:00:00+05:30")
    rec = mdb.get_schedule_in_effect()
    assert rec["lan_since"] is None
    assert rec["cloud_since"] is None


def test_back_to_scheduled_is_counted_from_the_switch(tmp_path, mdb):
    pytest.importorskip("time_machine")
    cfg = _cfg_rt(tmp_path)
    mdb.insert_run({"run_id": "l-lan-old", "mode": "lan", "started_at": "2026-11-11T01:00:00+05:30",
                    "ended_at": "2026-11-11T01:30:00+05:30", "status": "LAN_COMPLETE", "exit_code": 0,
                    "bytes_copied": 0, "extended_metrics": None})
    mdb.insert_run({"run_id": "c-cloud-new", "mode": "cloud", "started_at": "2027-01-09T18:00:00+05:30",
                    "ended_at": "2027-01-09T18:30:00+05:30", "status": "CLOUD_COMPLETE", "exit_code": 0,
                    "bytes_copied": 0, "extended_metrics": None})
    mdb.set_schedule_in_effect({**_BASE_SCHED, "cloud_scheduled": True, "lan_scheduled": False})
    switched = pendulum.datetime(2027, 1, 10, 9, tz=TZ)
    mdb.set_schedule_in_effect({**_BASE_SCHED, "cloud_scheduled": True, "lan_scheduled": True},
                               since=switched.isoformat())
    due = pendulum.instance(overdue.deadline("0 1 * * *", TZ, switched, 4, 2))

    def lan_items(when):
        with pendulum.travel_to(when, freeze=True):
            return [i for i in attention.compute_attention(mdb, cfg, now=when) if i["code"] == "stale:lan"]

    assert lan_items(pendulum.datetime(2027, 1, 10, 12, tz=TZ)) == []
    late = lan_items(due.add(seconds=60))
    assert len(late) == 1 and late[0]["level"] == attention.RED


# --- C1 reconcile (layer R) ---------------------------------------------------------------------------

class _FakeClient:
    """Stands in for the Prefect client: records every pause/resume/delete call."""

    def __init__(self, paused=False, schedules=None):
        self.paused = paused
        self.schedules = schedules or {}      # deployment name -> list of schedule ids
        self.reads, self.calls = [], []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read_deployment_by_name(self, full_name):
        self.reads.append(full_name)
        short = full_name.split("/")[-1]
        dep = MagicMock()
        dep.id = f"id-{short}"
        dep.paused = self.paused
        dep.schedules = [SimpleNamespace(id=s) for s in self.schedules.get(short, [])]
        return dep

    async def resume_deployment(self, dep_id):
        self.calls.append(("resume", dep_id))

    async def pause_deployment(self, dep_id):
        self.calls.append(("pause", dep_id))

    async def delete_deployment_schedule(self, dep_id, sched_id):
        self.calls.append(("delete", dep_id, sched_id))


def _reconcile(cfg, client):
    from launch import _reconcile_disabled_legs

    with patch("prefect.client.orchestration.get_client", return_value=client):
        return _reconcile_disabled_legs(cfg)


def test_reconcile_resumes_manual_leg(tmp_path):
    client = _FakeClient(paused=True)
    res = _reconcile(_cfg_rt(tmp_path, lan_scheduled=False), client)
    assert res["backup-lan"] == "resumed (leg enabled)"
    assert ("resume", "id-backup-lan") in client.calls
    assert not [c for c in client.calls if c[0] == "pause"]


def test_reconcile_keeps_manual_leg_active(tmp_path):
    client = _FakeClient(paused=False)
    res = _reconcile(_cfg_rt(tmp_path, lan_scheduled=False), client)
    assert res["backup-lan"] == "active"
    assert [c for c in client.calls if c[1] == "id-backup-lan"] == []


def test_reconcile_treats_manual_as_enabled(tmp_path):
    client = _FakeClient(paused=True)
    res = _reconcile(_cfg_rt(tmp_path, lan_scheduled=False), client)
    assert res == {"backup-lan": "resumed (leg enabled)", "backup-cloud": "resumed (leg enabled)",
                   "integrity-audit": "resumed (leg enabled)"}
    assert not [c for c in client.calls if c[0] == "pause"]


def test_reconcile_deletes_cron_of_manual_leg(tmp_path):
    client = _FakeClient(paused=False, schedules={"backup-lan": ["s1", "s2"], "backup-cloud": ["c1"]})
    res = _reconcile(_cfg_rt(tmp_path, lan_scheduled=False), client)
    assert ("delete", "id-backup-lan", "s1") in client.calls
    assert ("delete", "id-backup-lan", "s2") in client.calls
    assert res["backup-lan"] == "cron removed (manual only)"
    assert not [c for c in client.calls if c[0] == "pause"]


def test_reconcile_keeps_cron_of_scheduled_leg(tmp_path):
    client = _FakeClient(paused=False, schedules={"backup-lan": ["s1"], "backup-cloud": ["c1"]})
    _reconcile(_cfg_rt(tmp_path, lan_scheduled=False), client)
    assert [c for c in client.calls if c[0] == "delete" and c[1] == "id-backup-cloud"] == []


# --- C3 shipped configs --------------------------------------------------------------------------------

_SHIPPED = ["config.example.yaml", "deploy/config.install.yaml"]

_BLOCK = [
    "# WHICH BACKUPS RUN, AND HOW - the same three choices for lan: and cloud: below",
    "#   enabled: false                    = this backup is OFF (never runs, no alerts about it)",
    "#   enabled: true,  scheduled: true   = runs by itself on its schedule (schedule: lan_cron / cloud_cron)",
    '#   enabled: true,  scheduled: false  = manual only: runs when you press "Run backup" on the dashboard',
    "# Cloud only = lan.enabled: false. NAS only = cloud.enabled: false. Both = both true.",
    "# At least one switched-on backup must be scheduled (the settings are refused otherwise).",
]


@pytest.mark.parametrize("rel", _SHIPPED)
def test_shipped_configs_write_enabled_and_scheduled(rel):
    raw = yaml.safe_load((REPO / rel).read_text(encoding="utf-8"))
    for leg in ("lan", "cloud"):
        assert "enabled" in raw[leg] and isinstance(raw[leg]["enabled"], bool)
        assert "scheduled" in raw[leg] and isinstance(raw[leg]["scheduled"], bool)


@pytest.mark.parametrize("rel", _SHIPPED)
def test_shipped_configs_explain_the_three_states(rel):
    text = (REPO / rel).read_text(encoding="utf-8")
    lan_at = re.search(r"^lan:", text, re.M).start()
    lines = [ln.rstrip() for ln in text.splitlines()]
    for needle in _BLOCK:
        assert needle in lines, needle
        assert text.index(needle) < lan_at, needle


def test_leg_plan_text(tmp_path, capsys):
    sched = ScheduleConfig()
    off = _cfg_rt(tmp_path, cloud=True, lan=False)
    manual = _cfg_rt(tmp_path, lan_scheduled=False)
    on = _cfg_rt(tmp_path)
    assert config_help.leg_plan(off, "lan") == "switched off"
    assert config_help.leg_plan(manual, "lan") == "runs only when you press Run backup"
    assert config_help.leg_plan(on, "lan") == (
        f"runs on its schedule ({cron_to_human(sched.lan_cron, sched.timezone)})")
    assert config_help.leg_plan(on, "cloud") == (
        f"runs on its schedule ({cron_to_human(sched.cloud_cron, sched.timezone)})")
    assert config_help.leg_plan(_cfg_rt(tmp_path, cloud=False, lan=True), "cloud") == "switched off"

    # the install check prints one plan line per leg
    spec = importlib.util.spec_from_file_location("aam_deploy_test_config", REPO / "deploy" / "test_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = _config_dict(tmp_path / "site")
    d["lan"]["scheduled"] = False
    cfg_file = tmp_path / "site.yaml"
    cfg_file.write_text(yaml.safe_dump(d), encoding="utf-8")
    capsys.readouterr()
    assert mod.main(str(cfg_file)) == 0
    out = capsys.readouterr().out
    nas = [ln for ln in out.splitlines() if "Office NAS copy: " in ln]
    online = [ln for ln in out.splitlines() if "Online copy: " in ln]
    assert len(nas) == 1 and len(online) == 1
    assert nas[0].endswith("Office NAS copy: runs only when you press Run backup")
    assert online[0].endswith(
        f"Online copy: runs on its schedule ({cron_to_human(sched.cloud_cron, sched.timezone)})")


# =====================================================================================================
# C2 Behaviour (docs/TESTMATRIX_C_2026-11-24.md rows C2-01 .. C2-22: banner, watchdog, emails)
# One test per row, named with the row ID. C2-05 (flow failure mail), C2-13, C2-15, C2-18, C2-21 are
# owned by other suites (flow tests, B8 R31-R33, B9 U13/J03); C2-23 is the regression run.
# =====================================================================================================

import json
from contextlib import contextmanager

import watchdog
from core import email_compose, faults
from core import notify as c2_nt
from core.attention import AMBER, RED, compute_attention, leg_timing

C2_CREATED = "2026-11-01T00:00:00+05:30"
C2_ACTIVATED = "2026-12-31T06:00:00+05:30"
C2_SCHED = {"cloud_cron": "0 18 * * *", "lan_cron": "0 1 * * *", "timezone": TZ}
C2_OVERDUE_LAN = re.compile(r"^(first_run|stale|catching_up):lan(:|$)")
C2_ANY_LAN = re.compile(r"(^|:)lan(:|$)")


def c2_at(*parts):
    return pendulum.datetime(*parts, tz=TZ)


C2_WEEK_LATER = c2_at(2026, 11, 8, 12)      # a week after the database was first known
C2_NEXT_FY_NOW = c2_at(2027, 1, 15, 12)     # 15 Jan 2027, overlap active since 31 Dec 2026
C2_MANUAL = {"cfg_lan": False, "rec_lan": False}


def _c2_cfg(tmp_path, *, cloud_scheduled=True, lan_scheduled=True, lan=True, cloud=True) -> AppConfig:
    (tmp_path / "rt").mkdir(parents=True, exist_ok=True)
    key = tmp_path / "keys" / "sandbox-key.json"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text('{"type": "service_account", "sandbox": true}', encoding="utf-8")
    return AppConfig(
        paths={"source_drive": r"E:\FY26-27", "lan_destination": r"\\nas\backup\FY26-27",
               "runtime_dir": str(tmp_path / "rt"), "gcs_key_path": str(key)},
        wol={"enabled": False},
        cloud={"enabled": cloud, "scheduled": cloud_scheduled, "bucket": "aam-test-bucket",
               "project_number": "0"},
        lan={"enabled": lan, "scheduled": lan_scheduled},
        dashboard={"public_url": "http://backup-server:8080"},
        notifications={"recipients": ["o@firm.example"]},
        alerts={"stale_repeat_hours": 6},
        rollover={"overlap_enabled": True},
    )


@contextmanager
def _c2_world(tmp_path, *, cfg_lan=True, rec_lan=True, record=True, overlap=False):
    """(config, db): `cfg_lan` = the config flag, `rec_lan` = the flag in the schedule-in-effect record
    (`record=False`: no record at all, the config fallback applies)."""
    cfg = _c2_cfg(tmp_path, lan_scheduled=cfg_lan)
    db = ManifestDB.from_config(cfg)
    try:
        db.set_meta_created_at(C2_CREATED)
        if record:
            db.set_schedule_in_effect({**C2_SCHED, "cloud_scheduled": True, "lan_scheduled": rec_lan})
        if overlap:
            db.set_overlap_record("FY27-28", {"activated_at": C2_ACTIVATED, "from_fy": "FY26-27",
                                              "source_folder": r"E:\FY27-28", "clock": {}})
        yield cfg, db
    finally:
        db.close()


def _c2_run(db, run_id, mode, status, started, metrics=None):
    db.insert_run({"run_id": run_id, "mode": mode, "started_at": started.isoformat(),
                   "ended_at": started.isoformat(), "status": status, "exit_code": 0, "bytes_copied": 0,
                   "extended_metrics": None if metrics is None else json.dumps(metrics)})


def _c2_found(db, cfg, when):
    pytest.importorskip("time_machine")
    with pendulum.travel_to(when, freeze=True):
        return attention.compute_attention(db, cfg, now=when)


def _c2_codes(found):
    return [i["code"] for i in found]


def _c2_item(found, code):
    hits = [i for i in found if i["code"] == code]
    assert len(hits) == 1, (code, _c2_codes(found))
    return hits[0]


def _c2_not_lan(found):
    return [i for i in found if not C2_ANY_LAN.search(i["code"])]


def _c2_pair(tmp_path, when, *, overlap=False, setup=None, **manual_flags):
    """(baseline found, manual found): the same database content, scheduled vs the given flags."""
    out = []
    for name, flags in (("base", {}), ("manual", manual_flags)):
        with _c2_world(tmp_path / name, overlap=overlap, **flags) as (cfg, db):
            if setup:
                setup(db)
            out.append(_c2_found(db, cfg, when))
    return out


def test_C2_01_manual_leg_has_no_overdue_items(tmp_path):
    base, man = _c2_pair(tmp_path, C2_WEEK_LATER, **C2_MANUAL)
    assert [c for c in _c2_codes(base) if C2_OVERDUE_LAN.match(c)], "the scheduled baseline must be overdue"
    assert not [c for c in _c2_codes(man) if C2_OVERDUE_LAN.match(c)]
    assert _c2_not_lan(man) == _c2_not_lan(base)


def test_C2_02_manual_leg_old_success_not_overdue(tmp_path):
    def setup(db):
        _c2_run(db, "x-lan", "lan", "LAN_COMPLETE", C2_WEEK_LATER.subtract(days=30))
    base, man = _c2_pair(tmp_path, C2_WEEK_LATER, setup=setup, **C2_MANUAL)
    assert "stale:lan" in _c2_codes(base)
    assert "stale:lan" not in _c2_codes(man)


def test_C2_03_manual_leg_next_fy_has_no_overdue_items(tmp_path):
    base, man = _c2_pair(tmp_path, C2_NEXT_FY_NOW, overlap=True, **C2_MANUAL)
    assert [c for c in _c2_codes(base) if C2_OVERDUE_LAN.match(c) and c.endswith(":FY27-28")]
    for kind in ("first_run", "stale", "catching_up"):
        assert f"{kind}:lan:FY27-28" not in _c2_codes(man)
        assert f"{kind}:lan" not in _c2_codes(man)


def test_C2_04_manual_leg_failed_run_is_red(tmp_path):
    with _c2_world(tmp_path / "failed", overlap=True, **C2_MANUAL) as (cfg, db):
        _c2_run(db, "x-lan", "lan", "LAN_FAILED", c2_at(2027, 1, 14, 1))
        _c2_run(db, "x-lan-FY27-28", "lan", "LAN_FAILED", c2_at(2027, 1, 14, 1))
        found = _c2_found(db, cfg, C2_NEXT_FY_NOW)
    assert _c2_item(found, "last_run:lan")["level"] == RED
    assert _c2_item(found, "last_run:lan:FY27-28")["level"] == RED
    with _c2_world(tmp_path / "missed", **C2_MANUAL) as (cfg, db):
        _c2_run(db, "x-lan", "lan", "LAN_MISSED", c2_at(2027, 1, 14, 1))
        found = _c2_found(db, cfg, C2_NEXT_FY_NOW)
    assert _c2_item(found, "last_run:lan")["level"] == AMBER


def test_C2_06_manual_leg_keeps_audit_shutdown_and_log_items(tmp_path):
    with _c2_world(tmp_path, **C2_MANUAL) as (cfg, db):
        _c2_run(db, "x-lan", "lan", "LAN_COMPLETE", c2_at(2026, 11, 8, 1),
                {"nas_shutdown": {"outcome": "UNVERIFIED", "detail": "no answer"}, "artefacts_degraded": True})
        db.record_audit({"audit_id": "a1", "mode": "lan", "started_at": c2_at(2026, 11, 8, 3).isoformat(),
                         "ended_at": c2_at(2026, 11, 8, 4).isoformat(), "status": "VERIFICATION_FAILED",
                         "detail": None})
        found = _c2_found(db, cfg, C2_WEEK_LATER)
    assert _c2_item(found, "audit:lan")["level"] == RED
    assert _c2_item(found, "nas_shutdown")["level"] == AMBER
    assert _c2_item(found, "run_logs:lan")["level"] == AMBER


def _c2_ns_cfg(lan_flag="absent", with_schedule=True):
    """A SimpleNamespace config as tests/test_fy_overlap_attention.py T03 builds it (no flags unless given)."""
    from test_support import attention_cfg

    lan = SimpleNamespace(enabled=True) if lan_flag == "absent" else SimpleNamespace(enabled=True, scheduled=lan_flag)
    extra = {"schedule": attention_cfg.SCHEDULE} if with_schedule else {}
    return SimpleNamespace(
        cloud=SimpleNamespace(enabled=True), lan=lan,
        dashboard=SimpleNamespace(stale_grace_hours=attention_cfg.GRACE_HOURS, alert_failure_window_days=7,
                                  catching_up_max_days=14, public_url="http://backup-server:8080", port=8080,
                                  bind_address="127.0.0.1"),
        firm_name="T", notifications=SimpleNamespace(send_on_failure=True),
        budget=attention_cfg.BUDGET, **extra)


def _c2_mock_db():
    db = MagicMock()
    db.last_run.return_value = None
    db.last_successful_run.return_value = None
    db.latest_audit.return_value = None
    db.open_approvals.return_value = []
    db.latest_notification_for_mode.return_value = None
    db.notification_failures_since.return_value = []
    db.last_config_change_at.return_value = None
    db.first_known_at.return_value = C2_CREATED
    db.last_sent_at.return_value = None
    db.overlap_record.return_value = None
    db.get_schedule_in_effect.return_value = None
    return db


def test_C2_07_manual_leg_no_schedule_fallback_item():
    found = _c2_found(_c2_mock_db(), _c2_ns_cfg(lan_flag=False, with_schedule=False), C2_WEEK_LATER)
    assert _c2_item(found, "stale:cloud")["level"] == RED
    assert "cannot be read" in _c2_item(found, "stale:cloud")["message"]
    assert "stale:lan" not in _c2_codes(found)


def test_C2_08_watchdog_never_mails_manual_leg(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(c2_nt, "notify", lambda cfg, event, db=None: sent.append(event))
    with _c2_world(tmp_path, **C2_MANUAL) as (cfg, db):
        _c2_run(db, "x-cloud", "cloud", "CLOUD_COMPLETE", c2_at(2026, 11, 2, 18))
        pytest.importorskip("time_machine")
        with pendulum.travel_to(C2_WEEK_LATER, freeze=True):
            assert watchdog._check_stale_legs(cfg) == ["cloud"]
    assert [e.key for e in sent] == ["stale:cloud"]
    assert not [e for e in sent if e.mode == "lan"]


def test_C2_09_watchdog_manual_leg_both_fys_silent(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(c2_nt, "notify", lambda cfg, event, db=None: sent.append(event))
    with _c2_world(tmp_path, overlap=True, **C2_MANUAL) as (cfg, db):
        _c2_run(db, "x-cloud", "cloud", "CLOUD_COMPLETE", c2_at(2027, 1, 14, 18))
        _c2_run(db, "x-cloud-FY27-28", "cloud", "CLOUD_COMPLETE", c2_at(2027, 1, 14, 18))
        pytest.importorskip("time_machine")
        with pendulum.travel_to(C2_NEXT_FY_NOW, freeze=True):
            assert watchdog._check_stale_legs(cfg) == []
    assert sent == []


def test_C2_10_record_in_effect_decides_scheduled(tmp_path):
    def old_success(db):
        _c2_run(db, "x-lan", "lan", "LAN_COMPLETE", C2_WEEK_LATER.subtract(days=30))
    with _c2_world(tmp_path / "a", cfg_lan=False, rec_lan=True) as (cfg, db):     # saved, not applied yet
        old_success(db)
        found = _c2_found(db, cfg, C2_WEEK_LATER)
    assert _c2_item(found, "stale:lan")["level"] == RED
    with _c2_world(tmp_path / "b", cfg_lan=True, rec_lan=False) as (cfg, db):
        old_success(db)
        found = _c2_found(db, cfg, C2_WEEK_LATER)
    assert "stale:lan" not in _c2_codes(found)


def test_C2_11_config_fallback_carries_flags(tmp_path):
    with _c2_world(tmp_path / "base", record=False) as (cfg, db):
        base = _c2_found(db, cfg, C2_WEEK_LATER)
    with _c2_world(tmp_path / "man", record=False, cfg_lan=False) as (cfg, db):
        assert db.get_schedule_in_effect() is None
        man = _c2_found(db, cfg, C2_WEEK_LATER)
    assert [c for c in _c2_codes(base) if C2_OVERDUE_LAN.match(c)]
    assert not [c for c in _c2_codes(man) if C2_OVERDUE_LAN.match(c)]
    assert _c2_not_lan(man) == _c2_not_lan(base)


def test_C2_12_missing_flag_is_scheduled():
    absent = _c2_found(_c2_mock_db(), _c2_ns_cfg("absent"), C2_WEEK_LATER)
    explicit = _c2_found(_c2_mock_db(), _c2_ns_cfg(True), C2_WEEK_LATER)
    assert absent == explicit
    assert [c for c in _c2_codes(absent) if C2_OVERDUE_LAN.match(c)]


def test_C2_14_scheduled_default_is_baseline(tmp_path, monkeypatch):
    sent: dict[str, list] = {}
    results = {}
    when = C2_WEEK_LATER
    for name in ("explicit", "old_record", "no_record"):
        sent[name] = []
        monkeypatch.setattr(c2_nt, "notify", lambda cfg, event, db=None, _n=name: sent[_n].append(event.key))
        with _c2_world(tmp_path / name, record=(name == "explicit")) as (cfg, db):
            if name == "old_record":
                db._set_meta("schedule_in_effect", json.dumps(C2_SCHED))     # a record from before the flags
            _c2_run(db, "x-lan", "lan", "LAN_COMPLETE", when.subtract(days=7))
            _c2_run(db, "x-cloud", "cloud", "CLOUD_COMPLETE", when.subtract(hours=15))
            found = _c2_found(db, cfg, when)
            with pendulum.travel_to(when, freeze=True):
                timing = {leg: leg_timing(db, cfg, leg, when) for leg in ("cloud", "lan")}
                stale = watchdog._check_stale_legs(cfg)
            results[name] = (found, timing, stale)
    assert "stale:lan" in _c2_codes(results["explicit"][0])
    assert results["explicit"] == results["old_record"] == results["no_record"]
    assert sent["explicit"] == sent["old_record"] == sent["no_record"] == ["stale:lan"]


# --- emails ------------------------------------------------------------------------------------------

def _c2_event(mode, status="LAN_FAILED", kind="FAILURE"):
    return c2_nt.AlertEvent(key=f"backup:{mode}:{status}", kind=kind, mode=mode, title="t", message="m",
                            status=status)


C2_MANUAL_TEXT = ("This {run} has no schedule: it runs only when someone presses Run backup on the "
                  "dashboard. You get one email when it works again.")
C2_NOW_MAIL = c2_at(2026, 11, 8, 9)


def test_C2_19_next_text_manual_leg(tmp_path):
    man = _c2_cfg(tmp_path / "m", lan_scheduled=False)
    man_cloud = _c2_cfg(tmp_path / "mc", cloud_scheduled=False)
    sched = _c2_cfg(tmp_path / "s")
    now = C2_NOW_MAIL
    # a manual leg: the fixed text
    assert email_compose._next_text(man, _c2_event("lan"), None, now) == C2_MANUAL_TEXT.format(run="office NAS backup")
    assert email_compose._next_text(man_cloud, _c2_event("cloud", "CLOUD_FAILED"), None, now) == \
        C2_MANUAL_TEXT.format(run="online backup")
    # a scheduled leg: today's text (also the cloud leg next to a manual LAN leg)
    nxt = email_compose._next_run(sched, "lan_cron", now)
    assert nxt
    assert email_compose._next_text(sched, _c2_event("lan"), None, now) == (
        f"The next office NAS backup runs automatically on {nxt}. You get one email when it works again.")
    assert email_compose._next_text(man, _c2_event("cloud", "CLOUD_FAILED"), None, now) == (
        "The next online backup runs automatically on "
        f"{email_compose._next_run(man, 'cloud_cron', now)}. You get one email when it works again.")
    # mode audit unchanged, even next to a manual LAN leg
    audit = email_compose._next_text(man, _c2_event("audit", "VERIFICATION_FAILED"), None, now)
    assert audit.startswith("The next weekly safety check runs automatically on ")
    assert audit == email_compose._next_text(sched, _c2_event("audit", "VERIFICATION_FAILED"), None, now)
    # a _BLOCKED action event: today's text
    action = SimpleNamespace(severity=faults.ACTION)
    blocked = email_compose._next_text(man, _c2_event("lan", "LAN_BLOCKED", "ACTION_REQUIRED"), action, now)
    assert blocked == email_compose._next_text(sched, _c2_event("lan", "LAN_BLOCKED", "ACTION_REQUIRED"),
                                               action, now)
    assert blocked.startswith("Until you decide, each office NAS backup stops at this point")


def test_C2_20_data_safety_names_manual_leg(tmp_path):
    def lines_for(path, **flags):
        cfg = _c2_cfg(path, **flags)
        db = ManifestDB.from_config(cfg)
        try:
            _c2_run(db, "x-lan", "lan", "LAN_COMPLETE", C2_NOW_MAIL.subtract(hours=8))
            _c2_run(db, "x-cloud", "cloud", "CLOUD_COMPLETE", C2_NOW_MAIL.subtract(hours=15))
            return email_compose._data_safety(cfg, db, C2_NOW_MAIL)
        finally:
            db.close()

    base = lines_for(tmp_path / "base")
    man = lines_for(tmp_path / "man", lan_scheduled=False)
    lan_base = [ln for ln in base if ln.startswith("Office NAS backup")]
    lan_man = [ln for ln in man if ln.startswith("Office NAS backup")]
    assert len(lan_base) == len(lan_man) == 1 and "last good copy" in lan_man[0]
    assert lan_man[0].endswith(" Runs only by hand (no schedule).")
    assert lan_man[0] == lan_base[0] + " Runs only by hand (no schedule)."
    cloud_man = [ln for ln in man if ln.startswith("Online backup")]
    assert cloud_man == [ln for ln in base if ln.startswith("Online backup")]
    assert "Runs only by hand" not in cloud_man[0]
