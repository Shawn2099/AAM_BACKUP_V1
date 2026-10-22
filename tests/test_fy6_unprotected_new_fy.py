"""FY-6 (plan docs/PLAN_FY_FOLDERS_2026-10-22.md): files saved in the NEW financial year's folder before the
rollover switch are on one disk only (the backups still read the old folder). The daily check and the dashboard
say so loudly; the placeholder file does not count; the alarm also fires on the days the rollover is blocked."""

import os
from unittest.mock import patch

import pytest

from core import fy_rollover as fr
from core import faults
from core.attention import RED, compute_attention
from core.manifest import ManifestDB
from models.config import AppConfig
from test_support import ui_login

FROM, TO = "FY26-27", "FY27-28"
PLACEHOLDER = "README_FY.txt"


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "src" / FROM).mkdir(parents=True)
    return AppConfig(
        firm_name="AAM",
        paths={"source_drive": str(tmp_path / "src" / FROM), "lan_destination": f"\\\\nas\\share\\{FROM}",
               "runtime_dir": str(tmp_path / "rt")},
        wol={"enabled": False}, cloud={"enabled": False},
        dashboard={"public_url": "http://backup-server:8080", **ui_login.DASHBOARD},
        notifications={"smtp_host": "127.0.0.1", "smtp_port": 25, "sender": "b@firm.example",
                       "recipients": ["ops@firm.example"], "smtp_security": "none", "smtp_auth": False})


@pytest.fixture
def db(tmp_path):
    d = ManifestDB(str(tmp_path / "m.db"))
    yield d
    d.close()


def _new_folder(tmp_path, *files):
    folder = tmp_path / "src" / TO
    folder.mkdir(parents=True, exist_ok=True)
    for rel in files:
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return folder


def _today(fy):
    return patch("core.fy_rollover.get_fy_prefix", return_value=fy)


# ── the counter ───────────────────────────────────────────────────────────────

def test_a_missing_folder_is_none_and_the_placeholder_alone_is_zero(tmp_path, cfg):
    src = cfg.paths.source_drive
    assert fr.new_fy_file_count(src, TO, PLACEHOLDER) is None
    _new_folder(tmp_path, PLACEHOLDER, "desktop.ini")
    assert fr.new_fy_file_count(src, TO, PLACEHOLDER) == 0


def test_real_files_at_any_depth_are_counted(tmp_path, cfg):
    _new_folder(tmp_path, PLACEHOLDER, "a.xlsx", "clients/b.pdf", "clients/2026/c.pdf")
    assert fr.new_fy_file_count(cfg.paths.source_drive, TO, PLACEHOLDER) == 3


# ── when is it unprotected? ───────────────────────────────────────────────────

def test_a_real_file_in_the_next_year_is_unprotected(tmp_path, cfg, db):
    _new_folder(tmp_path, PLACEHOLDER, "a.xlsx", "b.xlsx")
    with _today(TO):
        found = fr.unprotected_new_fy(cfg, db)
    assert found == {"from_fy": FROM, "to_fy": TO, "files": 2}


@pytest.mark.parametrize("fy, files", [(FROM, ["a.xlsx"]), (TO, [PLACEHOLDER])])
def test_nothing_to_say_when_the_year_is_current_or_only_the_placeholder_exists(tmp_path, cfg, db, fy, files):
    _new_folder(tmp_path, *files)
    with _today(fy):
        assert fr.unprotected_new_fy(cfg, db) is None


def test_a_clock_behind_or_a_jump_is_not_an_unprotected_next_year(tmp_path, cfg, db):
    (tmp_path / "src" / "FY28-29").mkdir()
    (tmp_path / "src" / "FY28-29" / "a.xlsx").write_text("x", encoding="utf-8")
    with _today("FY28-29"):                       # two years ahead: a jump, G6 asks a human
        assert fr.unprotected_new_fy(cfg, db) is None
    _new_folder(tmp_path, "a.xlsx")
    with _today("FY25-26"):                       # clock behind
        assert fr.unprotected_new_fy(cfg, db) is None


def test_it_ends_when_the_switch_is_done(tmp_path, cfg, db):
    _new_folder(tmp_path, "a.xlsx")
    cfg.paths.source_drive = str(tmp_path / "src" / TO)          # the config now points at the new year
    cfg.paths.lan_destination = f"\\\\nas\\share\\{TO}"
    with _today(TO):
        assert fr.unprotected_new_fy(cfg, db) is None


# ── the dashboard ─────────────────────────────────────────────────────────────

def test_the_dashboard_shows_a_red_card_without_walking_the_tree_for_a_count(tmp_path, cfg, db):
    _new_folder(tmp_path, "a.xlsx", "b.xlsx", "c.xlsx")
    with _today(TO):
        items = compute_attention(db, cfg)
    [item] = [i for i in items if i["code"] == f"rollover:unprotected:{TO}"]
    assert item["level"] == RED
    assert TO in item["message"] and "not backed up" in item["message"].lower()


def test_no_card_when_nothing_is_unprotected(tmp_path, cfg, db):
    with _today(TO):
        assert not [i for i in compute_attention(db, cfg) if i["code"].startswith("rollover:unprotected")]


# ── the mail ──────────────────────────────────────────────────────────────────

def test_the_fault_code_exists_and_is_an_action():
    f = faults.get("AAM-ROL-06")
    assert f.code == "AAM-ROL-06" and f.severity == faults.ACTION


