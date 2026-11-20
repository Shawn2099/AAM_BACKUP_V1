"""OV-2.2: pure FY overlap decisions (core/fy_overlap.py)."""

from datetime import datetime

import pendulum
import pytest

from core import fy_overlap as ov
from core import rollover_journal as rj
from core.manifest import ManifestDB
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, RolloverConfig
from tests.test_config_cross_field import _quiet_legs

CFG_FY, NEXT = "FY26-27", "FY27-28"
NOW = "2027-01-01T02:01:00+05:30"


def _cfg(tmp_path, source="D:\\FY26-27", nas="\\\\nas\\share\\FY26-27", lan=True, **rollover):
    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}')
    return AppConfig(
        paths=PathsConfig(source_drive=source, lan_destination=nas, gcs_key_path=str(key)),
        lan=LanConfig(enabled=lan),
        rollover=RolloverConfig(**rollover),
        **_quiet_legs(cloud=CloudConfig(enabled=True, bucket="my-bucket", project_number="123")))


@pytest.fixture
def db(tmp_path):
    return ManifestDB(tmp_path / "m.db")


def _activate(db, fy=NEXT):
    db.record_activation(fy, activated_at=NOW, source_path=f"D:\\{fy}", created_by_app=True, clock_state="OK")


def _d(y, m, d):
    return pendulum.date(y, m, d)


def _journal(db, switch_done, frm=CFG_FY, to=NEXT):
    db.rollover_open(frm, to, NOW)
    if switch_done:
        db.rollover_step_done(frm, to, rj.CONFIG_SWITCH, NOW)


# ---- small helpers -------------------------------------------------------------------------

def test_next_fy_normal_and_wrap():
    assert ov.next_fy("FY26-27") == "FY27-28"
    assert ov.next_fy("FY99-00") == "FY00-01"
    assert ov.next_fy("fy26-27") == "FY27-28"


@pytest.mark.parametrize("bad", ["FY2026-27", "FY26-28", "26-27", "", "FY26-27x", "D:\\FY26-27"])
def test_next_fy_refuses_non_canonical(bad):
    with pytest.raises(ValueError):
        ov.next_fy(bad)


def test_run_date_is_the_ist_date_of_the_scheduled_time():
    late_utc = datetime(2026, 12, 31, 19, 0, tzinfo=pendulum.UTC)       # 00:30 IST on 1 Jan
    assert ov.run_date(late_utc) == _d(2027, 1, 1)
    assert ov.run_date(datetime(2027, 3, 31, 23, 59)) == _d(2027, 3, 31)   # naive = IST
    assert ov.run_date() == pendulum.now("Asia/Kolkata").date()


def test_calendar_fy():
    assert ov.calendar_fy(_d(2027, 3, 31)) == "FY26-27"
    assert ov.calendar_fy(_d(2027, 4, 1)) == "FY27-28"


# ---- the year, month by month --------------------------------------------------------------

@pytest.mark.parametrize("year, month, expect_active", [
    (2026, 4, False), (2026, 5, False), (2026, 6, False), (2026, 7, False), (2026, 8, False),
    (2026, 9, False), (2026, 10, False), (2026, 11, False), (2026, 12, False),
    (2027, 1, True), (2027, 2, True), (2027, 3, True),
])
def test_every_month_with_overlap_on_and_an_activation_row(tmp_path, db, year, month, expect_active):
    cfg = _cfg(tmp_path)
    _activate(db)
    d = _d(year, month, 15)
    assert ov.in_window(cfg, d) is expect_active
    assert ov.active_folders(cfg, db, d) == ([NEXT, CFG_FY] if expect_active else [CFG_FY])


