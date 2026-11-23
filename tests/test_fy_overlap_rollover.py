"""B4 (plan section 4): the April 1 final-copy gate while the FY overlap is active.

Same rig as test_a6_rollover_journal_flow (patched tools, one real manifest DB per rig), with the owner's layout
(E:\\FY26-27 + \\\\nas\\backup\\FY26-27) and an activation record for FY27-28. Copies of the old year are real
run_history rows (the rig mocks the run writer). Everything here is only reached while the overlap is active;
the plain rollover is covered unchanged by test_a6 / test_m2_rollover.
"""

import json
from types import SimpleNamespace

import pytest

from core import approvals as ap
from core import fy_overlap as fo
from core import fy_rollover as fr
from core import guards as g
from core.manifest import ManifestDB
from models.config import RolloverConfig
from tests.test_a6_rollover_journal_flow import _events, _reset_mocks
from tests.test_m2_rollover import _Rig
from unittest.mock import patch

FROM, TO = "FY26-27", "FY27-28"
REC = {"activated_at": "2026-12-31T06:00:00+05:30", "from_fy": FROM, "source_folder": "E:\\FY27-28", "clock": {}}


class _OverlapRig(_Rig):
    """The rollover rig with the owner's layout and an active overlap. The legs return the status they are
    given (failing ones too, no exception). The activation step and the unprotected-files alarm are stubs here
    (their own tests cover them); the ordering test uses the alarm stub."""

    def __init__(self, *, cloud="CLOUD_COMPLETE", lan="LAN_COMPLETE", approved=False, computed=TO,
                 lan_enabled=True, lan_scheduled=True, cloud_scheduled=True, overlap_enabled=True,
                 overlap_record=True):
        super().__init__(computed=computed, cloud=cloud, lan=lan, approved=approved)
        cfg = self.cfg
        cfg.paths.source_drive = "E:\\FY26-27"
        cfg.paths.lan_destination = "\\\\nas\\backup\\FY26-27"
        cfg.rollover = RolloverConfig(overlap_enabled=overlap_enabled)
        cfg.cloud.scheduled = cloud_scheduled
        cfg.lan.enabled = lan_enabled
        cfg.lan.scheduled = lan_scheduled
        if overlap_record:
            self.db.set_overlap_record(TO, dict(REC))
        self.patches += [patch("flow.fy_overlap.activate_if_due", return_value=fo.ALREADY_ACTIVE),
                         patch("flow._alarm_unprotected_new_fy")]

    def _leg(self, leg, status):
        def run(config, run_id, started_at, monotonic_start=None, approval_id=None, **kwargs):
            return {"status": status}
        return run

    def copy(self, leg, started_at, status, fy=FROM):
        """A row of the old year's run history, as a pipeline run would have written it."""
        self.db.insert_run({"run_id": f"old-{leg}-{started_at}", "mode": leg, "started_at": started_at,
                            "status": status, "extended_metrics": json.dumps({"guards": {"fy": fy}})})

    def metrics(self, status="ROLLOVER_COMPLETE"):
        """The extended metrics of the newest rollover row with this status."""
        calls = [c for c in self.m["_record_run"].call_args_list if c.args[4] == status]
        return json.loads(calls[-1].kwargs["extended_metrics"])


def _info(r):
    [info] = [e for e in _events(r) if e.kind == "INFO"]
    return info


def _pending_approval(r):
    return r.db.open_approval_request(
        leg="cloud", blocked_run_id="old-run", fy=FROM,
        guards=[{"guard": "G1", "reason": "x", "metrics": {}}], phrase="APPROVE",
        created_at="2027-03-30T10:00:00+05:30")


# ---- the switch by itself -------------------------------------------------------------------------------

def test_all_scheduled_copies_switch_by_itself_and_the_old_years_approvals_are_superseded():
    with _OverlapRig() as r:
        pending = _pending_approval(r)
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["update_config_yaml"].assert_called_once_with(
            "cfg.yaml", "E:\\", "\\\\nas\\backup", TO, expect_fy=FROM)
        assert r.db.get_approval(pending)["status"] == ap.SUPERSEDED
        assert r.db.overlap_record(TO)["ended_at"]
        r.m["_open_block_request"].assert_not_called()
        assert r.metrics()["final_copy"]["cloud"]["source"] == "check"
        assert "Nothing to do" in _info(r).message


