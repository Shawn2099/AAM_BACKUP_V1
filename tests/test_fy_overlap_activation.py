"""B1 - activation of the next financial year on 31 December (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md,
sections 1 (D-2), 3 and 9 task B1).

The daily rollover check creates the next year's SOURCE folder (+ placeholder) and writes the activation record,
only on a proven clock. The NAS folder is NOT made here (the next-year LAN pass does it, offline-safe).

Owner layout: source `E:\\FY26-27` on a drive root (the drive is a temp folder here), NAS `\\\\nas\\backup\\FY26-27`
(never touched by activation). A non-root source `D:\\Data\\FY26-27` is the control.
Fakes: `check_clock` and `notify` in core.fy_overlap; `pendulum.travel_to` moves the date. The real clock check is
never called.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pendulum
import pytest

import time_machine  # noqa: E402,F401  hard import: a missing package must fail loudly, never skip the file (pendulum.travel_to needs it)

import flow  # noqa: E402
from core import fy_overlap as fo  # noqa: E402
from core import fy_rollover as fr  # noqa: E402
from core.guards import MOUNT_MARKER  # noqa: E402
from core.health import CLOCK_OK, CLOCK_SKEWED, CLOCK_UNVERIFIED, ClockResult  # noqa: E402
from core.manifest import ManifestDB  # noqa: E402
from core.time_utils import IST  # noqa: E402
from models.config import AppConfig  # noqa: E402
from test_support import ui_login  # noqa: E402

FROM, TO = "FY26-27", "FY27-28"
LAN_DEST = r"\\nas\backup\FY26-27"
ACTIVATED_KEY = f"rollover:activated:{TO}"
NOT_ADDED_KEY = f"rollover:activation:{TO}"


def _clock(state: str) -> ClockResult:
    if state == CLOCK_OK:
        return ClockResult(CLOCK_OK, 0.2, "google", "")
    return ClockResult(state, None, None, f"test clock {state}")


def _config(tmp_path: Path, source: Path, *, overlap_enabled: bool = True, activate_on: str = "12-31") -> AppConfig:
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(source), "lan_destination": LAN_DEST,
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        rollover={"overlap_enabled": overlap_enabled, "overlap_activate_on": activate_on},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": 25, "sender": "b@firm.example",
                       "recipients": ["ops@firm.example"], "smtp_security": "none", "smtp_auth": False})


@pytest.fixture(params=["owner", "data"])
def source(request, tmp_path) -> Path:
    """The configured source folder: the owner's drive-root layout, or the non-root control."""
    if request.param == "owner":
        folder = tmp_path / "E" / FROM
    else:
        folder = tmp_path / "D" / "Data" / FROM
    folder.mkdir(parents=True)
    return folder


@pytest.fixture
def cfg(tmp_path, source) -> AppConfig:
    return _config(tmp_path, source)


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


@pytest.fixture
def fakes():
    """check_clock and notify faked; the clock answers OK unless a test changes `clock.return_value`."""
    clock = MagicMock(return_value=_clock(CLOCK_OK))
    notify = MagicMock()
    with patch.object(fo, "check_clock", clock), patch.object(fo, "notify", notify):
        yield SimpleNamespace(clock=clock, notify=notify)


def _at(y: int, m: int, d: int, h: int = 6):
    return pendulum.datetime(y, m, d, h, 0, 0, tz=IST)


def _run(cfg, db, when):
    with pendulum.travel_to(when, freeze=True):
        return fo.activate_if_due(cfg, db)


def _events(fakes):
    return [c.args[1] for c in fakes.notify.call_args_list]


def _new_folder(source: Path) -> Path:
    return source.parent / TO


# --- the due date -------------------------------------------------------------------------------------------

def test_dec_30_is_not_due(cfg, db, fakes, source):
    assert _run(cfg, db, _at(2026, 12, 30)) == fo.NOT_DUE
    assert not _new_folder(source).exists()
    fakes.clock.assert_not_called()
    assert db.overlap_record(TO) is None


def test_dec_31_creates_folder_placeholder_record_and_one_info_mail(cfg, db, fakes, source):
    assert _run(cfg, db, _at(2026, 12, 31)) == fo.ACTIVATED
    folder = _new_folder(source)
    assert folder.is_dir()
    assert (folder / cfg.rollover.source_placeholder_name).is_file()
    record = db.overlap_record(TO)
    assert record is not None
    assert record["from_fy"] == FROM
    assert Path(record["source_folder"]) == folder
    assert record["activated_at"]
    events = _events(fakes)
    assert len(events) == 1
    assert events[0].key == ACTIVATED_KEY and events[0].kind == "INFO"
    assert events[0].audience == "reports"


