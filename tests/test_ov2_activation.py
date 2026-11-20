"""OV-2.3 / OV-2.4 / OV-2.5: overlap activation and the early NAS folder (core/fy_overlap.py)."""

import os
import subprocess
from types import SimpleNamespace

import pendulum
import pytest

from core import fy_overlap as ov
from core import fy_rollover
from core.guards import MOUNT_MARKER
from core.health import CLOCK_OK, CLOCK_UNVERIFIED, source_drive_fault
from core.manifest import ManifestDB
from tests.test_ov2_fy_overlap import CFG_FY, NEXT
from tests.test_ov2_fy_overlap import _cfg as _unc_cfg


def _base_cfg(tmp_path, source, nas, lan=True, **rollover):
    """The shared config builder, with the NAS path swapped for a real local folder after validation
    (the model only accepts UNC paths, and a test needs one it can create folders in)."""
    cfg = _unc_cfg(tmp_path, source=source, lan=lan, **rollover)
    object.__setattr__(cfg.paths, "lan_destination", nas)
    return cfg

RUN_DATE = pendulum.date(2027, 1, 1)
T0 = pendulum.datetime(2027, 1, 1, 2, 1, tz="Asia/Kolkata")


class Mail:
    """notify_fn double: collects the events."""

    def __init__(self, status="SENT", raises=False):
        self.events, self.status, self.raises = [], status, raises

    def __call__(self, config, event, db=None):
        self.events.append(event)
        if self.raises:
            raise RuntimeError("smtp down")
        return SimpleNamespace(status=self.status)

    def keys(self):
        return [e.key for e in self.events]


class Clock:
    def __init__(self, state=CLOCK_OK, reason=""):
        self.state, self.reason, self.calls = state, reason, 0

    def __call__(self):
        self.calls += 1
        return SimpleNamespace(state=self.state, reason=self.reason,
                               to_metrics=lambda: {"state": self.state})


@pytest.fixture
def env(tmp_path):
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / CFG_FY).mkdir()
    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    cfg = _base_cfg(tmp_path, source=str(src_root / CFG_FY), nas=str(nas_root / CFG_FY))
    db = ManifestDB(tmp_path / "m.db")
    yield SimpleNamespace(cfg=cfg, db=db, src=src_root, nas=nas_root, tmp=tmp_path)
    db.close()


@pytest.fixture
def frozen():
    def go(dt):
        pendulum.travel_to(dt, freeze=True)
    yield go
    pendulum.travel_back()


def _run(env, clock=None, mail=None, run_date=RUN_DATE):
    mail = mail if mail is not None else Mail()
    return ov.ensure_overlap_activated(env.cfg, run_date, env.db, clock_check=clock or Clock(),
                                       notify_fn=mail), mail


# ---- OV-2.3 ---------------------------------------------------------------------------------

