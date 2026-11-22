"""Break tests for the FY overlap (core/fy_overlap.py): real-world calendar, folder, clock and database
scenarios. Every scenario checks the same three things: nothing the office owns is touched or deleted
(no data loss), a folder that cannot be set up is deferred with an alert (fail closed), and a second
run changes nothing (idempotence). Temp dirs only, no network, no real config."""

import os
import subprocess
from pathlib import Path

import pendulum
import pytest
from pydantic import ValidationError

from core import fy_overlap as ov
from core.fy_rollover import effective_fy_prefix
from core.health import CLOCK_SKEWED
from core.manifest import ManifestDB
from models.config import RolloverConfig
from tests.test_ov2_activation import Clock, Mail, _base_cfg
from tests.test_ov2_fy_overlap import _cfg as _unc_cfg

CFG_FY, NEXT = "FY26-27", "FY27-28"
NOW = "2027-01-01T02:01:00+05:30"
RUN_DATE = pendulum.date(2027, 1, 1)
T0 = pendulum.datetime(2027, 1, 1, 2, 1, tz="Asia/Kolkata")
ALERT_KEY = f"overlap:activation:{NEXT}"


def _d(y, m, d):
    return pendulum.date(y, m, d)


@pytest.fixture
def env(tmp_path):
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / CFG_FY).mkdir()
    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    cfg = _base_cfg(tmp_path, source=str(src_root / CFG_FY), nas=str(nas_root / CFG_FY))
    db = ManifestDB(tmp_path / "m.db")
    env_obj = _Env(cfg=cfg, db=db, src=src_root, nas=nas_root, tmp=tmp_path)
    yield env_obj
    env_obj.db.close()


class _Env:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def frozen():
    def go(dt):
        pendulum.travel_to(dt, freeze=True)
    yield go
    pendulum.travel_back()


def _same_fy_cfg(tmp_path, src_root, fy):
    """Config whose source and NAS folders both end in `fy` (the validator refuses a mismatch)."""
    cfg = _unc_cfg(tmp_path, source=str(src_root / fy), nas="\\\\nas\\share\\" + fy)
    object.__setattr__(cfg.paths, "lan_destination", str(tmp_path / "nas" / fy))
    return cfg


def _activate(db, fy=NEXT):
    db.record_activation(fy, activated_at=NOW, source_path=f"D:\\{fy}", created_by_app=True, clock_state="OK")


def _listing(folder: Path) -> list[str]:
    return sorted(os.listdir(folder))


def _put_file(folder: Path, name: str = "client.xlsx", text: str = "precious") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / name
    f.write_text(text, encoding="utf-8")
    return f


def _make_junction(link: Path, target: Path) -> bool:
    if os.name != "nt":
        return False
    target.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True)
    return proc.returncode == 0


# ---- calendar edges --------------------------------------------------------------------------

