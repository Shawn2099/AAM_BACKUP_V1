"""Manual-only LAN: a failed manual run that woke the NAS and left it on shows an amber item, and the failure
mail does not promise a scheduled run (owner decision 3, 2026-11-23)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import flow
from core.attention import AMBER, RED, compute_attention
from test_m3_dashboard_truth import _cfg, _codes, _run, db  # noqa: F401
from tests.test_ov_review_points import env, lan_night  # noqa: F401  (fixtures)


def _config(scheduled, wol=True):
    return SimpleNamespace(lan=SimpleNamespace(enabled=True, scheduled=scheduled,
                                               shutdown_on_all_retries_exhausted=False),
                           cloud=SimpleNamespace(enabled=True, scheduled=True),
                           wol=SimpleNamespace(enabled=wol, server_ip="10.0.0.5",
                                               smb_connect_timeout_seconds=1))


# ── mail text ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("loop", [False, True])
def test_failure_note_says_next_manual_backup_for_a_manual_only_lan(loop):
    note = flow._nas_alert_note(False, loop, _config(scheduled=False))
    assert "next manual backup" in note and "scheduled run" not in note


@pytest.mark.parametrize("loop", [False, True])
def test_failure_note_keeps_the_scheduled_wording_for_a_nightly_lan(loop):
    note = flow._nas_alert_note(False, loop, _config(scheduled=True))
    assert "the next scheduled run will re-sync" in note and "manual" not in note


def test_suspect_text_follows_the_leg_mode():
    manual = flow._failure_alert_text("cloud", "CLOUD_SUSPECT", "x", config=SimpleNamespace(
        cloud=SimpleNamespace(enabled=True, scheduled=False)))
    nightly = flow._failure_alert_text("cloud", "CLOUD_SUSPECT", "x", config=_config(True))
    assert "The next manual backup will re-sync" in manual and "scheduled" not in manual
    assert "The next scheduled run will re-sync" in nightly


# ── the flag ─────────────────────────────────────────────────────────────────────────────────────

def _flag(config, db_obj, votes=((False, "d"),), smb_open=True):
    with patch("flow.ManifestDB.from_config", return_value=db_obj), patch.object(db_obj, "close", lambda: None), \
            patch("core.wol._smb_port_open", return_value=smb_open):
        flow._flag_nas_left_on(config, list(votes), "r1")


def test_left_on_is_recorded_when_a_manual_only_run_failed_and_the_nas_still_answers(db):  # noqa: F811
    _run(db, "r1", "lan", "LAN_FAILED", 1)
    _flag(_config(False), db)
    items = compute_attention(db, _cfg_manual(db))
    left = [i for i in items if i["code"] == "nas_left_on"]
    assert left and left[0]["level"] == AMBER and "left on" in left[0]["message"]
    assert not [i for i in items if i["code"] == "nas_left_on" and i["level"] == RED]


def _cfg_manual(db):
    cfg = _cfg()
    cfg.lan.scheduled = False
    cfg.lan.on_demand_warn_after_days = 30
    cfg.cloud.scheduled = True
    cfg.cloud.on_demand_warn_after_days = 30
    return cfg


@pytest.mark.parametrize("config,votes,smb", [
    (_config(True), ((False, "d"),), True),             # nightly leg: not this feature
    (_config(False, wol=False), ((False, "d"),), True),  # WoL off: the NAS was never woken by us
    (_config(False), ((False, "d"),), False),            # the NAS is not on
    (_config(False), (), True),                          # no folder ran at all
])
def test_left_on_is_not_recorded_otherwise(db, config, votes, smb):  # noqa: F811
    _run(db, "r1", "lan", "LAN_FAILED", 1)
    _flag(config, db, votes, smb)
    assert "nas_left_on" not in {i["code"] for i in compute_attention(db, _cfg_manual(db))}


def test_a_later_run_without_the_flag_clears_the_item(db):  # noqa: F811
    _run(db, "r1", "lan", "LAN_FAILED", 3)
    _flag(_config(False), db)
    _run(db, "r2", "lan", "LAN_COMPLETE", 1)
    assert "nas_left_on" not in {i["code"] for i in compute_attention(db, _cfg_manual(db))}


def test_the_manual_loop_flags_a_nas_that_stays_on(lan_night):  # noqa: F811
    lan_night.env.cfg.lan.scheduled = False
    calls = []
    with patch.object(flow, "_flag_nas_left_on", lambda config, votes, run_id: calls.append(run_id)):
        flow.backup.fn("config.yaml", "lan")
    assert len(calls) == 1 and calls[0].startswith("p11-")