def test_window_edges_1_jan_31_mar_1_apr(tmp_path, db):
    cfg = _cfg(tmp_path)
    _activate(db)
    assert not ov.in_window(cfg, _d(2026, 12, 31))
    assert ov.in_window(cfg, _d(2027, 1, 1))
    assert ov.in_window(cfg, _d(2027, 3, 31))
    assert not ov.in_window(cfg, _d(2027, 4, 1))                  # calendar FY moved on
    assert ov.active_folders(cfg, db, _d(2026, 12, 31)) == [CFG_FY]
    assert ov.active_folders(cfg, db, _d(2027, 1, 1)) == [NEXT, CFG_FY]
    assert ov.active_folders(cfg, db, _d(2027, 3, 31)) == [NEXT, CFG_FY]
    # 1 April, no journal: switch pending keeps both folders
    assert ov.active_folders(cfg, db, _d(2027, 4, 1)) == [NEXT, CFG_FY]


@pytest.mark.parametrize("start_month, jan, feb, mar", [
    (1, True, True, True), (2, False, True, True), (3, False, False, True)])
def test_early_start_month(tmp_path, db, start_month, jan, feb, mar):
    cfg = _cfg(tmp_path, early_start_month=start_month)
    _activate(db)
    assert [ov.in_window(cfg, _d(2027, m, 10)) for m in (1, 2, 3)] == [jan, feb, mar]
    assert not ov.in_window(cfg, _d(2026, 12, 10))


# ---- switch pending ------------------------------------------------------------------------

@pytest.mark.parametrize("day", [1, 2, 3, 4, 5])
def test_pending_for_five_days_while_config_switch_is_not_done(tmp_path, db, day):
    cfg = _cfg(tmp_path)
    _activate(db)
    d = _d(2027, 4, day)
    assert ov.switch_pending(cfg, db, d) is True                  # no journal yet
    _journal(db, switch_done=False)
    assert ov.switch_pending(cfg, db, d) is True                  # open journal, switch not done
    assert ov.active_folders(cfg, db, d) == [NEXT, CFG_FY]


def test_not_pending_once_config_switch_is_done_even_with_open_follow_ups(tmp_path, db):
    cfg = _cfg(tmp_path)
    _journal(db, switch_done=True)                                # archive / shutdown / announce still pending
    assert not rj.is_complete(db.rollover_get(CFG_FY, NEXT))
    assert ov.switch_pending(cfg, db, _d(2027, 4, 2)) is False


def test_after_the_switch_the_config_already_names_the_next_fy(tmp_path, db):
    cfg = _cfg(tmp_path, source="D:\\FY27-28", nas="\\\\nas\\share\\FY27-28")
    _activate(db)
    _journal(db, switch_done=True)
    d = _d(2027, 4, 3)
    assert ov.switch_pending(cfg, db, d) is False
    assert ov.in_window(cfg, d) is False                          # April is not early_start..March
    assert ov.active_folders(cfg, db, d) == ["FY27-28"]


def test_after_the_switch_next_january_starts_a_new_cycle(tmp_path, db):
    cfg = _cfg(tmp_path, source="D:\\FY27-28", nas="\\\\nas\\share\\FY27-28")
    _activate(db, "FY28-29")
    assert ov.active_folders(cfg, db, _d(2028, 1, 5)) == ["FY28-29", "FY27-28"]


def test_overlap_off(tmp_path, db):
    cfg = _cfg(tmp_path, overlap_enabled=False)
    _activate(db)
    for d in (_d(2027, 2, 1), _d(2027, 4, 2)):
        assert ov.in_window(cfg, d) is False
        assert ov.switch_pending(cfg, db, d) is False
        assert ov.active_folders(cfg, db, d) == [CFG_FY]


def test_clock_behind_is_never_pending_or_in_window(tmp_path, db):
    cfg = _cfg(tmp_path)
    _activate(db)
    d = _d(2026, 2, 10)                                           # calendar FY25-26, configured FY26-27
    assert ov.in_window(cfg, d) is False
    assert ov.switch_pending(cfg, db, d) is False
    assert ov.active_folders(cfg, db, d) == [CFG_FY]


def test_jump_two_fys_ahead_is_never_pending(tmp_path, db):
    cfg = _cfg(tmp_path)
    _activate(db)
    _activate(db, "FY28-29")
    d = _d(2028, 4, 5)                                            # calendar FY28-29
    assert ov.switch_pending(cfg, db, d) is False
    assert ov.in_window(cfg, d) is False
    assert ov.active_folders(cfg, db, d) == [CFG_FY]


