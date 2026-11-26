"""Tests for RF-3: blocked email truthfully reports copy-only run results."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from test_a2_interrupted_run_status import _cloud_pipeline, _lan_pipeline, _make_config

import flow
from core import guards, statuses

G3_BLOCK = guards.g3_mass_delete(900, 1000, 50, 10.0)
G1_BLOCK = guards.g1_source_has_files(None, "D:\\src")


def _real_blocked_record(leg: str, results: list[guards.GuardResult]) -> dict:
    try:
        return guards.enforce(leg, results, fy="FY26-27", max_delete=0)
    except guards.GuardBlocked as gb:
        return gb.record


def test_rf3_5_copy_only_note_unit():
    assert statuses.copy_only_note(None) == ""
    assert statuses.copy_only_note({}) == ""
    assert statuses.copy_only_note({"attempted": False}) == ""
    assert statuses.copy_only_note(
        {"attempted": True, "copied": 7, "bytes": 2097152, "deletes_held": 900, "error": None}
    ) == (
        "Copy-only run: 7 file(s) (2.00 MB) were copied to the backup; "
        "no deletions were made, 900 planned deletion(s) are held until you approve."
    )
    assert statuses.copy_only_note(
        {"attempted": True, "copied": None, "bytes": None, "deletes_held": 900, "error": "SyncFailed: exit 5"}
    ) == (
        "Copy-only run was attempted but did not finish (SyncFailed: exit 5); "
        "the number of files copied is unknown; no deletions were made."
    )


def test_rf3_1_6_7_lan_g3_block_copy_only_mail_row_and_metrics():
    rec_g3 = _real_blocked_record("lan", [G3_BLOCK])
    with _lan_pipeline("pre", None) as (record_run, _rec), \
            patch("flow.lan_guards_task", side_effect=guards.GuardBlocked("lan", rec_g3)), \
            patch("flow.lan_sync_task") as copy_task, \
            patch("flow.notify") as notify, \
            patch("flow.ManifestDB") as mdb:
        mdb.from_config.return_value.open_approval_request.return_value = "appr-rf3-lan"
        copy_task.with_options.return_value.return_value = {
            "status": "LAN_COMPLETE",
            "exit_code": 0,
            "stats": {"transfers": 7, "bytes": 2097152, "deletes": 0, "errors": 0, "checks": 1},
        }
        with pytest.raises(guards.GuardBlocked):
            flow._run_lan_pipeline(_make_config(), "run-rf3-lan", "2026-11-25T21:00:00", allow_shutdown=False)

    msg = notify.call_args.args[1].message
    assert "Some files may already have been changed" in msg
    assert "Copy-only run: 7 file(s) (2.00 MB)" in msg
    assert "Nothing was copied or deleted" not in msg
    assert record_run.call_args.args[4] == "LAN_BLOCKED"
    assert record_run.call_args.kwargs["files_copied"] == 7
    assert record_run.call_args.kwargs["bytes_copied"] == 2097152
    ext = json.loads(record_run.call_args.kwargs["extended_metrics"])
    assert ext["guards"]["copy_only"]["attempted"] is True


def test_rf3_2_cloud_g3_block_copy_only_mail():
    rec_g3 = _real_blocked_record("cloud", [G3_BLOCK])
    with _cloud_pipeline("pre", None) as (_record_run, _rec, _alert), \
            patch("flow.cloud_guards_task", side_effect=guards.GuardBlocked("cloud", rec_g3)), \
            patch("flow.cloud_sync_task") as copy_task, \
            patch("flow.notify") as notify, \
            patch("flow.ManifestDB") as mdb:
        mdb.from_config.return_value.open_approval_request.return_value = "appr-rf3-cloud"
        copy_task.with_options.return_value.return_value = {
            "status": "CLOUD_COMPLETE",
            "exit_code": 0,
            "stats": {"transfers": 7, "bytes": 2097152, "deletes": 0, "errors": 0, "checks": 1},
        }
        with pytest.raises(guards.GuardBlocked):
            flow._run_cloud_pipeline(_make_config(), "run-rf3-cloud", "2026-11-25T02:00:00")

    msg = notify.call_args.args[1].message
    assert "Some files may already have been changed" in msg
    assert "Copy-only run: 7 file(s) (2.00 MB)" in msg
    assert "Nothing was copied or deleted" not in msg


def test_rf3_3_lan_copy_only_failure_reports_unknown_in_mail():
    rec_g3 = _real_blocked_record("lan", [G3_BLOCK])
    with _lan_pipeline("pre", None) as (_record_run, _rec), \
            patch("flow.lan_guards_task", side_effect=guards.GuardBlocked("lan", rec_g3)), \
            patch("flow.lan_sync_task") as copy_task, \
            patch("flow.notify") as notify, \
            patch("flow.ManifestDB") as mdb:
        mdb.from_config.return_value.open_approval_request.return_value = "appr-rf3-fail"
        copy_task.with_options.return_value.side_effect = RuntimeError("SyncFailed: exit 5")
        with pytest.raises(guards.GuardBlocked):
            flow._run_lan_pipeline(_make_config(), "run-rf3-fail", "2026-11-25T21:00:00", allow_shutdown=False)

    msg = notify.call_args.args[1].message
    assert "Copy-only run was attempted but did not finish" in msg
    assert "unknown" in msg
    assert "Nothing was copied" not in msg


def test_rf3_4_multi_guard_block_skips_copy_only_and_keeps_nothing_changed():
    rec_g1_g3 = _real_blocked_record("lan", [G1_BLOCK, G3_BLOCK])
    with _lan_pipeline("pre", None) as (_record_run, _rec), \
            patch("flow.lan_guards_task", side_effect=guards.GuardBlocked("lan", rec_g1_g3)), \
            patch("flow.lan_sync_task") as copy_task, \
            patch("flow.notify") as notify, \
            patch("flow.ManifestDB") as mdb:
        mdb.from_config.return_value.open_approval_request.return_value = "appr-rf3-multi"
        with pytest.raises(guards.GuardBlocked):
            flow._run_lan_pipeline(_make_config(), "run-rf3-multi", "2026-11-25T21:00:00", allow_shutdown=False)

    copy_task.with_options.return_value.assert_not_called()
    msg = notify.call_args.args[1].message
    assert "Nothing was copied or deleted." in msg