def test_the_daily_check_alarms_even_on_a_day_the_rollover_is_blocked(tmp_path, cfg, db):
    import flow

    _new_folder(tmp_path, "a.xlsx")
    sent = []
    with _today(TO), patch("flow.notify", side_effect=lambda c, e, **kw: sent.append(e)):
        flow._alarm_unprotected_new_fy(cfg, db)
    [event] = sent
    assert event.key == f"rollover:unprotected:{TO}" and event.kind == "ACTION_REQUIRED"
    assert event.fault_code == "AAM-ROL-06" and "1" in event.message and FROM in event.message
    assert event.cooldown_hours == cfg.alerts.stale_repeat_hours


def test_no_mail_when_all_is_protected_and_a_failure_to_check_never_raises(tmp_path, cfg, db):
    import flow

    sent = []
    with _today(TO), patch("flow.notify", side_effect=lambda c, e, **kw: sent.append(e)):
        flow._alarm_unprotected_new_fy(cfg, db)
        assert sent == []
        with patch("core.fy_rollover.unprotected_new_fy", side_effect=OSError("disk")):
            flow._alarm_unprotected_new_fy(cfg, db)      # must not raise
    assert sent == []


def test_the_check_runs_at_the_top_of_the_daily_flow_before_anything_can_raise():
    import inspect

    import flow

    src = inspect.getsource(flow.rollover_check_flow.fn)
    assert src.index("_alarm_unprotected_new_fy") < src.index("_rollover_resume")


def _record(db, key, status="SENT", kind="ACTION_REQUIRED"):
    db.record_notification({
        "event_key": key, "kind": kind, "mode": "rollover", "subject": "s", "status": status, "error": None,
        "attempts": 1, "created_at": "2026-04-01T06:00:00+00:00", "fault_code": "AAM-ROL-06",
        "message_id": None, "thread_id": None, "attachments_rung": None, "audience": "alerts",
        "recipients": None})


def _after_the_switch(tmp_path, cfg):
    cfg.paths.source_drive = str(tmp_path / "src" / TO)
    cfg.paths.lan_destination = f"\\nas\share\{TO}"


def test_a_resolved_mail_follows_once_after_a_sent_alarm(tmp_path, cfg, db):
    import flow

    _record(db, f"rollover:unprotected:{TO}")
    _after_the_switch(tmp_path, cfg)
    sent = []
    with _today(TO), patch("flow.notify", side_effect=lambda c, e, **kw: sent.append(e)):
        flow._alarm_unprotected_new_fy(cfg, db)
        [event] = sent
        assert event.kind == "RECOVERED" and event.key == f"recovered:rollover:{TO}"
        assert event.resolved_code == "AAM-ROL-06"
        _record(db, f"recovered:rollover:{TO}", kind="RECOVERED")
        flow._alarm_unprotected_new_fy(cfg, db)
    assert len(sent) == 1                                  # never again


def test_no_resolved_mail_when_the_office_was_never_mailed(tmp_path, cfg, db):
    import flow

    _record(db, f"rollover:unprotected:{TO}", status="FAILED")       # the mail never went out
    _after_the_switch(tmp_path, cfg)
    sent = []
    with _today(TO), patch("flow.notify", side_effect=lambda c, e, **kw: sent.append(e)):
        flow._alarm_unprotected_new_fy(cfg, db)
    assert sent == []


# ── FY-14: presence only on the dashboard, junction-safe count in the mail ────

def _junction(tmp_path, link: str, target):
    import subprocess

    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("cannot create a junction here")


def test_a_junction_inside_the_new_folder_is_not_counted_or_followed(tmp_path, cfg):
    folder = _new_folder(tmp_path, "a.xlsx")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "big1.dat").write_text("x", encoding="utf-8")
    (outside / "big2.dat").write_text("x", encoding="utf-8")
    _junction(tmp_path, folder / "linked", outside)
    assert fr.new_fy_file_count(cfg.paths.source_drive, TO, PLACEHOLDER) == 1


def test_presence_check_stops_at_the_first_file_and_never_walks_the_tree(tmp_path, cfg, monkeypatch):
    _new_folder(tmp_path, PLACEHOLDER, "clients/2026/c.pdf")
    monkeypatch.setattr(fr.os, "walk", lambda *a, **k: (_ for _ in ()).throw(AssertionError("walked")))
    assert fr.new_fy_has_files(cfg.paths.source_drive, TO, PLACEHOLDER) is True
    assert fr.new_fy_has_files(cfg.paths.source_drive, "FY99-00", PLACEHOLDER) is False      # no such folder


def test_a_new_folder_that_is_itself_a_link_does_not_count(tmp_path, cfg, monkeypatch):
    _new_folder(tmp_path, "a.xlsx")
    monkeypatch.setattr(fr.os.path, "isjunction", lambda p: str(p).endswith(TO), raising=False)
    assert fr.new_fy_has_files(cfg.paths.source_drive, TO, PLACEHOLDER) is False
    assert fr.new_fy_file_count(cfg.paths.source_drive, TO, PLACEHOLDER) is None


def test_the_dashboard_asks_for_presence_only_and_the_mail_for_the_count(tmp_path, cfg, db):
    _new_folder(tmp_path, "a.xlsx", "b.xlsx")
    with _today(TO):
        quick = fr.unprotected_new_fy(cfg, db, count=False)
        exact = fr.unprotected_new_fy(cfg, db)
    assert quick["files"] is None and exact["files"] == 2
    assert "file(s)" not in fr.unprotected_message(quick) and "2 file(s)" in fr.unprotected_message(exact)
    assert "NOT backed up" in fr.unprotected_message(quick)
