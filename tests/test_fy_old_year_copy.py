"""A financial year is CLOSED (tracking stopped) only once every enabled leg has fully copied it
(owner decision 2026-11-23, finding FV-1; plan docs/PLAN_FY_OVERLAP_2026-11-20.md section 5.9).

The facts are derived from run_history (a run of that leg and FY, status in SUCCESS_STATUSES, started AFTER
the year ended), so a crash or restart loses nothing; only years named by a finished rollover journal can be
kept. The normal rollover (both final backups succeed) closes the year at the switch exactly as before."""

import pendulum
import pytest

from core import fy_overlap as ov
from core import manual_leg
from core import rollover_journal as rj
from core.manifest import ManifestDB
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, RolloverConfig
from tests.test_config_cross_field import _quiet_legs

OLD, NEW = "FY26-27", "FY27-28"
JOURNAL_AT = "2027-04-01T06:00:00+05:30"
AFTER_END = "2027-04-01T06:00:10+05:30"
BEFORE_END = "2027-03-31T21:00:00+05:30"
D = pendulum.date(2027, 4, 3)


def _cfg(tmp_path, lan=True, cloud=True, lan_scheduled=True, cloud_scheduled=True):
    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}')
    return AppConfig(
        paths=PathsConfig(source_drive=f"D:\\{NEW}", lan_destination=f"\\\\nas\\share\\{NEW}",
                          gcs_key_path=str(key)),
        lan=LanConfig(enabled=lan, scheduled=lan_scheduled),
        rollover=RolloverConfig(),
        **_quiet_legs(cloud=CloudConfig(enabled=cloud, bucket="my-bucket", project_number="123",
                                        scheduled=cloud_scheduled)))


@pytest.fixture
def db(tmp_path):
    return ManifestDB(tmp_path / "m.db")


def _switched(db, frm=OLD, to=NEW):
    db.rollover_open(frm, to, JOURNAL_AT)
    db.rollover_step_done(frm, to, rj.CONFIG_SWITCH, JOURNAL_AT)


_n = [0]


def _run(db, leg, status, started=AFTER_END, fy=OLD):
    _n[0] += 1
    db.insert_run({"fy": fy, "run_id": f"r{_n[0]}", "mode": leg, "started_at": started,
                   "ended_at": started, "status": status})


def test_a_switched_year_with_no_confirmed_copy_stays_active_after_the_new_year(tmp_path, db):
    cfg = _cfg(tmp_path)
    _switched(db)
    assert ov.active_folders(cfg, db, D) == [NEW, OLD]
    assert ov.role_of(cfg, OLD) == ov.OLD and ov.role_of(cfg, NEW) == ov.CURRENT
    [state] = ov.unfinished_old_years(cfg, db)
    assert state["fy"] == OLD and state["missing"] == ["cloud", "lan"]


@pytest.mark.parametrize("leg,status", [
    ("lan", "LAN_FAILED"), ("lan", "LAN_PARTIAL"), ("lan", "LAN_BLOCKED"), ("lan", "LAN_SLOT_TIMEOUT"),
    ("lan", "LAN_RUNNING"), ("lan", "LAN_SUSPECT"), ("cloud", "CLOUD_PARTIAL"), ("cloud", "CLOUD_FAILED"),
    ("cloud", "CLOUD_VERIFY_FAILED"), ("cloud", "CLOUD_BLOCKED")])
def test_a_failed_partial_or_blocked_sync_never_counts(tmp_path, db, leg, status):
    cfg = _cfg(tmp_path, cloud=leg == "cloud", lan=leg == "lan")
    _switched(db)
    _run(db, leg, status)
    assert ov.active_folders(cfg, db, D) == [NEW, OLD]


def test_a_success_that_started_before_the_year_ended_does_not_count(tmp_path, db):
    cfg = _cfg(tmp_path)
    _switched(db)
    _run(db, "cloud", "CLOUD_COMPLETE", BEFORE_END)
    _run(db, "lan", "LAN_COMPLETE", BEFORE_END)
    assert ov.active_folders(cfg, db, D) == [NEW, OLD]


def test_a_success_of_another_year_does_not_count(tmp_path, db):
    cfg = _cfg(tmp_path)
    _switched(db)
    _run(db, "cloud", "CLOUD_COMPLETE", fy=NEW)
    _run(db, "lan", "LAN_COMPLETE", fy=NEW)
    assert ov.active_folders(cfg, db, D) == [NEW, OLD]


