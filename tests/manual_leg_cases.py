"""Shared cases for a manual-only leg (owner 2026-11-23); bound to `lan` in test_manual_lan_*.py and to
`cloud` in test_manual_cloud_*.py. Not collected itself (no test_ prefix)."""
from test_m3_dashboard_truth import _cfg, _codes, _run

from core import manual_leg
from core.attention import AMBER, RED, compute_attention, leg_timing

OTHER = {"lan": "cloud", "cloud": "lan"}
OK = {"lan": "LAN_COMPLETE", "cloud": "CLOUD_COMPLETE"}
BAD = {"lan": "LAN_FAILED", "cloud": "CLOUD_FAILED"}


def cfg_for(leg, scheduled=False, enabled=True, warn=30):
    cfg = _cfg()
    sec = getattr(cfg, leg)
    sec.enabled = enabled
    sec.scheduled = scheduled
    sec.on_demand_warn_after_days = warn
    other = getattr(cfg, OTHER[leg])
    other.scheduled = True
    other.on_demand_warn_after_days = 30
    return cfg


def _other_fresh(db, leg):
    _run(db, "o", OTHER[leg], OK[OTHER[leg]], 1)


def stale_10_days_is_quiet(db, leg):
    _run(db, "m", leg, OK[leg], 10 * 24)
    _other_fresh(db, leg)
    assert compute_attention(db, cfg_for(leg)) == []


def nightly_leg_with_same_history_is_red(db, leg):
    _run(db, "m", leg, OK[leg], 10 * 24)
    _other_fresh(db, leg)
    items = compute_attention(db, cfg_for(leg, scheduled=True))
    assert f"stale:FY26-27:{leg}" in _codes(items, RED)


def older_than_warn_days_is_amber_only(db, leg):
    _run(db, "m", leg, OK[leg], 40 * 24)
    _other_fresh(db, leg)
    items = compute_attention(db, cfg_for(leg))
    assert _codes(items) == {f"manual_old:{leg}"}
    assert _codes(items, AMBER) == {f"manual_old:{leg}"}
    assert "40 days ago" in items[0]["message"]


def warn_days_key_is_used(db, leg):
    _run(db, "m", leg, OK[leg], 10 * 24)
    _other_fresh(db, leg)
    assert _codes(compute_attention(db, cfg_for(leg, warn=5))) == {f"manual_old:{leg}"}


def never_backed_up_is_amber_and_says_so(db, leg):
    _other_fresh(db, leg)
    items = compute_attention(db, cfg_for(leg))
    assert _codes(items, RED) == set() and _codes(items) == {f"manual_old:{leg}"}
    assert "no successful backup recorded yet" in items[0]["message"]


def a_failed_manual_run_is_still_real(db, leg):
    _run(db, "m", leg, OK[leg], 10 * 24)
    _run(db, "b", leg, BAD[leg], 1)
    _other_fresh(db, leg)
    assert f"last_run:FY26-27:{leg}" in _codes(compute_attention(db, cfg_for(leg)), RED)


def both_manual_gives_one_amber_line(db, leg):
    cfg = cfg_for(leg)
    getattr(cfg, OTHER[leg]).scheduled = False
    _run(db, "a", "lan", "LAN_COMPLETE", 2)
    _run(db, "b", "cloud", "CLOUD_COMPLETE", 2)
    items = compute_attention(db, cfg)
    assert _codes(items) == {"manual:none_automatic"} and _codes(items, AMBER) == _codes(items)
    assert manual_leg.nothing_automatic(cfg) is True


def one_nightly_leg_means_something_is_automatic(db, leg):
    assert manual_leg.nothing_automatic(cfg_for(leg)) is False


def timing_has_no_overdue_for_manual(db, leg):
    _run(db, "m", leg, OK[leg], 10 * 24)
    t = leg_timing(db, cfg_for(leg), leg)
    assert t["state"] == "manual" and t["due_at"] is None and t["overdue_at"] is None


def mode_line_texts(db, leg):
    name = manual_leg.LEG_NAME[leg]
    _run(db, "m", leg, OK[leg], 10 * 24)
    line = manual_leg.mode_line(cfg_for(leg), db, leg)
    assert line["mode"] == "manual" and line["soft_warn"] is False
    assert line["text"].startswith(f"{name}: manual only, last backup ")
    assert line["text"].endswith(", 10 days ago")
    off = manual_leg.mode_line(cfg_for(leg, enabled=False), db, leg)
    assert off == {"mode": "off", "text": f"{name}: off", "soft_warn": False}
    assert manual_leg.mode_line(cfg_for(leg, scheduled=True), db, leg) is None


def health_is_200_for_stale_manual(client, db, leg):
    _run(db, "m", leg, OK[leg], 10 * 24)
    _other_fresh(db, leg)
    assert client.get("/health").status_code == 200


def status_shows_mode(client, db, leg):
    _run(db, "m", leg, OK[leg], 3 * 24)
    body = client.get("/status").json()
    assert body[leg]["mode"]["mode"] == "manual" and "3 days ago" in body[leg]["mode"]["text"]
    assert body[leg]["timing"]["state"] == "manual"
    assert body[OTHER[leg]]["mode"]["text"] == ""


def serve_registers_no_cron(leg):
    from serve import _nightly_schedules
    assert _nightly_schedules(cfg_for(leg), leg, "0 1 * * *", "Asia/Kolkata") == []
    assert len(_nightly_schedules(cfg_for(leg, scheduled=True), leg, "0 1 * * *", "Asia/Kolkata")) == 1
