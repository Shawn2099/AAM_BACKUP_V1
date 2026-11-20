"""Fixes of the Opus review of WP2 (plan docs/PLAN_FY_OVERLAP_2026-11-20.md 5.1, 5.2, 5.5).

I-1 placeholder uses guard G1's rule; I-2 a failed activation escalates and a half-made folder is
remembered; I-3 an unreadable folder never ends an alarm (SiblingReadError); I-4 a misnamed archive is not
alarmed; N-2 the G3 message names the limit that applied; N-4 the deferred alert marker follows the mail status;
N-7 the clock check removes its throwaway store; N-11 RESOLVED wording."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import flow
from core import fy_overlap as ov
from core import fy_rollover, guards
from core import notify as nt
from tests.test_ov2_activation import T0, Clock, Mail, _run, env, frozen  # noqa: F401 - fixtures
from tests.test_ov2_fy_overlap import NEXT
from tests.test_ov2_sibling_check import DEC, FROM, JAN, PLACEHOLDER, TO, _event, _folder, _Mail, _PRIOR, cfg, db  # noqa: F401,E501

PH = "README_FY.txt"


def _skeleton(folder: Path) -> None:
    """Only what G1 never counts: empty client sub-folders, system files and the mount marker."""
    folder.mkdir(exist_ok=True)
    (folder / "Client A" / "2027").mkdir(parents=True)
    (folder / "Client B").mkdir()
    for name in ("desktop.ini", "Thumbs.db", guards.MOUNT_MARKER):
        (folder / name).write_text("x", encoding="utf-8")


# ---- I-1 ---------------------------------------------------------------------------------------

def test_a_skeleton_folder_gets_the_placeholder_so_g1_passes(tmp_path):
    folder = tmp_path / NEXT
    _skeleton(folder)
    assert guards.first_regular_file(str(folder), guards.SOURCE_IGNORE_NAMES) is None      # G1 would block
    assert fy_rollover.write_placeholder_if_empty(folder, NEXT, PH) is True
    assert guards.first_regular_file(str(folder), guards.SOURCE_IGNORE_NAMES) == str(folder / PH)
    guards.require_source_files(str(folder))                                                # does not raise


def test_a_folder_with_one_real_file_gets_no_placeholder(tmp_path):
    folder = tmp_path / NEXT
    _skeleton(folder)
    (folder / "Client A" / "2027" / "ledger.xlsx").write_text("x", encoding="utf-8")        # nested: still real
    assert fy_rollover.write_placeholder_if_empty(folder, NEXT, PH) is False
    assert not (folder / PH).exists()


def test_an_existing_placeholder_is_kept_in_a_skeleton_folder(tmp_path):
    folder = tmp_path / NEXT
    _skeleton(folder)
    (folder / PH).write_text("mine", encoding="utf-8")
    assert fy_rollover.write_placeholder_if_empty(folder, NEXT, PH) is False
    assert (folder / PH).read_text(encoding="utf-8") == "mine"


def test_adopting_a_skeleton_folder_writes_the_placeholder(env):  # noqa: F811
    folder = env.src / NEXT
    _skeleton(folder)
    res, _mail = _run(env)
    assert res["state"] == "activated"
    assert (folder / env.cfg.rollover.source_placeholder_name).is_file()
    assert guards.first_regular_file(str(folder), guards.SOURCE_IGNORE_NAMES) is not None
    assert env.db.get_activation(NEXT)["created_by_app"] == 0


def test_the_rollover_source_step_handles_a_skeleton_folder(tmp_path):
    _skeleton(tmp_path / NEXT)
    out = fy_rollover.create_source_folder(str(tmp_path), NEXT, PH)
    assert (out / PH).is_file()
    guards.require_source_files(str(out))


# ---- I-2 ---------------------------------------------------------------------------------------

def _mkdir_denied(monkeypatch, name):
    real = Path.mkdir

    def deny(self, *a, **k):
        if self.name == name:
            raise PermissionError(5, "Access is denied", str(self))
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "mkdir", deny)


def test_a_failed_mkdir_is_deferred_and_mailed_once_after_the_hours(env, frozen, monkeypatch):  # noqa: F811
    _mkdir_denied(monkeypatch, NEXT)
    mail = Mail()
    frozen(T0)
    res, _ = _run(env, mail=mail)
    assert res["state"] == "deferred" and "PermissionError" in res["reason"]
    assert "right to create folders in" in res["reason"] and str(env.src) in res["reason"]
    assert env.db._meta(f"overlap_first_deferred:{NEXT}")             # the marker is set
    assert mail.events == []                                           # nothing before the hours
    frozen(T0.add(hours=env.cfg.rollover.activation_alert_after_hours + 1))
    _run(env, mail=mail)
    _run(env, mail=mail)
    [event] = mail.events                                              # ONE mail
    assert event.key == f"overlap:activation:{NEXT}" and event.kind == "ACTION_REQUIRED"
    assert "PermissionError" in event.message and "give the backup account" in event.message
    assert env.db.get_activation(NEXT) is None


def test_a_failing_placeholder_write_is_deferred_too(env, frozen):  # noqa: F811
    frozen(T0)
    with patch("core.fy_overlap.write_placeholder_if_empty", side_effect=OSError("disk full")):
        res, mail = _run(env)
    assert res["state"] == "deferred" and "disk full" in res["reason"]
    assert env.db._meta(f"overlap_first_deferred:{NEXT}")
    assert env.db.get_activation(NEXT) is None and mail.events == []


def test_a_failing_deferral_never_raises(env):  # noqa: F811
    with patch("core.fy_overlap.write_placeholder_if_empty", side_effect=OSError("disk full")), \
            patch("core.fy_overlap._defer", side_effect=RuntimeError("db gone")):
        res, _ = _run(env)
    assert res["state"] == "deferred" and "disk full" in res["reason"]


def test_a_folder_made_by_the_app_is_remembered_when_recording_fails(env, frozen):  # noqa: F811
    frozen(T0)
    real = env.db.record_activation
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("database is locked")
        return real(*a, **k)
    env.db.record_activation = flaky
    res, mail = _run(env)
    assert res["state"] == "deferred" and (env.src / NEXT).is_dir()
    assert env.db.get_activation(NEXT) is None
    assert env.db._meta(f"overlap_created_pending:{NEXT}") == str(env.src / NEXT)
    res, mail = _run(env, mail=mail)                                   # next run adopts exactly that folder
    assert res["state"] == "activated"
    assert env.db.get_activation(NEXT)["created_by_app"] == 1
    assert "was created for it" in mail.events[-1].message
    assert not env.db._meta(f"overlap_created_pending:{NEXT}")         # cleared


def test_a_folder_that_was_not_made_by_the_app_stays_adopted(env):  # noqa: F811
    (env.src / NEXT).mkdir()
    env.db._set_meta(f"overlap_created_pending:{NEXT}", str(env.src / "somewhere else"))
    res, mail = _run(env)
    assert res["state"] == "activated" and env.db.get_activation(NEXT)["created_by_app"] == 0
    assert "was already there" in mail.events[-1].message


# ---- N-4 ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("status, marker_set", [("SENT", True), ("SUPPRESSED", True),
                                                ("DISABLED", False), ("FAILED", False)])
def test_the_alert_marker_follows_the_mail_status(env, frozen, monkeypatch, status, marker_set):  # noqa: F811
    _mkdir_denied(monkeypatch, NEXT)
    mail = Mail(status=status)
    frozen(T0)
    _run(env, mail=mail)
    frozen(T0.add(hours=env.cfg.rollover.activation_alert_after_hours + 1))
    _run(env, mail=mail)
    assert bool(env.db._meta(f"overlap_alert_sent:{NEXT}")) is marker_set
    _run(env, mail=mail)
    assert len(mail.events) == (1 if marker_set else 2)               # retried only when not told


# ---- I-3 ---------------------------------------------------------------------------------------

def _fail_scandir_of(monkeypatch, folder):
    real = os.scandir

    def scandir(path="."):
        if os.path.normcase(str(path)) == os.path.normcase(str(folder)):
            raise PermissionError(5, "Access is denied", str(path))
        return real(path)
    monkeypatch.setattr(os, "scandir", scandir)


def test_an_unlistable_parent_raises(tmp_path, cfg, monkeypatch):  # noqa: F811
    _folder(tmp_path, TO, "a.xlsx")
    _fail_scandir_of(monkeypatch, tmp_path / "src")
    with pytest.raises(ov.SiblingReadError):
        ov.sibling_findings(cfg, [FROM])


def test_an_entry_that_cannot_be_classified_raises(tmp_path, cfg, monkeypatch):  # noqa: F811
    _folder(tmp_path, TO, "a.xlsx")
    real = os.scandir

    class Entry:
        def __init__(self, e):
            self._e, self.name, self.path = e, e.name, e.path

        def is_dir(self, *a, **k):
            if self.name == TO:
                raise OSError("network name no longer available")
            return self._e.is_dir(*a, **k)

    monkeypatch.setattr(os, "scandir", lambda p=".": [Entry(e) for e in real(p)])
    with pytest.raises(ov.SiblingReadError):
        ov.sibling_findings(cfg, [FROM])


def test_an_unreadable_candidate_folder_raises_in_both_modes(tmp_path, cfg, monkeypatch):  # noqa: F811
    _folder(tmp_path, TO, "a.xlsx")
    _fail_scandir_of(monkeypatch, tmp_path / "src" / TO)
    for count in (True, False):
        with pytest.raises(ov.SiblingReadError):
            ov.sibling_findings(cfg, [FROM], count=count)


def test_the_counters_signal_unreadable_with_none(tmp_path, cfg, monkeypatch):  # noqa: F811
    _folder(tmp_path, TO, "a.xlsx")
    _fail_scandir_of(monkeypatch, tmp_path / "src" / TO)
    src = cfg.paths.source_drive
    assert fy_rollover.new_fy_file_count(src, TO, PLACEHOLDER) is None
    assert fy_rollover.new_fy_has_files(src, TO, PLACEHOLDER) is None
    assert fy_rollover.new_fy_has_files(src, "FY99-00", PLACEHOLDER) is False       # a missing folder is still False
    assert fy_rollover.new_fy_file_count(src, "FY99-00", PLACEHOLDER) is None


def test_an_unreadable_subfolder_with_files_elsewhere_still_counts(tmp_path, cfg, monkeypatch):  # noqa: F811
    _folder(tmp_path, TO, "a.xlsx", "locked/b.xlsx")
    _fail_scandir_of(monkeypatch, tmp_path / "src" / TO / "locked")
    assert fy_rollover.new_fy_file_count(cfg.paths.source_drive, TO, PLACEHOLDER) == 1


def _open_alarm(tmp_path, cfg, db, *files, name=TO):  # noqa: F811
    _folder(tmp_path, name, *files)
    mail = _Mail(db)
    with patch("flow.notify", mail):
        flow._alarm_siblings(cfg, db, DEC)
    assert [e.kind for e in mail.events] == ["ACTION_REQUIRED"]
    return mail


def test_no_resolved_when_the_parent_listing_fails(tmp_path, cfg, db, monkeypatch):  # noqa: F811
    mail = _open_alarm(tmp_path, cfg, db, "a.xlsx")
    _fail_scandir_of(monkeypatch, tmp_path / "src")
    with patch("flow.notify", mail):
        flow._alarm_siblings(cfg, db, DEC)
    assert [e.kind for e in mail.events] == ["ACTION_REQUIRED"]


def test_no_resolved_when_an_entry_cannot_be_classified(tmp_path, cfg, db):  # noqa: F811
    mail = _open_alarm(tmp_path, cfg, db, "a.xlsx")
    with patch("flow.notify", mail), patch("core.fy_overlap.sibling_findings",
                                           side_effect=ov.SiblingReadError("is_dir failed")):
        flow._alarm_siblings(cfg, db, DEC)
    assert [e.kind for e in mail.events] == ["ACTION_REQUIRED"]


def test_no_resolved_and_the_incident_stays_open_when_the_count_is_unreadable(  # noqa: F811
        tmp_path, cfg, db, monkeypatch):
    mail = _open_alarm(tmp_path, cfg, db, "a.xlsx")
    _fail_scandir_of(monkeypatch, tmp_path / "src" / TO)
    with patch("flow.notify", mail):
        flow._alarm_siblings(cfg, db, DEC)
    assert [e.kind for e in mail.events] == ["ACTION_REQUIRED"]
    with patch("core.fy_overlap.run_date", return_value=DEC):
        assert nt._incident_closed(_event(f"overlap:unprotected:{TO}", TO), db, cfg, _PRIOR) is False


def test_the_misnamed_incident_stays_open_when_the_parent_cannot_be_listed(  # noqa: F811
        tmp_path, cfg, db, monkeypatch):
    _folder(tmp_path, "FY 27-28")
    _fail_scandir_of(monkeypatch, tmp_path / "src")
    with patch("core.fy_overlap.run_date", return_value=DEC):
        assert nt._incident_closed(_event("overlap:sibling:FY 27-28", None), db, cfg, _PRIOR) is False


def test_the_resolved_mail_itself_is_not_sent_when_the_parent_cannot_be_listed(  # noqa: F811
        tmp_path, cfg, db, monkeypatch):
    sent = []
    _fail_scandir_of(monkeypatch, tmp_path / "src")
    with patch("flow.notify", lambda *a, **k: sent.append(a)):
        flow._send_sibling_resolved(cfg, db, "overlap:sibling:FY 27-28", protected=False)
    assert sent == []


def test_the_dashboard_adds_no_item_when_the_folders_cannot_be_read(tmp_path, cfg, db):  # noqa: F811
    from core import attention

    _folder(tmp_path, TO, "a.xlsx")
    with patch("core.fy_overlap.run_date", return_value=DEC), patch(
            "core.fy_overlap.sibling_findings", side_effect=ov.SiblingReadError("x")):
        items = attention.compute_attention(db, cfg)
    assert not [i for i in items if i["code"].startswith("overlap:")]


# ---- I-4 / N-11 --------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["FY 2023-24 Archive", "FY25-26 old", "FY26-27 - Copy", "FY 2026-27 backup",
                                  "fy 22 23"])
def test_a_misnamed_folder_for_the_configured_or_a_past_year_is_ignored(tmp_path, cfg, name):  # noqa: F811
    _folder(tmp_path, name)
    assert ov.sibling_findings(cfg, [FROM]) == []


@pytest.mark.parametrize("name", ["FY 27-28", "FY2027-28 new", "FY 28 29", "FY_2030-31", "FY27-28 - Copy"])
def test_a_misnamed_folder_for_a_newer_year_is_reported(tmp_path, cfg, name):  # noqa: F811
    _folder(tmp_path, name)
    assert [f["name"] for f in ov.sibling_findings(cfg, [FROM])] == [name]


def test_the_suggested_name_is_never_one_that_already_exists(tmp_path, cfg):  # noqa: F811
    _folder(tmp_path, "FY 27-28")
    _folder(tmp_path, "fy27-28")                       # the right name is taken (empty: no unprotected finding)
    assert ov.sibling_findings(cfg, [FROM]) == []


def test_the_century_wrap_reads_99_00_01_as_newer():
    assert ov._start_year_is_newer("FY 00-01", "FY99-00") is True
    assert ov._start_year_is_newer("FY 98-99", "FY99-00") is False
    assert ov._start_year_is_newer("FY 2100-01", "FY99-00") is True


def test_the_misnamed_resolved_text_says_no_longer_reported_when_the_folder_is_still_there(  # noqa: F811
        tmp_path, db):
    from models.config import AppConfig  # noqa: F401
    from tests.test_ov2_sibling_check import _cfg

    ignoring = _cfg(tmp_path, sibling_ignore=["FY 27-28"])
    _folder(tmp_path, "FY 27-28")
    events = []
    with patch("flow.notify", lambda config, event, db=None: events.append(event)):
        flow._send_sibling_resolved(ignoring, db, "overlap:sibling:FY 27-28", protected=False)
    assert "no longer reported" in events[0].message and "renamed or removed" not in events[0].message
    events.clear()
    (tmp_path / "src" / "FY 27-28").rmdir()
    with patch("flow.notify", lambda config, event, db=None: events.append(event)):
        flow._send_sibling_resolved(ignoring, db, "overlap:sibling:FY 27-28", protected=False)
    assert "renamed or removed" in events[0].message


# ---- N-2 ---------------------------------------------------------------------------------------

def test_the_g3_message_names_the_small_folder_limit_when_that_applied():
    r = guards.g3_mass_delete(15, 50, 1000, 10.0, small_folder_files=100, small_folder_max_deletes=10)
    assert not r.passed
    assert "limit 10 files for a small folder" in r.reason and "limits" not in r.reason
    assert r.metrics["limit_percent"] == 10.0 and r.metrics["limit_files"] == 1000      # metrics kept


def test_the_g3_message_names_the_percent_limits_for_a_big_folder():
    r = guards.g3_mass_delete(500, 1000, 1000, 10.0, small_folder_files=100, small_folder_max_deletes=10)
    assert not r.passed and "limits 1,000 files / 10.0%" in r.reason


# ---- N-7 ---------------------------------------------------------------------------------------

def test_the_default_clock_check_removes_its_throwaway_store(cfg, monkeypatch):  # noqa: F811
    import core.health as health
    from core.run_artefacts import RunArtefacts

    seen = []
    real = RunArtefacts.ephemeral

    def spy(label="adhoc"):
        art = real(label)
        seen.append(art.root)
        return art
    monkeypatch.setattr(RunArtefacts, "ephemeral", staticmethod(spy))
    monkeypatch.setattr(health, "check_clock", lambda **k: "result")
    assert ov._default_clock_check(cfg) == "result"
    assert seen and not seen[0].exists()
    # and also when the check raises
    seen.clear()

    def boom(**k):
        raise RuntimeError("x")
    monkeypatch.setattr(health, "check_clock", boom)
    with pytest.raises(RuntimeError):
        ov._default_clock_check(cfg)
    assert seen and not seen[0].exists()


# ---- N-1 / N-6 texts ---------------------------------------------------------------------------

def test_the_help_texts():
    from core import config_help
    from models.config import GuardsConfig, RolloverConfig

    assert "G2" in GuardsConfig.model_fields["small_folder_max_deletes"].description
    desc = RolloverConfig.model_fields["activation_alert_after_hours"].description
    assert desc.startswith("If next year's folder cannot be set up for backup")
    assert "action-needed email after this many hours" in desc
    assert "action-needed email" in config_help.HELP["rollover.activation_alert_after_hours"][1]
    assert "G2" in config_help.HELP["guards.small_folder_max_deletes"][1]