def test_the_year_closes_only_when_every_enabled_leg_has_a_confirmed_success(tmp_path, db):
    cfg = _cfg(tmp_path)
    _switched(db)
    _run(db, "cloud", "CLOUD_NO_CHANGES_COMPLETE")
    [state] = ov.unfinished_old_years(cfg, db)
    assert state["done"] == ["cloud"] and state["missing"] == ["lan"]
    assert ov.active_folders(cfg, db, D) == [NEW, OLD]
    _run(db, "lan", "LAN_COMPLETE")
    assert ov.unfinished_old_years(cfg, db) == []
    assert ov.active_folders(cfg, db, D) == [NEW]


def test_a_leg_that_is_off_is_not_required(tmp_path, db):
    cfg = _cfg(tmp_path, lan=False)
    _switched(db)
    assert ov.active_folders(cfg, db, D) == [NEW, OLD]
    _run(db, "cloud", "CLOUD_COMPLETE")
    assert ov.active_folders(cfg, db, D) == [NEW]


def test_a_manual_only_leg_is_required(tmp_path, db):
    cfg = _cfg(tmp_path, lan_scheduled=False)
    _switched(db)
    _run(db, "cloud", "CLOUD_COMPLETE")
    assert manual_leg.is_manual_only(cfg, "lan")
    assert ov.active_folders(cfg, db, D) == [NEW, OLD]
    advice = ov.not_fully_copied_advice(cfg, OLD, ["lan"])
    assert "start a LAN backup from the dashboard" in advice and "NAS" in advice


def test_nothing_is_kept_without_a_finished_switch_or_for_an_arbitrary_year(tmp_path, db):
    cfg = _cfg(tmp_path)
    assert ov.active_folders(cfg, db, D) == [NEW]                      # no journal at all
    db.rollover_open(OLD, NEW, JOURNAL_AT)                              # the switch step not finished
    assert ov.active_folders(cfg, db, D) == [NEW]
    _run(db, "lan", "LAN_COMPLETE", fy="FY20-21")
    assert ov.active_folders(cfg, db, D) == [NEW]


def test_a_year_the_settings_were_put_back_to_is_current_not_old(tmp_path, db):
    cfg = _cfg(tmp_path)
    cfg.paths.source_drive = f"D:\\{OLD}"
    cfg.paths.lan_destination = f"\\\\nas\\share\\{OLD}"
    _switched(db)
    assert OLD not in [o["fy"] for o in ov.unfinished_old_years(cfg, db)]


def test_a_settled_year_is_never_reopened_by_a_leg_switched_on_later(tmp_path, db):
    only_cloud = _cfg(tmp_path, lan=False)
    _switched(db)
    _run(db, "cloud", "CLOUD_COMPLETE")
    assert ov.active_folders(only_cloud, db, D) == [NEW]
    ov.mark_year_closed(db, OLD, AFTER_END)                              # what the daily check writes
    with_lan = _cfg(tmp_path)
    assert ov.active_folders(with_lan, db, D) == [NEW]


def test_the_state_is_derived_from_the_database_so_a_restart_loses_nothing(tmp_path):
    path = tmp_path / "durable.db"
    first = ManifestDB(path)
    cfg = _cfg(tmp_path)
    _switched(first)
    _run(first, "cloud", "CLOUD_COMPLETE")
    first.close()
    again = ManifestDB(path)                                             # a new process after a crash
    assert ov.active_folders(cfg, again, D) == [NEW, OLD]
    assert ov.unfinished_old_years(cfg, again)[0]["missing"] == ["lan"]
    _run(again, "lan", "LAN_COMPLETE")
    again.close()
    third = ManifestDB(path)
    assert ov.active_folders(cfg, third, D) == [NEW]


def test_old_years_show_in_the_report_notes_and_the_attention_banner(tmp_path, db):
    from core.attention import compute_attention

    cfg = _cfg(tmp_path, lan_scheduled=False)
    _switched(db)
    _run(db, "cloud", "CLOUD_COMPLETE")
    [line] = manual_leg.old_year_lines(cfg, db)
    assert line["soft_warn"] and f"{OLD} is not yet fully copied to LAN" in line["text"]
    assert any(ln is line or ln["text"] == line["text"] for ln in manual_leg.mode_lines(cfg, db))
    items = compute_attention(db, cfg, now=pendulum.datetime(2027, 4, 3, 9, tz="Asia/Kolkata"))
    old = [i for i in items if i["code"] == f"old_year:{OLD}"]
    assert len(old) == 1 and old[0]["level"] != "red"
    assert f"{OLD} is not yet fully copied to LAN" in old[0]["message"]
    _run(db, "lan", "LAN_COMPLETE")
    items = compute_attention(db, cfg, now=pendulum.datetime(2027, 4, 3, 9, tz="Asia/Kolkata"))
    assert not [i for i in items if i["code"].startswith("old_year:")]
    assert manual_leg.old_year_lines(cfg, db) == []