def test_a_failed_nas_final_copy_blocks_and_both_years_stay_active():
    with _OverlapRig(lan="LAN_FAILED") as r:
        with pytest.raises(fr.RolloverError, match="lan"):
            r.run()
        r.m["update_config_yaml"].assert_not_called()
        assert r.db.overlap_record(TO).get("ended_at") is None
        reason = r.m["_open_block_request"].call_args.args[3]["results"][0]["reason"]
        assert "lan=LAN_FAILED" in reason
        assert "both FY26-27 and FY27-28 are backed up every night" in reason
        assert "To finish lan: switch the NAS on" in reason
        assert r.metrics("ROLLOVER_BLOCKED")["final_copy"]["lan"]["outstanding"] is True
        assert r.statuses()[-1] == "ROLLOVER_BLOCKED"


def test_a_manual_nas_copy_after_the_closing_moment_switches_by_itself_and_the_mail_names_its_time():
    with _OverlapRig(lan="LAN_FAILED") as r:
        r.copy("lan", "2027-04-01T14:00:00+05:30", "LAN_COMPLETE")
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["update_config_yaml"].assert_called_once()
        r.m["_open_block_request"].assert_not_called()
        lan = r.metrics()["final_copy"]["lan"]
        assert lan["source"] == "earlier" and lan["done_at"] == "2027-04-01T14:00:00+05:30"
        assert lan["warning"] == "LAN_FAILED"
        message = _info(r).message
        assert "1 Apr 2027, 14:00" in message and "NAS copy of FY26-27 is from" in message
        assert "ended LAN_FAILED" in message


def test_a_copy_from_before_the_closing_moment_does_not_count():
    with _OverlapRig(lan="LAN_FAILED") as r:
        r.copy("lan", "2027-03-31T20:00:00+05:30", "LAN_COMPLETE")
        with pytest.raises(fr.RolloverError):
            r.run()
        r.m["update_config_yaml"].assert_not_called()
        assert r.metrics("ROLLOVER_BLOCKED")["final_copy"]["lan"]["outstanding"] is True


def test_a_nightly_cloud_copy_counts_when_this_mornings_cloud_run_fails_and_the_mail_warns():
    with _OverlapRig(cloud="CLOUD_FAILED") as r:
        r.copy("cloud", "2027-04-01T01:00:00+05:30", "CLOUD_COMPLETE")
        assert r.run() == "ROLLOVER_COMPLETED"
        cloud = r.metrics()["final_copy"]["cloud"]
        assert cloud["source"] == "earlier" and cloud["warning"] == "CLOUD_FAILED"
        assert "1 Apr 2027, 01:00" in _info(r).message


@pytest.mark.parametrize("leg,status", [
    ("cloud", "CLOUD_VERIFY_FAILED"),
    ("cloud", "CLOUD_PARTIAL"),
    ("lan", "LAN_PARTIAL"),
    ("lan", "LAN_FAILED"),
])
def test_only_verified_copies_count_after_the_closing_moment(leg, status):
    with _OverlapRig(cloud="CLOUD_FAILED", lan="LAN_FAILED") as r:
        r.copy(leg, "2027-04-02T02:00:00+05:30", status)
        with pytest.raises(fr.RolloverError):
            r.run()
        assert r.metrics("ROLLOVER_BLOCKED")["final_copy"][leg]["outstanding"] is True


# ---- manual-only legs ------------------------------------------------------------------------------------

def test_an_unscheduled_nas_is_not_run_by_the_check_and_waits_for_a_manual_copy():
    with _OverlapRig(lan_scheduled=False) as r:
        with pytest.raises(fr.RolloverError):
            r.run()
        r.m["_run_lan_pipeline"].assert_not_called()
        r.m["update_config_yaml"].assert_not_called()
        lan = r.metrics("ROLLOVER_BLOCKED")["final_copy"]["lan"]
        assert lan["outstanding"] and lan["status"] == "LAN_MANUAL_FINAL_NEEDED"
        assert "NAS (run by hand)" in lan["hint"]
        # the office runs the NAS by hand on 2 April; the next check switches and still does not run it
        r.copy("lan", "2027-04-02T10:00:00+05:30", "LAN_COMPLETE")
        _reset_mocks(r)
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["_run_lan_pipeline"].assert_not_called()
        r.m["update_config_yaml"].assert_called_once()