def test_jan_1_catches_up_when_the_server_was_off_on_dec_31(cfg, db, fakes, source):
    assert _run(cfg, db, _at(2027, 1, 1)) == fo.ACTIVATED
    assert _new_folder(source).is_dir()
    assert db.overlap_record(TO) is not None


def test_second_call_is_a_no_op(cfg, db, fakes, source):
    _run(cfg, db, _at(2026, 12, 31))
    fakes.notify.reset_mock()
    fakes.clock.reset_mock()
    assert _run(cfg, db, _at(2027, 1, 2)) == fo.ALREADY_ACTIVE
    fakes.clock.assert_not_called()
    fakes.notify.assert_not_called()


def test_crash_after_the_folder_rerun_completes(cfg, db, fakes, source, monkeypatch):
    real_set = db.set_overlap_record
    calls = {"n": 0}

    def crash_once(fy, record):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash between the folder and the record")
        return real_set(fy, record)

    monkeypatch.setattr(db, "set_overlap_record", crash_once)
    assert _run(cfg, db, _at(2026, 12, 31)) == fo.FAILED          # never raises
    assert _new_folder(source).is_dir()                             # the folder was made
    assert db.overlap_record(TO) is None                            # but no record yet
    fakes.notify.assert_not_called()

    assert _run(cfg, db, _at(2027, 1, 1)) == fo.ACTIVATED           # next morning completes it
    assert db.overlap_record(TO) is not None
    assert [e.key for e in _events(fakes)] == [ACTIVATED_KEY]


def test_crash_after_the_record_redoes_nothing(cfg, db, fakes, source):
    _run(cfg, db, _at(2026, 12, 31))
    with patch.object(fr, "create_source_folder", wraps=fr.create_source_folder) as spy:
        assert _run(cfg, db, _at(2027, 1, 1)) == fo.ALREADY_ACTIVE
    spy.assert_not_called()


# --- proven clock ---------------------------------------------------------------------------------------------

def test_unproven_clock_creates_nothing_warns_then_asks_next_day_then_succeeds(cfg, db, fakes, source):
    fakes.clock.return_value = _clock(CLOCK_UNVERIFIED)
    assert _run(cfg, db, _at(2026, 12, 31)) == fo.CLOCK_UNPROVEN
    assert not _new_folder(source).exists()
    assert db.overlap_record(TO) is None
    first = _events(fakes)[-1]
    assert first.key == NOT_ADDED_KEY and first.kind == "WARNING"
    assert first.fault_code == "AAM-ROL-07"

    assert _run(cfg, db, _at(2027, 1, 1)) == fo.CLOCK_UNPROVEN
    assert not _new_folder(source).exists()
    assert _events(fakes)[-1].kind == "ACTION_REQUIRED"

    fakes.clock.return_value = _clock(CLOCK_OK)
    assert _run(cfg, db, _at(2027, 1, 2)) == fo.ACTIVATED
    assert _new_folder(source).is_dir()
    assert _events(fakes)[-1].key == ACTIVATED_KEY


def test_skewed_clock_waits_too(cfg, db, fakes, source):
    fakes.clock.return_value = _clock(CLOCK_SKEWED)
    assert _run(cfg, db, _at(2026, 12, 31)) == fo.CLOCK_UNPROVEN
    assert not _new_folder(source).exists()


def test_system_clock_behind_does_nothing(cfg, db, fakes, source):
    assert _run(cfg, db, _at(2026, 11, 20)) == fo.NOT_DUE
    fakes.clock.assert_not_called()
    assert not _new_folder(source).exists()


def test_clock_behind_the_configured_year_is_refused_without_a_clock_check(cfg, db, fakes, source):
    with patch.object(fo, "get_fy_prefix", return_value="FY25-26"):
        assert _run(cfg, db, _at(2027, 1, 1)) == fo.REFUSED_STATE
    fakes.clock.assert_not_called()
    assert not _new_folder(source).exists()


def test_jump_of_more_than_one_year_is_refused(cfg, db, fakes, source):
    assert _run(cfg, db, _at(2028, 6, 1)) == fo.REFUSED_STATE
    fakes.clock.assert_not_called()
    assert not _new_folder(source).exists()


# --- switches and layout --------------------------------------------------------------------------------------

def test_overlap_off_does_nothing_at_all(tmp_path, source, db, fakes):
    cfg = _config(tmp_path, source, overlap_enabled=False)
    assert _run(cfg, db, _at(2026, 12, 31)) == fo.DISABLED
    fakes.clock.assert_not_called()
    fakes.notify.assert_not_called()
    assert not _new_folder(source).exists()
    assert db.overlap_record(TO) is None