def test_century_wrap(tmp_path, db):
    cfg = _cfg(tmp_path, source="D:\\FY99-00", nas="\\\\nas\\share\\FY99-00")
    _activate(db, "FY00-01")
    assert ov.active_folders(cfg, db, _d(2000, 2, 1)) == ["FY00-01", "FY99-00"]
    assert ov.switch_pending(cfg, db, _d(2000, 4, 2)) is True
    assert ov.folder_for(cfg, "FY00-01") == "D:\\FY00-01"
    assert ov.next_fy("FY99-00") == "FY00-01"


# ---- activation, order ---------------------------------------------------------------------

def test_without_an_activation_row_only_the_configured_fy(tmp_path, db):
    cfg = _cfg(tmp_path)
    assert ov.active_folders(cfg, db, _d(2027, 2, 1)) == [CFG_FY]
    assert ov.active_folders(cfg, db, _d(2027, 4, 2)) == [CFG_FY]


def test_new_fy_comes_first(tmp_path, db):
    cfg = _cfg(tmp_path)
    _activate(db)
    folders = ov.active_folders(cfg, db, _d(2027, 2, 1))
    assert folders[0] == NEXT and folders[1] == CFG_FY


def test_source_without_an_fy_leaf_falls_back_to_effective_prefix(tmp_path, db):
    cfg = _cfg(tmp_path)
    cfg.paths.source_drive = "D:\\DATA"                           # not reachable via the validator; direct set
    assert ov.active_folders(cfg, db, _d(2027, 2, 1)) == [ov.effective_fy_prefix("D:\\DATA")]


# ---- folder_view / folder_for / role_of -----------------------------------------------------

@pytest.mark.parametrize("source, nas, src_new, nas_new", [
    ("D:\\FY26-27", "\\\\nas\\share\\FY26-27", "D:\\FY27-28", "\\\\nas\\share\\FY27-28"),
    ("E:\\DATA\\FY26-27", "\\\\nas\\share\\FY26-27", "E:\\DATA\\FY27-28", "\\\\nas\\share\\FY27-28"),
    ("D:\\FY26-27", "\\\\nas\\share\\sub\\FY26-27", "D:\\FY27-28", "\\\\nas\\share\\sub\\FY27-28"),
    ("\\\\srv\\files\\FY26-27", "\\\\nas\\share\\FY26-27", "\\\\srv\\files\\FY27-28", "\\\\nas\\share\\FY27-28"),
])
def test_folder_view_paths(tmp_path, source, nas, src_new, nas_new):
    cfg = _cfg(tmp_path, source=source, nas=nas)
    view = ov.folder_view(cfg, NEXT)
    assert view.paths.source_drive == src_new
    assert view.paths.lan_destination == nas_new
    assert ov.folder_for(cfg, NEXT) == src_new
    assert ov.folder_for(cfg, CFG_FY) == source


def test_folder_view_of_the_configured_fy_is_the_config_itself(tmp_path):
    cfg = _cfg(tmp_path)
    assert ov.folder_view(cfg, CFG_FY) is cfg


def test_folder_view_does_not_mutate_the_original(tmp_path):
    cfg = _cfg(tmp_path)
    view = ov.folder_view(cfg, NEXT)
    assert view is not cfg and view.paths is not cfg.paths
    assert cfg.paths.source_drive == "D:\\FY26-27"
    assert cfg.paths.lan_destination == "\\\\nas\\share\\FY26-27"


def test_cloud_only_leaves_the_placeholder_untouched(tmp_path):
    cfg = _cfg(tmp_path, nas="\\\\nas\\share", lan=False)
    view = ov.folder_view(cfg, NEXT)
    assert view.paths.source_drive == "D:\\FY27-28"
    assert view.paths.lan_destination == "\\\\nas\\share"


def test_role_of(tmp_path):
    cfg = _cfg(tmp_path)
    assert ov.role_of(cfg, CFG_FY) == "current"
    assert ov.role_of(cfg, NEXT) == "early"
