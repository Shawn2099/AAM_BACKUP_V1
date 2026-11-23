"""Manual-only LAN leg: attention, timing, mode line, schedule registration (owner 2026-11-23)."""
import manual_leg_cases as c
from test_m3_dashboard_truth import client, db  # noqa: F401

LEG = "lan"


def test_manual_lan_stale_10_days_is_quiet(db):  # noqa: F811
    c.stale_10_days_is_quiet(db, LEG)


def test_manual_lan_nightly_leg_with_same_history_is_red(db):  # noqa: F811
    c.nightly_leg_with_same_history_is_red(db, LEG)


def test_manual_lan_older_than_warn_days_is_amber_only(db):  # noqa: F811
    c.older_than_warn_days_is_amber_only(db, LEG)


def test_manual_lan_warn_days_key_is_used(db):  # noqa: F811
    c.warn_days_key_is_used(db, LEG)


def test_manual_lan_never_backed_up_is_amber_and_says_so(db):  # noqa: F811
    c.never_backed_up_is_amber_and_says_so(db, LEG)


def test_manual_lan_a_failed_manual_run_is_still_real(db):  # noqa: F811
    c.a_failed_manual_run_is_still_real(db, LEG)


def test_manual_lan_both_manual_gives_one_amber_line(db):  # noqa: F811
    c.both_manual_gives_one_amber_line(db, LEG)


def test_manual_lan_one_nightly_leg_means_something_is_automatic(db):  # noqa: F811
    c.one_nightly_leg_means_something_is_automatic(db, LEG)


def test_manual_lan_timing_has_no_overdue_for_manual(db):  # noqa: F811
    c.timing_has_no_overdue_for_manual(db, LEG)


def test_manual_lan_mode_line_texts(db):  # noqa: F811
    c.mode_line_texts(db, LEG)


def test_manual_lan_health_is_200_for_stale(client, db, monkeypatch):  # noqa: F811
    import ui
    monkeypatch.setattr(ui, '_cfg', lambda: c.cfg_for(LEG))
    c.health_is_200_for_stale_manual(client, db, LEG)


def test_manual_lan_status_shows_mode(client, db, monkeypatch):  # noqa: F811
    import ui
    monkeypatch.setattr(ui, '_cfg', lambda: c.cfg_for(LEG))
    c.status_shows_mode(client, db, LEG)


def test_manual_lan_serve_registers_no_cron():
    c.serve_registers_no_cron(LEG)