def test_dec31_2359_is_still_dec31_and_changes_nothing(env):
    """A run at 23:59 on 31 Dec is a December run: no early folder, nothing created, nothing moved."""
    scheduled = pendulum.datetime(2026, 12, 31, 23, 59, tz="Asia/Kolkata")
    d = ov.run_date(scheduled)
    assert d == _d(2026, 12, 31)
    res = ov.ensure_overlap_activated(env.cfg, d, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "not_needed"
    assert not (env.src / NEXT).exists()
    assert ov.active_folders(env.cfg, env.db, d) == [CFG_FY]


def test_jan1_0001_starts_the_next_folder_exactly_once(env):
    """00:01 on 1 Jan is the first in-window run: the next folder is created, a second run is a no-op."""
    d = ov.run_date(pendulum.datetime(2027, 1, 1, 0, 1, tz="Asia/Kolkata"))
    assert d == _d(2027, 1, 1)
    mail = Mail()
    first = ov.ensure_overlap_activated(env.cfg, d, env.db, clock_check=Clock(), notify_fn=mail)
    second = ov.ensure_overlap_activated(env.cfg, d, env.db, clock_check=Clock(), notify_fn=mail)
    assert first["state"] == "activated" and second["state"] == "already_active"
    assert (env.src / NEXT).is_dir()
    assert len(mail.events) == 1
    assert ov.active_folders(env.cfg, env.db, d) == [NEXT, CFG_FY]


def test_mar31_2359_and_apr1_0001_keep_both_folders(env, frozen):
    """The last window night and the first switch-pending night both back up both folders."""
    last_night = ov.run_date(pendulum.datetime(2027, 3, 31, 23, 59, tz="Asia/Kolkata"))
    assert last_night == _d(2027, 3, 31)
    res = ov.ensure_overlap_activated(env.cfg, last_night, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "activated"
    frozen(pendulum.datetime(2027, 4, 1, 0, 1, tz="Asia/Kolkata"))
    apr1 = ov.run_date()
    assert apr1 == _d(2027, 4, 1)
    assert ov.active_folders(env.cfg, env.db, last_night) == [NEXT, CFG_FY]
    assert ov.active_folders(env.cfg, env.db, apr1) == [NEXT, CFG_FY]       # switch pending, no journal
    again = ov.ensure_overlap_activated(env.cfg, apr1, env.db, clock_check=Clock(), notify_fn=Mail())
    assert again["state"] == "already_active"


def test_leap_day_2028_is_in_window_and_activates(tmp_path):
    """29 Feb in a leap year: the window and the calendar FY both hold, and the early folder is created."""
    src_root = tmp_path / "src"
    (src_root / "FY27-28").mkdir(parents=True)
    cfg = _same_fy_cfg(tmp_path, src_root, "FY27-28")
    db = ManifestDB(tmp_path / "m.db")
    try:
        leap = _d(2028, 2, 29)
        assert ov.calendar_fy(leap) == "FY27-28"
        assert ov.in_window(cfg, leap) is True
        res = ov.ensure_overlap_activated(cfg, leap, db, clock_check=Clock(), notify_fn=Mail())
        assert res["state"] == "activated" and res["fy"] == "FY28-29"
        assert (src_root / "FY28-29").is_dir()
        assert ov.active_folders(cfg, db, leap) == ["FY28-29", "FY27-28"]
    finally:
        db.close()


def test_scheduled_run_late_across_midnight_keeps_its_scheduled_day(env, frozen):
    """A 23:50 run on 31 Mar that only starts at 00:20 on 1 Apr is still a 31 Mar run (the scheduled day),
    so the window applies and the early folder is started; the same run after midnight is still a switch run."""
    scheduled = pendulum.datetime(2027, 3, 31, 23, 50, tz="Asia/Kolkata")
    frozen(pendulum.datetime(2027, 4, 1, 0, 20, tz="Asia/Kolkata"))
    d = ov.run_date(scheduled)
    assert d == _d(2027, 3, 31)
    res = ov.ensure_overlap_activated(env.cfg, d, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "activated"
    assert ov.active_folders(env.cfg, env.db, d) == [NEXT, CFG_FY]


def test_century_wrap_fy99_00_activates_fy00_01(tmp_path):
    """FY99-00 -> FY00-01: the next folder is named with the modulo-100 year and created beside the configured one."""
    src_root = tmp_path / "src"
    (src_root / "FY99-00").mkdir(parents=True)
    cfg = _same_fy_cfg(tmp_path, src_root, "FY99-00")
    db = ManifestDB(tmp_path / "m.db")
    try:
        d = _d(2000, 2, 1)
        assert ov.calendar_fy(d) == "FY99-00"
        res = ov.ensure_overlap_activated(cfg, d, db, clock_check=Clock(), notify_fn=Mail())
        assert res["state"] == "activated" and res["fy"] == "FY00-01"
        assert (src_root / "FY00-01").is_dir()
        assert ov.active_folders(cfg, db, d) == ["FY00-01", "FY99-00"]
    finally:
        db.close()


# ---- clock ------------------------------------------------------------------------------------

def test_clock_jumps_back_into_december_keeps_activation_and_folders(env):
    """The server clock jumps back to December after the early folder was started: that run backs up only the
    configured year, the activation row and every file in the early folder survive."""
    _activate(env.db)
    _put_file(env.src / NEXT)
    d = _d(2026, 12, 10)
    res = ov.ensure_overlap_activated(env.cfg, d, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "not_needed"
    assert ov.active_folders(env.cfg, env.db, d) == [CFG_FY]
    assert env.db.get_activation(NEXT) is not None
    assert (env.src / NEXT / "client.xlsx").read_text(encoding="utf-8") == "precious"


def test_clock_behind_the_configured_fy_is_never_pending_or_activated(env):
    """A clock earlier than the configured FY (calendar FY25-26 while FY26-27 is configured) never rolls anything."""
    d = _d(2026, 2, 10)
    assert ov.in_window(env.cfg, d) is False
    assert ov.switch_pending(env.cfg, env.db, d) is False
    res = ov.ensure_overlap_activated(env.cfg, d, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "not_needed"
    assert not (env.src / NEXT).exists()


def test_skewed_clock_defers_then_alerts_once_after_the_hours(env, frozen):
    """Clock not proven (skewed) at activation: nothing created, one action mail after 24 h, not a second one."""
    mail = Mail()
    skewed = Clock(state=CLOCK_SKEWED, reason="server is 9 minutes off")
    frozen(T0)
    res1 = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=skewed, notify_fn=mail)
    assert res1["state"] == "deferred" and "could not be proven correct" in res1["reason"]
    assert mail.events == [] and not (env.src / NEXT).exists()
    frozen(T0.add(hours=25))
    res2 = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=skewed, notify_fn=mail)
    assert res2["state"] == "deferred"
    assert mail.keys() == [ALERT_KEY] and mail.events[0].kind == "ACTION_REQUIRED"
    frozen(T0.add(hours=26))
    ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=skewed, notify_fn=mail)
    assert mail.keys() == [ALERT_KEY]                                   # told once, not on every run
    assert env.db.get_activation(NEXT) is None


# ---- folders named by hand, both / one / neither present ---------------------------------------

def test_hand_made_lowercase_next_folder_is_adopted_not_duplicated(env):
    """Someone made 'fy27-28' by hand with a file in it: the backup uses that folder, makes no second one,
    and leaves the file exactly where it is."""
    _put_file(env.src / "fy27-28")
    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "activated" and "adopted" in res["reason"]
    names = [n for n in _listing(env.src) if n.upper() == NEXT]
    assert names == ["fy27-28"]
    assert (env.src / "fy27-28" / "client.xlsx").read_text(encoding="utf-8") == "precious"
    assert env.db.get_activation(NEXT)["created_by_app"] == 0


@pytest.mark.parametrize("bad_name", ["FY27-28 ", "FY27-28 .", "FY27-28\u00a0"])
def test_next_folder_with_odd_whitespace_is_not_mistaken_for_the_right_one(env, bad_name):
    """A name with a trailing space is not the canonical name. Windows strips trailing spaces from folder names
    through the normal API, so when the odd name cannot be stored the test is skipped, not passed."""
    try:
        os.mkdir(env.src / bad_name)
    except OSError:
        pytest.skip("the file system cannot store this folder name")
    stored = [n for n in _listing(env.src) if n not in (CFG_FY,)]
    if bad_name not in stored:
        pytest.skip("Windows stored the folder under a different name, so the odd name cannot be tested")
    found = ov.sibling_findings(env.cfg, [CFG_FY])
    assert [f["name"] for f in found] == [bad_name]
    assert found[0]["kind"] == ov.SIBLING_MISNAMED


@pytest.mark.xfail(strict=False, reason="SUSPECTED BUG: a folder named 'FY27-8' is never reported as misnamed "
                   "because suggested_name() gives the same name back and the check then finds the folder itself "
                   "(core/fy_overlap.py:469-470 with suggested_name at :492-498)")
def test_fy27_8_missing_zero_is_reported_as_misnamed(env):
    _put_file(env.src / "FY27-8")
    found = ov.sibling_findings(env.cfg, [CFG_FY])
    assert [(f["kind"], f["name"]) for f in found] == [(ov.SIBLING_MISNAMED, "FY27-8")]


@pytest.mark.parametrize("name", ["FY 27-28", "FY2027-28", "FY_27_28"])
def test_misnamed_next_year_folders_are_reported_with_the_right_name(env, name):
    """Next year's folder under a name the backup does not know: reported, with the canonical name to use."""
    (env.src / name).mkdir()
    found = ov.sibling_findings(env.cfg, [CFG_FY])
    assert [(f["kind"], f["name"]) for f in found] == [(ov.SIBLING_MISNAMED, name)]
    title, message = ov.sibling_text(found[0], env.cfg, RUN_DATE)
    assert f'"{name}" is not backed up' in title
    assert "named FY27-28" in message and "NOT backed up" in message


@pytest.mark.parametrize("name", ["FY25-26 archive", "FY 26-27 copy"])
def test_archive_style_names_for_the_configured_or_past_year_are_ignored(env, name):
    (env.src / name).mkdir()
    assert ov.sibling_findings(env.cfg, [CFG_FY]) == []


def test_both_folders_present_nothing_reported_and_nothing_created(env):
    _activate(env.db)
    _put_file(env.src / NEXT)
    before = _listing(env.src)
    assert ov.sibling_findings(env.cfg, [NEXT, CFG_FY]) == []
    assert ov.active_folders(env.cfg, env.db, RUN_DATE) == [NEXT, CFG_FY]
    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "already_active"
    assert _listing(env.src) == before


def test_only_the_configured_folder_present_next_is_created_by_the_app(env):
    _put_file(env.src / CFG_FY, "current.xlsx")
    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "activated" and res["fy"] == NEXT
    assert env.db.get_activation(NEXT)["created_by_app"] == 1
    assert (env.src / CFG_FY / "current.xlsx").exists()


def test_neither_folder_present_creates_only_the_next_one(env):
    for child in list((env.src / CFG_FY).iterdir()):
        child.rmdir()
    (env.src / CFG_FY).rmdir()
    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "activated"
    assert _listing(env.src) == [NEXT]                                  # configured folder never invented


def test_junction_named_as_next_year_is_ignored_by_the_sibling_check_and_its_target_is_untouched(env):
    target = env.tmp / "elsewhere"
    _put_file(target, "old-client.xlsx", "keep me")
    if not _make_junction(env.src / NEXT, target):
        pytest.skip("the OS refused to create a junction")
    assert ov.sibling_findings(env.cfg, [CFG_FY]) == []
    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "deferred" and "link" in res["reason"]
    assert (target / "old-client.xlsx").read_text(encoding="utf-8") == "keep me"
    assert env.db.get_activation(NEXT) is None


def test_permission_denied_on_create_defers_with_no_row_then_alerts_once(env, frozen, monkeypatch):
    real_mkdir = Path.mkdir

    def denied(self, *args, **kwargs):
        if self.name == NEXT:
            raise PermissionError("[WinError 5] Access is denied")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied)
    mail = Mail()
    frozen(T0)
    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=mail)
    assert res["state"] == "deferred" and "could not be created" in res["reason"]
    assert "PermissionError" in res["reason"]
    assert env.db.get_activation(NEXT) is None and not (env.src / NEXT).exists()
    assert mail.events == []
    frozen(T0.add(hours=25))
    ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=mail)
    assert mail.keys() == [ALERT_KEY]
    assert env.db.get_activation(NEXT) is None