def test_with_the_nas_disabled_only_the_cloud_copy_is_required():
    with _OverlapRig(lan_enabled=False) as r:
        assert r.run() == "ROLLOVER_COMPLETED"
        assert r.metrics()["final_copy"]["lan"]["required"] is False
        r.m["_run_lan_pipeline"].assert_not_called()


# ---- JUMP, forced switch -----------------------------------------------------------------------------------

def test_a_jump_of_more_than_one_fy_is_blocked_even_with_copies():
    with _OverlapRig(computed="FY28-29") as r:
        r.db.set_overlap_record("FY28-29", dict(REC))   # the overlap path is taken, the verdict is still a block
        r.copy("cloud", "2027-04-01T01:00:00+05:30", "CLOUD_COMPLETE")
        r.copy("lan", "2027-04-01T02:00:00+05:30", "LAN_COMPLETE")
        with pytest.raises(fr.RolloverError, match="more than one"):
            r.run()
        r.m["update_config_yaml"].assert_not_called()


def test_a_forced_switch_goes_through_without_the_copy_and_names_the_forced_legs():
    with _OverlapRig(lan="LAN_FAILED", approved=True) as r:
        assert r.run(approval_id="a1") == "ROLLOVER_COMPLETED"
        r.m["update_config_yaml"].assert_called_once()
        assert r.metrics()["forced_legs"] == ["lan"]
        assert "Forced: NAS had no verified final copy of FY26-27" in _info(r).message
        assert r.db.overlap_record(TO)["ended_at"]


# ---- the guard text and the alarm ------------------------------------------------------------------------

@pytest.mark.parametrize("final", [
    {"cloud": "CLOUD_COMPLETE", "lan": "LAN_COMPLETE"},
    {"cloud": "CLOUD_COMPLETE", "lan": "LAN_FAILED"},
    {"lan": "LAN_MANUAL_FINAL_NEEDED"},
])
@pytest.mark.parametrize("kind", [fr.FORWARD, fr.JUMP])
def test_the_overlap_keyword_changes_only_the_text_of_g6(final, kind):
    plain = g.g6_fy_rollover(FROM, TO, kind, final)
    text = g.g6_fy_rollover(FROM, TO, kind, final, overlap_active=True,
                            hints={"lan": "switch the NAS on"})
    assert (text.guard, text.verdict, text.fault_code, text.metrics) == \
           (plain.guard, plain.verdict, plain.fault_code, plain.metrics)
    if plain.verdict == g.PASS or kind == fr.JUMP:
        assert text.reason == plain.reason
    else:
        assert "both FY26-27 and FY27-28 are backed up every night" in text.reason
        assert "To finish lan: switch the NAS on." in text.reason


def _tmp_fy_tree(tmp_path, *, overlap_enabled=True):
    root = tmp_path / ("on" if overlap_enabled else "off")
    src = root / "data" / "FY26-27"
    src.mkdir(parents=True)
    nas = root / "nas" / "FY26-27"
    nas.mkdir(parents=True)
    (root / "data" / "FY27-28").mkdir()
    (root / "data" / "FY27-28" / "letter.docx").write_text("saved in the new year", encoding="utf-8")
    return SimpleNamespace(paths=SimpleNamespace(source_drive=str(src), lan_destination=str(nas)),
                           rollover=RolloverConfig(overlap_enabled=overlap_enabled))


def test_the_unprotected_files_alarm_is_silent_while_the_overlap_is_active(tmp_path):
    cfg = _tmp_fy_tree(tmp_path)
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        with patch("core.fy_rollover.get_fy_prefix", return_value=TO):
            assert fr.unprotected_new_fy(cfg, db) is not None         # no overlap: the warning stands (baseline)
            db.set_overlap_record(TO, dict(REC))
            assert fr.unprotected_new_fy(cfg, db) is None             # the folder is backed up: silent
            assert fr.unprotected_new_fy(cfg, db, count=False) is None
            db.set_overlap_record(TO, {**REC, "ended_at": "2027-04-03T06:00:00+05:30"})
            assert fr.unprotected_new_fy(cfg, db)["to_fy"] == TO      # ended: the baseline question again
            cfg_off = _tmp_fy_tree(tmp_path, overlap_enabled=False)
            db.set_overlap_record(TO, dict(REC))
            assert fr.unprotected_new_fy(cfg_off, db) is not None     # overlap switched off: baseline again
    finally:
        db.close()


