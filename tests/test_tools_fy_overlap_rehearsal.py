"""tools/fy_overlap_rehearsal.py (OV-11.5): the safety checks, with fakes. Nothing real is touched."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import fy_overlap_rehearsal as fr  # noqa: E402

from models.config import AppConfig  # noqa: E402

NAS = r"\\nas\backup"


def _raw(tmp_path, source=None, lan=None, **over):
    raw = {
        "paths": {"source_drive": source or r"E:\FY26-27", "lan_destination": lan or NAS + r"\FY26-27",
                  "runtime_dir": str(tmp_path / "runtime"), "gcs_key_path": str(tmp_path / "key.json")},
        "cloud": {"enabled": True, "bucket": "aam-prod-bucket", "project_number": "1"},
        "wol": {"enabled": False},
    }
    raw.update(over)
    return raw


def _cfg(tmp_path, **kw):
    return AppConfig(**_raw(tmp_path, **kw))


def _plan(tmp_path):
    return fr.build_plan(_cfg(tmp_path), tmp_path / "scratch")


NOON = datetime(2026, 11, 21, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))      # between 10:00 SUN-less and 18:00 cloud


def _checks(tmp_path, **kw):
    plan = kw.pop("plan", None) or _plan(tmp_path)
    args = dict(now=NOON, min_gap_minutes=90, read_lock=lambda p: (False, 1), get_active_slots=lambda: 0,
                path_exists=lambda p: False, cloud_prefix_exists=lambda b, p: False,
                nas_share_reachable=lambda p: True)
    args.update(kw)
    return fr.run_safety_checks(_cfg(tmp_path), plan, **args)


def test_all_clear_has_no_problems(tmp_path):
    assert _checks(tmp_path) == []


def test_plan_uses_only_rehearsal_names(tmp_path):
    plan = _plan(tmp_path)
    assert plan.nas_folders == {"FY90-91": NAS + r"\FY90-91", "FY91-92": NAS + r"\FY91-92"}
    assert set(plan.cloud_prefixes.values()) == {"FY90-91", "FY91-92"}
    assert all(fr.is_rehearsal_path(p, parent=NAS) for p in plan.nas_folders.values())
    assert all(str(p).startswith(str(plan.scratch)) for p in plan.sources.values())


def test_refuses_when_production_is_a_rehearsal_year(tmp_path):
    cfg = _cfg(tmp_path, source=r"E:\FY90-91", lan=NAS + r"\FY90-91")
    with pytest.raises(fr.Refusal):
        fr.build_plan(cfg, tmp_path / "s")


def test_refuses_when_a_leg_or_overlap_is_off(tmp_path):
    with pytest.raises(fr.Refusal):
        fr.build_plan(_cfg(tmp_path, cloud={"enabled": False}), tmp_path / "s")
    with pytest.raises(fr.Refusal):
        fr.build_plan(_cfg(tmp_path, rollover={"overlap_enabled": False}), tmp_path / "s")


def test_refuses_on_any_production_lock(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.paths.backup_lock_path.parent.mkdir(parents=True)
    cfg.paths.backup_lock_path.write_text("123:1.0")
    live = _checks(tmp_path, read_lock=lambda p: (True, 123))
    stale = _checks(tmp_path, read_lock=lambda p: (False, 123))
    broken = _checks(tmp_path, read_lock=lambda p: (_ for _ in ()).throw(OSError("denied")))
    assert any("a backup is running" in p for p in live)
    assert any("stale lock" in p for p in stale)
    assert any("cannot be read" in p for p in broken)


def test_refuses_when_slot_taken_or_unreadable(tmp_path):
    assert any("is taken" in p for p in _checks(tmp_path, get_active_slots=lambda: 1))
    assert any("does not exist" in p for p in _checks(tmp_path, get_active_slots=lambda: None))

    def boom():
        raise ConnectionError("no api")
    assert any("could not be read" in p for p in _checks(tmp_path, get_active_slots=boom))


def test_refuses_when_a_scheduled_job_is_close(tmp_path):
    cloud_soon = datetime(2026, 11, 21, 17, 0, tzinfo=ZoneInfo("Asia/Kolkata"))      # cloud cron 18:00
    assert any("cloud job" in p for p in _checks(tmp_path, now=cloud_soon))
    assert _checks(tmp_path, now=cloud_soon, min_gap_minutes=30) == []


def test_refuses_leftovers_and_unreachable_nas(tmp_path):
    plan = _plan(tmp_path)
    assert any("already exists" in p for p in _checks(tmp_path, plan=plan, path_exists=lambda p: p.endswith("FY91-92")))
    assert any("already holds objects" in p for p in _checks(tmp_path, plan=plan, cloud_prefix_exists=lambda b, p: True))
    assert any("cannot be reached" in p for p in _checks(tmp_path, plan=plan, nas_share_reachable=lambda p: False))

    def boom(b, p):
        raise RuntimeError("rclone")
    assert any("could not be checked" in p for p in _checks(tmp_path, plan=plan, cloud_prefix_exists=boom))


def test_sandbox_config_is_isolated_and_never_shuts_down(tmp_path):
    prod = _cfg(tmp_path)
    plan = fr.build_plan(prod, tmp_path / "scratch")
    raw = _raw(tmp_path)
    raw["lan"] = {"shutdown_after_backup": True}
    sandbox = AppConfig(**fr.build_sandbox_raw(raw, plan))
    assert fr.check_sandbox(sandbox, prod, plan) == []
    assert sandbox.lan.shutdown_after_backup is False and sandbox.lan.shutdown_on_all_retries_exhausted is False
    assert sandbox.paths.database_path != prod.paths.database_path
    assert sandbox.paths.backup_lock_path != prod.paths.backup_lock_path
    assert sandbox.paths.source_drive.endswith("FY90-91") and sandbox.cloud.bucket == prod.cloud.bucket
    # the production raw mapping is never mutated
    assert raw["lan"]["shutdown_after_backup"] is True and raw["paths"]["source_drive"] == r"E:\FY26-27"


def test_check_sandbox_catches_shared_state_and_shutdown(tmp_path):
    prod = _cfg(tmp_path)
    plan = fr.build_plan(prod, tmp_path / "scratch")
    bad = prod.model_copy(deep=True)            # production itself as the "sandbox"
    problems = fr.check_sandbox(bad, prod, plan)
    assert any("production manifest.db" in p for p in problems)
    assert any("production backup.lock" in p for p in problems)
    assert any("shutdown" in p for p in problems)


def test_delete_guards_only_accept_rehearsal_targets(tmp_path):
    plan = _plan(tmp_path)
    for bad in (NAS, NAS + r"\FY26-27", NAS + r"\FY90-91\sub", r"\\other\share\FY90-91", "E:\\FY90-91x"):
        assert not fr.is_rehearsal_path(bad, parent=NAS), bad
        with pytest.raises(fr.Refusal):
            fr.remove_nas_folder(bad, plan)
    for bad in ("", "FY26-27", "FY90-91/x", "FY90-91/", "*"):
        if bad == "FY90-91/":
            continue      # a trailing slash is the same prefix
        assert not fr.is_rehearsal_prefix(bad), bad
    assert fr.is_rehearsal_prefix("FY91-92")
    with pytest.raises(fr.Refusal):
        fr._cloud_purge(_cfg(tmp_path), "b", "FY26-27")


def test_dry_run_changes_nothing_and_prints_the_plan(tmp_path, monkeypatch, capsys):
    import yaml
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(_raw(tmp_path)), encoding="utf-8")
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(fr, "run_safety_checks", lambda *a, **k: [])
    called = []
    monkeypatch.setattr(fr, "execute", lambda *a, **k: called.append(1))
    monkeypatch.setattr(fr, "cleanup", lambda *a, **k: called.append(2))
    rc = fr.main(["--config", str(cfg_file), "--scratch", str(scratch)])
    out = capsys.readouterr().out
    assert rc == 0 and not called and not scratch.exists()
    assert "DRY RUN" in out and "FY91-92" in out and "WOULD" in out


def test_failed_safety_check_blocks_even_with_execute(tmp_path, monkeypatch, capsys):
    import yaml
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(_raw(tmp_path)), encoding="utf-8")
    monkeypatch.setattr(fr, "run_safety_checks", lambda *a, **k: ["a backup is running"])
    called = []
    monkeypatch.setattr(fr, "execute", lambda *a, **k: called.append(1))
    rc = fr.main(["--execute", "--config", str(cfg_file), "--scratch", str(tmp_path / "scratch")])
    assert rc == 2 and not called
    assert "would NOT run" in capsys.readouterr().out