# ---- settings ---------------------------------------------------------------------------------

def test_overlap_off_with_files_in_next_folder_warns_and_touches_nothing(env):
    env.cfg.rollover.overlap_enabled = False
    _put_file(env.src / NEXT)
    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "not_needed"
    assert ov.active_folders(env.cfg, env.db, RUN_DATE) == [CFG_FY]
    found = ov.sibling_findings(env.cfg, [CFG_FY])
    assert [(f["kind"], f["fy"]) for f in found] == [(ov.SIBLING_UNPROTECTED, NEXT)]
    _, message = ov.sibling_text(found[0], env.cfg, RUN_DATE, env.db)
    assert "switched off" in message
    assert (env.src / NEXT / "client.xlsx").read_text(encoding="utf-8") == "precious"


@pytest.mark.parametrize("bad", [0, 4])
def test_early_start_month_outside_1_to_3_is_refused_by_the_config(bad):
    with pytest.raises(ValidationError):
        RolloverConfig(early_start_month=bad)


@pytest.mark.parametrize("good", [1, 3])
def test_early_start_month_at_its_bounds_is_accepted(good):
    assert RolloverConfig(early_start_month=good).early_start_month == good


def test_config_pointing_at_a_non_fy_leaf_does_nothing(env):
    env.cfg.paths.source_drive = str(env.src / "DATA")
    before = _listing(env.src)
    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "not_needed"
    assert ov.protected_fy_names(env.cfg) == []
    assert ov.protected_folders(env.cfg) == []
    assert ov.sibling_findings(env.cfg, [effective_fy_prefix(env.cfg.paths.source_drive)]) == []
    assert ov.active_folders(env.cfg, env.db, RUN_DATE) == [effective_fy_prefix(env.cfg.paths.source_drive)]
    assert _listing(env.src) == before