def test_the_activation_runs_before_the_unprotected_files_alarm():
    with _OverlapRig(overlap_record=False) as r:
        r.order.attach_mock(r.m["activate_if_due"], "activate")
        r.order.attach_mock(r.m["_alarm_unprotected_new_fy"], "alarm")
        r.run()
        names = [c[0] for c in r.order.mock_calls]
        assert names.index("activate") < names.index("alarm")


# ---- helpers ---------------------------------------------------------------------------------------------

def test_no_overlap_record_means_nothing_is_superseded_or_stamped(tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        pending = db.open_approval_request(
            leg="cloud", blocked_run_id="old", fy=FROM, guards=[{"guard": "G1", "reason": "x", "metrics": {}}],
            phrase="APPROVE", created_at="2027-03-30T10:00:00+05:30")
        assert fo.end_overlap(db, FROM, TO, "2027-04-02T06:00:00+05:30") is False
        assert db.get_approval(pending)["status"] == ap.PENDING
    finally:
        db.close()


def test_final_copy_state_reports_the_source_the_hint_and_the_manual_leg(tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        db.insert_run({"run_id": "n-cloud", "mode": "cloud", "started_at": "2027-04-01T01:00:00+05:30",
                       "status": "CLOUD_COMPLETE", "extended_metrics": json.dumps({"guards": {"fy": FROM}})})
        cfg = SimpleNamespace(cloud=SimpleNamespace(enabled=True, scheduled=True),
                              lan=SimpleNamespace(enabled=True, scheduled=False))
        state = fo.final_copy_state(cfg, db, FROM, {"cloud": {"status": "CLOUD_NO_CHANGES_COMPLETE",
                                                              "started_at": "2027-04-02T06:00:00+05:30"}})
        assert state["cloud"]["source"] == "check" and state["cloud"]["outstanding"] is False
        assert state["lan"]["outstanding"] is True and state["lan"]["status"] == "LAN_MANUAL_FINAL_NEEDED"
        assert state["lan"]["hint"].startswith("NAS (run by hand)")
        assert fo.outstanding_legs(state) == ["lan"]
        assert fo.announce_text(state, FROM, []) == ""
    finally:
        db.close()


# ---- B4 review G3: a resume after the switch keeps the forced legs and the copy times ----------------------

def _announce_fails_once(r):
    """The switch mail cannot be sent the first time (the announce step is not blocking)."""
    calls = {"n": 0}

    def notify(config, event, *a, **k):
        if event.kind == "INFO":
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("smtp down")
    r.m["notify"].side_effect = notify


def _resume_next_morning(r):
    from tests.test_a6_rollover_journal_flow import _config_now_points_at_the_new_fy
    _config_now_points_at_the_new_fy(r)
    r.m["update_config_yaml"].return_value = None
    for name in ("_run_cloud_pipeline", "_run_lan_pipeline", "notify", "_record_run"):
        r.m[name].reset_mock()
    r.m["notify"].side_effect = None
    return r.run()


def test_a_resumed_announce_still_names_the_forced_legs():
    with _OverlapRig(lan="LAN_FAILED", approved=True) as r:
        _announce_fails_once(r)
        assert r.run(approval_id="a1") == "ROLLOVER_COMPLETED"
        assert _resumed_ok(r)
        r.m["_run_lan_pipeline"].assert_not_called()           # the final backups are not repeated
        message = _info(r).message
        assert "Forced: NAS had no verified final copy of FY26-27" in message
        assert r.metrics()["forced_legs"] == ["lan"]


def test_a_resumed_announce_still_warns_about_an_earlier_copy_time():
    with _OverlapRig(lan="LAN_FAILED") as r:
        r.copy("lan", "2027-04-01T14:00:00+05:30", "LAN_COMPLETE")
        _announce_fails_once(r)
        assert r.run() == "ROLLOVER_COMPLETED"
        assert _resumed_ok(r)
        message = _info(r).message
        assert "1 Apr 2027, 14:00" in message and "ended LAN_FAILED" in message
        assert r.metrics()["final_copy"]["lan"]["source"] == "earlier"


def _resumed_ok(r):
    return _resume_next_morning(r) == "ROLLOVER_COMPLETED"