def test_not_needed_outside_window_and_when_overlap_off(env, tmp_path):
    res, mail = _run(env, run_date=pendulum.date(2026, 12, 31))
    assert res["state"] == "not_needed" and res["fy"] is None and not mail.events
    off = _base_cfg(tmp_path, source=str(env.src / CFG_FY), nas=str(env.nas / CFG_FY), overlap_enabled=False)
    res = ov.ensure_overlap_activated(off, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "not_needed"
    assert not (env.src / NEXT).exists()


def test_created_when_missing_with_placeholder_and_row_and_one_info_mail(env):
    clock = Clock()
    res, mail = _run(env, clock)
    assert res["state"] == "activated" and res["fy"] == NEXT
    folder = env.src / NEXT
    assert folder.is_dir()
    note = folder / env.cfg.rollover.source_placeholder_name
    assert note.read_text(encoding="utf-8") == fy_rollover._PLACEHOLDER_TEXT.format(fy=NEXT)
    row = env.db.get_activation(NEXT)
    assert row["created_by_app"] == 1 and row["source_path"] == str(folder) and row["clock_state"] == "OK"
    assert mail.keys() == [f"overlap:start:{NEXT}"]
    ev = mail.events[0]
    assert ev.kind == "INFO" and ev.mode == "rollover" and ev.fy == NEXT
    assert CFG_FY in ev.message and NEXT in ev.message
    assert ov.active_folders(env.cfg, env.db, RUN_DATE) == [NEXT, CFG_FY]


def test_does_not_create_parents(env, tmp_path):
    cfg = _base_cfg(tmp_path, source=str(tmp_path / "gone" / CFG_FY), nas=str(env.nas / CFG_FY))
    res = ov.ensure_overlap_activated(cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "deferred" and "cannot be reached" in res["reason"]
    assert not (tmp_path / "gone").exists()
    assert env.db.get_activation(NEXT) is None


def test_adopts_non_empty_folder_without_placeholder(env):
    folder = env.src / NEXT
    folder.mkdir()
    (folder / "ledger.xlsx").write_text("data")
    res, mail = _run(env)
    assert res["state"] == "activated"
    assert sorted(p.name for p in folder.iterdir()) == ["ledger.xlsx"]
    assert env.db.get_activation(NEXT)["created_by_app"] == 0
    assert mail.keys() == [f"overlap:start:{NEXT}"]


def test_adopts_empty_folder_and_gives_it_the_placeholder(env):
    folder = env.src / NEXT
    folder.mkdir()
    res, _ = _run(env)
    assert res["state"] == "activated"
    assert [p.name for p in folder.iterdir()] == [env.cfg.rollover.source_placeholder_name]
    assert env.db.get_activation(NEXT)["created_by_app"] == 0


def test_canonical_name_matches_case_insensitively(env):
    folder = env.src / "fy27-28"
    folder.mkdir()
    (folder / "a.txt").write_text("x")
    res, _ = _run(env)
    assert res["state"] == "activated"
    assert env.db.get_activation(NEXT)["source_path"] == str(folder)
    assert sorted(p.name for p in env.src.iterdir()) == ["FY26-27", "fy27-28"]     # no second folder


def test_unproven_clock_defers_without_touching_folders_then_activates_later(env):
    bad = Clock(CLOCK_UNVERIFIED, "no time server")
    res, mail = _run(env, bad)
    assert res["state"] == "deferred" and "clock" in res["reason"]
    assert not (env.src / NEXT).exists() and env.db.get_activation(NEXT) is None
    assert not mail.events
    assert ov.active_folders(env.cfg, env.db, RUN_DATE) == [CFG_FY]
    res, _ = _run(env, bad, mail)
    assert res["state"] == "deferred" and bad.calls == 2          # every call re-checks
    good = Clock()
    res, mail = _run(env, good, mail)
    assert res["state"] == "activated"
    assert (env.src / NEXT).is_dir()
    assert mail.keys() == [f"overlap:start:{NEXT}"]


def test_clock_skewed_is_not_proven_either(env):
    res, _ = _run(env, Clock("SKEWED", "off by an hour"))
    assert res["state"] == "deferred"
    assert not (env.src / NEXT).exists()


def test_alert_only_after_the_configured_hours_and_only_once(env, frozen):
    hours = env.cfg.rollover.activation_alert_after_hours
    bad, mail = Clock(CLOCK_UNVERIFIED, "no time server"), Mail()
    frozen(T0)
    _run(env, bad, mail)                                           # first deferral: marker only
    assert not mail.events
    frozen(T0.add(hours=hours - 1))
    _run(env, bad, mail)
    assert not mail.events                                         # window not long enough yet
    frozen(T0.add(hours=hours + 1))
    _run(env, bad, mail)
    assert mail.keys() == [f"overlap:activation:{NEXT}"]
    ev = mail.events[0]
    assert ev.kind == "ACTION_REQUIRED" and ev.fy == NEXT and ev.fault_code == "AAM-ROL-03"
    assert "is not being backed up because" in ev.message
    frozen(T0.add(hours=hours + 5))
    _run(env, bad, mail)
    frozen(T0.add(hours=hours + 30))
    _run(env, bad, mail)
    assert mail.keys() == [f"overlap:activation:{NEXT}"]           # still ONE
    assert env.db.get_activation(NEXT) is None


def test_first_deferral_time_survives_a_new_db_connection(env, frozen):
    hours = env.cfg.rollover.activation_alert_after_hours
    bad, mail = Clock(CLOCK_UNVERIFIED, "x"), Mail()
    frozen(T0)
    _run(env, bad, mail)
    env.db.close()
    db2 = ManifestDB(env.tmp / "m.db")
    frozen(T0.add(hours=hours + 1))
    ov.ensure_overlap_activated(env.cfg, RUN_DATE, db2, clock_check=bad, notify_fn=mail)
    db2.close()
    env.db = ManifestDB(env.tmp / "m.db")
    assert mail.keys() == [f"overlap:activation:{NEXT}"]


def test_failed_alert_mail_is_tried_again(env, frozen):
    hours = env.cfg.rollover.activation_alert_after_hours
    bad, mail = Clock(CLOCK_UNVERIFIED, "x"), Mail(status="FAILED")
    frozen(T0)
    _run(env, bad, mail)
    frozen(T0.add(hours=hours + 1))
    _run(env, bad, mail)
    _run(env, bad, mail)
    assert len(mail.events) == 2


def test_parent_missing_defers_and_alerts_after_hours(env, tmp_path, frozen):
    cfg = _base_cfg(tmp_path, source=str(tmp_path / "gone" / CFG_FY), nas=str(env.nas / CFG_FY))
    hours = cfg.rollover.activation_alert_after_hours
    mail = Mail()
    frozen(T0)
    ov.ensure_overlap_activated(cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=mail)
    frozen(T0.add(hours=hours + 1))
    res = ov.ensure_overlap_activated(cfg, RUN_DATE, env.db, clock_check=Clock(), notify_fn=mail)
    assert res["state"] == "deferred"
    assert mail.keys() == [f"overlap:activation:{NEXT}"]
    assert "cannot be reached" in mail.events[0].message


def _make_link(link, target):
    """A symlink, else a Windows junction; skip only when the OS refuses both."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return
    pytest.skip("this OS refuses to create a symlink or junction here")


def test_symlink_is_refused(env):
    target = env.tmp / "elsewhere"
    target.mkdir()
    _make_link(env.src / NEXT, target)
    res, mail = _run(env)
    assert res["state"] == "deferred" and "link" in res["reason"]
    assert env.db.get_activation(NEXT) is None
    assert os.listdir(target) == []                                # nothing written through the link


def test_junction_is_refused(env):
    if os.name != "nt":
        pytest.skip("junctions are Windows-only")
    target = env.tmp / "elsewhere"
    target.mkdir()
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(env.src / NEXT), str(target)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("the OS refused to create a junction")
    res, _ = _run(env)
    assert res["state"] == "deferred" and "link" in res["reason"]
    assert env.db.get_activation(NEXT) is None and os.listdir(target) == []


def test_a_file_with_the_name_is_not_adopted(env):
    (env.src / NEXT).write_text("not a folder")
    res, _ = _run(env)
    assert res["state"] == "deferred" and env.db.get_activation(NEXT) is None


def test_idempotent_second_call_is_already_active_one_mail(env):
    res1, mail = _run(env)
    clock2 = Clock(CLOCK_UNVERIFIED, "ignored")
    res2, _ = _run(env, clock2, mail)
    assert (res1["state"], res2["state"]) == ("activated", "already_active")
    assert clock2.calls == 0                                       # no clock proof once active
    assert mail.keys() == [f"overlap:start:{NEXT}"]


def test_activation_time_never_moves(env, frozen):
    frozen(T0)
    _run(env)
    first = env.db.get_activation(NEXT)["activated_at"]
    frozen(T0.add(days=3))
    _run(env)
    assert env.db.get_activation(NEXT)["activated_at"] == first


def test_mail_failure_does_not_undo_activation(env):
    res, mail = _run(env, mail=Mail(raises=True))
    assert res["state"] == "activated"
    assert env.db.get_activation(NEXT) is not None and (env.src / NEXT).is_dir()
    assert len(mail.events) == 1


def test_switch_pending_day_activates_too(env):
    res, _ = _run(env, run_date=pendulum.date(2027, 4, 2))
    assert res["state"] == "activated"


def test_never_raises_on_a_broken_db(env):
    class Broken:
        def get_activation(self, fy):
            raise RuntimeError("db gone")

        def rollover_get(self, *a):
            raise RuntimeError("db gone")
    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, Broken(), clock_check=Clock(), notify_fn=Mail())
    assert res["state"] == "deferred"


def test_default_clock_check_uses_health_config(env, monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return SimpleNamespace(state=CLOCK_OK, reason="")
    monkeypatch.setattr("core.health.check_clock", fake)
    res = ov.ensure_overlap_activated(env.cfg, RUN_DATE, env.db, notify_fn=Mail())
    assert res["state"] == "activated"
    h = env.cfg.health
    assert (seen["max_skew_seconds"], seen["connection_timeout"], seen["ntp_server"], seen["w32tm_timeout_s"]) == (
        h.max_clock_skew_seconds, h.clock_check_timeout_seconds, h.ntp_server, h.w32tm_timeout_seconds)


# ---- OV-2.4 ---------------------------------------------------------------------------------

def test_missing_after_activation_is_not_recreated_and_still_fails_the_health_check(env):
    _run(env)
    folder = env.src / NEXT
    for child in folder.iterdir():
        child.unlink()
    folder.rmdir()
    clock = Clock()
    res, mail = _run(env, clock, Mail())
    assert res["state"] == "already_active" and not mail.events and clock.calls == 0
    assert not folder.exists()                                      # never recreated
    assert ov.active_folders(env.cfg, env.db, RUN_DATE) == [NEXT, CFG_FY]
    view = ov.folder_view(env.cfg, NEXT)
    code, reason = source_drive_fault(view.paths.source_drive)      # the pipeline's own health check
    assert code == "AAM-SRC-01" and NEXT in reason


# ---- OV-2.5 ---------------------------------------------------------------------------------

def _nas(env):
    return env.nas / NEXT


def test_nas_missing_is_created_with_marker(env):
    res = ov.ensure_overlap_nas_folder(env.cfg, NEXT)
    assert res["state"] == "created" and res["fy"] == NEXT
    assert [p.name for p in _nas(env).iterdir()] == [MOUNT_MARKER]


def test_nas_with_marker_is_ok(env):
    _nas(env).mkdir()
    (_nas(env) / MOUNT_MARKER).touch()
    (_nas(env) / "a.txt").write_text("x")
    assert ov.ensure_overlap_nas_folder(env.cfg, NEXT)["state"] == "ok"


def test_nas_marker_name_is_case_insensitive(env):
    _nas(env).mkdir()
    (_nas(env) / MOUNT_MARKER.lower()).touch()
    assert ov.ensure_overlap_nas_folder(env.cfg, NEXT)["state"] == "ok"


def test_nas_empty_gets_marker(env):
    _nas(env).mkdir()
    assert ov.ensure_overlap_nas_folder(env.cfg, NEXT)["state"] == "marked"
    assert [p.name for p in _nas(env).iterdir()] == [MOUNT_MARKER]


def test_nas_non_empty_without_marker_is_untouched(env):
    _nas(env).mkdir()
    (_nas(env) / "someone.txt").write_text("x")
    res = ov.ensure_overlap_nas_folder(env.cfg, NEXT)
    assert res["state"] == "needs_confirmation"
    assert [p.name for p in _nas(env).iterdir()] == ["someone.txt"]


def test_nas_unreachable_share_does_nothing(env, tmp_path):
    cfg = _base_cfg(tmp_path, source=str(env.src / CFG_FY), nas=str(tmp_path / "nonas" / CFG_FY))
    res = ov.ensure_overlap_nas_folder(cfg, NEXT)
    assert res["state"] == "unreachable"
    assert not (tmp_path / "nonas").exists()


def test_nas_lan_off_does_nothing(env, tmp_path):
    cfg = _base_cfg(tmp_path, source=str(env.src / CFG_FY), nas=str(env.nas / CFG_FY), lan=False)
    assert ov.ensure_overlap_nas_folder(cfg, NEXT)["state"] == "unreachable"
    assert not _nas(env).exists()


def test_nas_works_for_the_configured_fy_too(env):
    assert ov.ensure_overlap_nas_folder(env.cfg, CFG_FY)["state"] == "created"


# ---- fy_rollover edits ----------------------------------------------------------------------

def test_rollover_create_nas_folder_leaves_non_empty_unmarked_folder_alone(env):
    _nas(env).mkdir()
    (_nas(env) / "someone.txt").write_text("x")
    path = fy_rollover.create_nas_folder(str(env.nas), NEXT)
    assert path == _nas(env)
    assert [p.name for p in path.iterdir()] == ["someone.txt"]


def test_rollover_create_nas_folder_still_marks_new_and_empty_folders(env):
    path = fy_rollover.create_nas_folder(str(env.nas), NEXT)
    assert [p.name for p in path.iterdir()] == [MOUNT_MARKER]
    fy_rollover.create_nas_folder(str(env.nas), NEXT)      # idempotent
    assert [p.name for p in path.iterdir()] == [MOUNT_MARKER]


def test_rollover_create_source_folder_keeps_existing_files_free_of_placeholder(env):
    folder = env.src / NEXT
    folder.mkdir()
    (folder / "ledger.xlsx").write_text("x")
    out = fy_rollover.create_source_folder(str(env.src), NEXT, env.cfg.rollover.source_placeholder_name)
    assert out == folder and [p.name for p in folder.iterdir()] == ["ledger.xlsx"]


def test_rollover_create_source_folder_writes_placeholder_into_empty_folder_once(env):
    name = env.cfg.rollover.source_placeholder_name
    out = fy_rollover.create_source_folder(str(env.src), NEXT, name)
    fy_rollover.create_source_folder(str(env.src), NEXT, name)
    assert [p.name for p in out.iterdir()] == [name]
