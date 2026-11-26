"""Tests for AF-1: is_g3_only_block works on real guards.enforce() / GuardBlocked records."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_a2_interrupted_run_status import _cloud_pipeline, _lan_pipeline, _make_config

import flow
from core import guards


def _enforce_blocked(results: list[guards.GuardResult],
                     approved: frozenset[str] = frozenset()) -> dict:
    with pytest.raises(guards.GuardBlocked) as exc_info:
        guards.enforce("cloud", results, approved_guards=approved)
    return exc_info.value.record


def _g1(ok: bool = True) -> guards.GuardResult:
    return guards.g1_source_has_files("a.txt" if ok else None, "D:\\src")


def _g2(ok: bool = True) -> guards.GuardResult:
    return guards.g2_source_shrink(100 if ok else 10, 100, 50)


def _g3(ok: bool = True) -> guards.GuardResult:
    return guards.g3_mass_delete(0 if ok else 900, 1000, 50, 10.0)


def test_af1_only_g3_fails_returns_true():
    rec = _enforce_blocked([_g1(True), _g3(False)])
    assert guards.is_g3_only_block(rec) is True


def test_af1_g3_and_g1_fail_returns_false():
    rec = _enforce_blocked([_g1(False), _g3(False)])
    assert guards.is_g3_only_block(rec) is False


def test_af1_g3_approved_and_nothing_else_fails_returns_false():
    rec = guards.enforce(
        "cloud", [_g1(True), _g3(False)],
        approved_guards=frozenset({"G3_MASS_DELETE"}),
    )
    assert guards.is_g3_only_block(rec) is False


def test_af1_only_g2_fails_returns_false():
    rec = _enforce_blocked([_g1(True), _g2(False), _g3(True)])
    assert guards.is_g3_only_block(rec) is False


@pytest.mark.parametrize("bad", [None, "not-a-dict", [], {}, {"results": []}, {"results": None}])
def test_af1_none_non_dict_or_empty_returns_false(bad):
    assert guards.is_g3_only_block(bad) is False


def _fake_rclone_copy(seen_cmds: list[list[str]]):
    def fake_run_tool(step, cmd, timeout_s=None, artefacts=None, log_path=None, **_kw):
        seen_cmds.append(list(cmd))
        return SimpleNamespace(
            completed=True, exit_code=0, spawn_error=None, timed_out=False, stalled=False,
            log_text=lambda: '{"level":"info","msg":"Transferred","stats":{"transfers":3,"bytes":1024,"deletes":0,"errors":0,"checks":1}}\n',
            stderr_text=lambda: "",
            stdout_text=lambda: "",
            describe_failure=lambda: "",
        )
    return fake_run_tool


def test_af1_e2e_cloud_g3_only_block_enters_copy_only_via_real_enforce(tmp_path):
    from models.config import AppConfig, CloudConfig, PathsConfig, WolConfig

    src = tmp_path / "FY26-27"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")
    key = tmp_path / "key.json"
    key.write_text("{}", encoding="utf-8")

    cfg = AppConfig(
        paths=PathsConfig(
            source_drive=str(src),
            lan_destination=r"\\nas\share\FY26-27",
            gcs_key_path=str(key),
            database_path=str(tmp_path / "test.db"),
        ),
        wol=WolConfig(enabled=False),
        cloud=CloudConfig(bucket="test-bucket", project_number="123456"),
    )

    real_record = _enforce_blocked([_g1(True), _g3(False)])
    seen_cmds: list[list[str]] = []
    orig_cloud_sync_fn = flow.cloud_sync_task.fn

    with _cloud_pipeline("pre", None) as (record_run, _rec, _alert), \
            patch("flow.cloud_guards_task", side_effect=guards.GuardBlocked("cloud", real_record)), \
            patch("flow.cloud_access_task"), \
            patch("flow.cloud_sync_task") as sync_task, \
            patch("core.cloud_sync.run_tool", side_effect=_fake_rclone_copy(seen_cmds)), \
            patch("flow.notify"), \
            patch("flow.ManifestDB") as mdb:
        sync_task.with_options.return_value.side_effect = orig_cloud_sync_fn
        mdb.from_config.return_value.open_approval_request.return_value = "appr-af1-cloud"
        with pytest.raises(guards.GuardBlocked):
            flow._run_cloud_pipeline(cfg, "run-af1-cloud", "2026-11-25T02:00:00")

    assert len(seen_cmds) == 1
    assert seen_cmds[0][1] == "copy"
    assert "--max-delete" not in seen_cmds[0]
    assert record_run.call_args.args[4] == "CLOUD_BLOCKED"


def test_af1_e2e_lan_g3_only_block_enters_copy_only_via_real_enforce(tmp_path):
    from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, WolConfig

    src = tmp_path / "FY26-27"
    src.mkdir()
    (src / "a.txt").write_text("hi", encoding="utf-8")

    cfg = AppConfig(
        paths=PathsConfig(
            source_drive=str(src),
            lan_destination=r"\\nas\share\FY26-27",
            database_path=str(tmp_path / "test.db"),
        ),
        wol=WolConfig(enabled=False),
        cloud=CloudConfig(enabled=False),
        lan=LanConfig(versions_enabled=False),
    )

    real_record = _enforce_blocked([_g1(True), _g3(False)])
    seen_cmds: list[list[str]] = []
    orig_lan_sync_fn = flow.lan_sync_task.fn

    with _lan_pipeline("pre", None) as (record_run, _rec), \
            patch("flow.lan_guards_task", side_effect=guards.GuardBlocked("lan", real_record)), \
            patch("flow.lan_access_task"), \
            patch("flow.lan_sync_task") as sync_task, \
            patch("core.lan_sync.run_tool", side_effect=_fake_rclone_copy(seen_cmds)), \
            patch("flow.notify"), \
            patch("flow.ManifestDB") as mdb:
        sync_task.with_options.return_value.side_effect = orig_lan_sync_fn
        mdb.from_config.return_value.open_approval_request.return_value = "appr-af1-lan"
        with pytest.raises(guards.GuardBlocked):
            flow._run_lan_pipeline(cfg, "run-af1-lan", "2026-11-25T21:00:00", allow_shutdown=False)

    assert len(seen_cmds) == 1
    assert seen_cmds[0][1] == "copy"
    assert "--delete-after" not in seen_cmds[0]
    assert "--max-delete" not in seen_cmds[0]
    assert record_run.call_args.args[4] == "LAN_BLOCKED"