# ---- idempotence and database ---------------------------------------------------------------

def test_activation_recorded_twice_keeps_the_first_row_and_one_mail(env):
    mail = Mail()
    first = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=mail)
    row = dict(env.db.get_activation(NEXT))
    assert first["state"] == "activated"
    assert env.db.record_activation(NEXT, activated_at="2027-01-05T09:00:00+05:30", source_path="X",
                                    created_by_app=False, clock_state="OK") is False
    again = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=mail)
    assert again["state"] == "already_active"
    assert env.db.get_activation(NEXT) == row
    assert mail.keys() == [f"overlap:start:{NEXT}"]


def test_manifest_db_renamed_fails_safe_and_next_folder_is_adopted_again(env):
    """After a manifest.db rename the activation row is gone: only the configured year is backed up (safe),
    the sibling check flags the early folder, and the next run adopts it again without touching its files."""
    ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    _put_file(env.src / NEXT, "early.xlsx", "early work")
    db_path = Path(env.db.db_path) if hasattr(env.db, "db_path") else env.tmp / "m.db"
    env.db.close()
    renamed = db_path.with_name("m.db.old")
    os.replace(db_path, renamed)
    env.db = ManifestDB(db_path)
    assert env.db.get_activation(NEXT) is None
    assert ov.active_folders(env.cfg, env.db, RUN_DATE) == [CFG_FY]
    found = ov.sibling_findings(env.cfg, [CFG_FY])
    assert [(f["kind"], f["fy"]) for f in found] == [(ov.SIBLING_UNPROTECTED, NEXT)]
    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "activated" and "adopted" in res["reason"]
    assert (env.src / NEXT / "early.xlsx").read_text(encoding="utf-8") == "early work"
    assert ov.active_folders(env.cfg, env.db, RUN_DATE) == [NEXT, CFG_FY]