def test_source_without_a_fy_folder_is_a_no_op(tmp_path, cfg, db, fakes):
    data = tmp_path / "Data"
    data.mkdir()
    cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"source_drive": str(data)})})
    assert _run(cfg, db, _at(2026, 12, 31)) == fo.NO_FY_FOLDER
    fakes.clock.assert_not_called()
    assert not (tmp_path / TO).exists()


def test_office_made_folder_is_adopted(cfg, db, fakes, source):
    folder = _new_folder(source)
    folder.mkdir()
    (folder / "office.xlsx").write_text("saved by the office", encoding="utf-8")
    assert _run(cfg, db, _at(2026, 12, 31)) == fo.ACTIVATED
    assert (folder / "office.xlsx").read_text(encoding="utf-8") == "saved by the office"
    assert (folder / cfg.rollover.source_placeholder_name).is_file()
    assert db.overlap_record(TO) is not None


def test_link_or_junction_is_refused_with_the_reason(cfg, db, fakes, source):
    folder = _new_folder(source)
    folder.mkdir()
    with patch.object(fo, "_is_link_or_junction", return_value=True):
        assert _run(cfg, db, _at(2026, 12, 31)) == fo.REFUSED_LINK
    assert db.overlap_record(TO) is None
    assert not (folder / cfg.rollover.source_placeholder_name).exists()
    last = _events(fakes)[-1]
    assert last.key == NOT_ADDED_KEY and last.kind == "WARNING"
    assert "link or junction" in last.message


def test_activation_never_touches_the_nas(cfg, db, fakes, source, tmp_path):
    with patch.object(fo, "ensure_nas_folder") as nas:
        assert _run(cfg, db, _at(2026, 12, 31)) == fo.ACTIVATED
    nas.assert_not_called()
    assert not (tmp_path / "nas").exists()


def test_rollover_check_runs_activation_before_the_unprotected_alarm(monkeypatch):
    calls = []
    monkeypatch.setattr(flow, "log_retention_task", MagicMock())
    monkeypatch.setattr(flow, "load_config", MagicMock())
    monkeypatch.setattr(flow, "ManifestDB", MagicMock())
    with patch("flow.fy_overlap.activate_if_due", side_effect=lambda *a, **k: calls.append("activate")), \
            patch.object(flow, "_alarm_unprotected_new_fy", side_effect=lambda *a: calls.append("alarm")), \
            patch.object(flow, "_rollover_resume",
                         side_effect=lambda *a: calls.append("resume") or "NO_ROLLOVER_NEEDED"):
        assert flow.rollover_check_flow.fn("cfg.yaml") == "NO_ROLLOVER_NEEDED"
    assert calls == ["activate", "alarm", "resume"]


# --- ensure_nas_folder (plan section 3; used by the next-year LAN pass) ---------------------------------------

def test_nas_missing_folder_is_created_with_marker(tmp_path):
    root = tmp_path / "backup"
    root.mkdir()
    assert fo.ensure_nas_folder(str(root), TO) == fo.NAS_CREATED
    assert (root / TO / MOUNT_MARKER).is_file()


def test_nas_folder_with_marker_is_ready(tmp_path):
    root = tmp_path / "backup"
    (root / TO).mkdir(parents=True)
    (root / TO / MOUNT_MARKER).touch()
    assert fo.ensure_nas_folder(str(root), TO) == fo.NAS_READY


def test_nas_empty_folder_gets_the_marker(tmp_path):
    root = tmp_path / "backup"
    (root / TO).mkdir(parents=True)
    assert fo.ensure_nas_folder(str(root), TO) == fo.NAS_MARKED
    assert (root / TO / MOUNT_MARKER).is_file()


def test_nas_folder_with_files_and_no_marker_is_left_alone(tmp_path):
    root = tmp_path / "backup"
    (root / TO).mkdir(parents=True)
    (root / TO / "old.dat").write_text("data", encoding="utf-8")
    assert fo.ensure_nas_folder(str(root), TO) == fo.NAS_NEEDS_APPROVAL
    assert not (root / TO / MOUNT_MARKER).exists()
    assert (root / TO / "old.dat").read_text(encoding="utf-8") == "data"


def test_nas_share_unreachable_raises_and_creates_nothing(tmp_path):
    missing = tmp_path / "no-such-share"
    with pytest.raises(OSError):
        fo.ensure_nas_folder(str(missing), TO)
    assert not missing.exists()
