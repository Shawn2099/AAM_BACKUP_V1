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
