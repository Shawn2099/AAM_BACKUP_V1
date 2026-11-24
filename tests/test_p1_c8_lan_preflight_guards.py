"""Plan P1 chunk C8: LAN preflight, guards and access (docs/PLAN_P1_RCLONE_LAN_2026-11-25.md
§2 steps 3-5, §4; test matrix rows G-1..G-12).

These tests describe the NEW shapes and fail until C8 lands:
  flow.lan_preflight_task(config, artefacts_dir)
      -> plan dict (rclone dry-run via lan_sync.plan_lan_mirror) + canary_present,
         or {"source_empty": True, "canary_present": ...} with NO dry-run (G1 speaks first).
  flow.lan_guards_task(config, fy, plan, approval_id=None, run_id=None, already_deleted=0)
      -> no destination_files parameter; reads plan["source_files"], plan["dest_files"],
         plan["planned_deletes"], plan["canary_present"]; returns max_delete.
  flow.lan_access_task(config, plan, run_id)
      -> free space uses plan["planned_bytes"]; None -> AAM-NAS-05 ("rclone dry-run byte count").

FAIL (infrastructure) conditions raise a RuntimeError carrying a core.faults code
(faults.coded); a guard verdict raises core.guards.GuardBlocked. A FAIL is never a BLOCK.
"""

import inspect
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pendulum
import pytest

import flow
from core import faults
from core import guards as g
from core.run_artefacts import RunArtefacts
from models.config import GuardsConfig, LanConfig
from test_support.fake_tools import fake_rclone_lan

FIXTURES = Path(__file__).resolve().parents[1] / "test_support" / "rclone_logs"
RUN_ID = "3f2a9c1e-77aa-4c6b-9d11-0a1b2c3d4e5f-lan"
STARTED = pendulum.datetime(2026, 11, 25, 9, 0, tz="Asia/Kolkata")
FY = "FY26-27"
UNC_DEST = r"\\10.10.151.53\backup\FY26-27"
NAS_IPS = {"10.10.151.53"}
LOCAL_IPS = {"127.0.0.1", "10.0.0.5"}
CANARY = ".AAM_TARGET_MOUNTED"


# ── helpers ───────────────────────────────────────────────────────────────

def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _artefacts_dir(tmp_path) -> str:
    return str(RunArtefacts(tmp_path / "runlogs", RUN_ID, started=STARTED).dir)


def _config(tmp_path, dest=None, source_has_files=True):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    if source_has_files:
        (src / "ledger.xlsx").write_text("x", encoding="utf-8")
    cfg = MagicMock()
    cfg.paths.source_drive = str(src)
    cfg.paths.lan_destination = dest if dest is not None else str(tmp_path / "dest")
    cfg.lan = LanConfig()
    cfg.wol.smb_connect_timeout_seconds = 1.0   # WolConfig() needs a NAS MAC; only this field is read
    # cloud is not used by the LAN preflight (C8); stubbed so no test depends on a bucket name
    cfg.cloud.modify_window_seconds = 2
    cfg.cloud.retry_count = 3
    cfg.cloud.sync_retries_sleep_seconds = 1
    cfg.cloud.error_tail_lines = 100
    cfg.guards = GuardsConfig()
    return cfg


def _canary_dest(tmp_path) -> str:
    dest = tmp_path / "dest"
    dest.mkdir(exist_ok=True)
    (dest / CANARY).write_text("canary", encoding="utf-8")
    return str(dest)


def _clean_spec() -> dict:
    """C0b fixtures: 12 source files, empty destination, 13062 bytes to copy."""
    return {"exit_code": 0, "log": _fixture("lan_clean_dryrun.jsonl"),
            "combined": _fixture("lan_clean_combined.txt")}


def _plan(**overrides) -> dict:
    plan = {"ok": True, "error": None, "exit_code": 0,
            "source_files": 1000, "dest_files": 1000, "planned_deletes": 0,
            "moves": 0, "planned_copies": 0, "planned_bytes": 0,
            "canary_present": True}
    plan.update(overrides)
    return plan


def _guards(cfg, plan, *, baseline=None, resolve=frozenset(NAS_IPS), approval_id=None,
            already_deleted=0):
    db = MagicMock()
    db.guard_baseline.return_value = baseline
    with ExitStack() as st:
        st.enter_context(patch("flow.ManifestDB")).from_config.return_value = db
        st.enter_context(patch("flow.guards.resolve_host",
                               return_value=None if resolve is None else set(resolve)))
        st.enter_context(patch("flow.guards.local_addresses", return_value=set(LOCAL_IPS)))
        return flow.lan_guards_task.fn(cfg, FY, plan, approval_id=approval_id, run_id=RUN_ID,
                                       already_deleted=already_deleted)


def _records(guard_block_exc) -> dict:
    return {r["guard"]: r for r in guard_block_exc.record["results"]}


def _verdict(rec: dict, guard: str) -> str:
    return {r["guard"]: r["verdict"] for r in rec["results"]}[guard]


def _assert_fail(excinfo, code: str) -> None:
    """A measurement/infrastructure FAIL: coded, and never a guard BLOCK."""
    assert not isinstance(excinfo.value, g.GuardBlocked), "a FAIL must not be a guard BLOCK"
    assert faults.code_of(excinfo.value) == code


# ── G-1 .. G-4, G-2(b): flow.lan_preflight_task ───────────────────────────

def test_g1_smb_unreachable_fails_coded_and_never_calls_rclone(tmp_path):
    cfg = _config(tmp_path, dest=UNC_DEST)
    with patch("core.lan_preflight._is_smb_reachable", return_value=False), \
         fake_rclone_lan({"rclone-lan-plan": _clean_spec()}) as fake:
        with pytest.raises(RuntimeError) as exc:
            flow.lan_preflight_task.fn(cfg, _artefacts_dir(tmp_path))
    _assert_fail(exc, "AAM-NAS-01")
    assert len(fake) == 0


def test_g2b_canary_is_file_oserror_fails_coded_nas_01(tmp_path):
    cfg = _config(tmp_path, dest=_canary_dest(tmp_path))
    real_is_file = Path.is_file

    def unreadable_canary(self):
        if self.name == CANARY:
            raise OSError("access denied")
        return real_is_file(self)

    with patch.object(Path, "is_file", unreadable_canary), \
         fake_rclone_lan({"rclone-lan-plan": _clean_spec()}) as fake:
        with pytest.raises(RuntimeError) as exc:
            flow.lan_preflight_task.fn(cfg, _artefacts_dir(tmp_path))
    _assert_fail(exc, "AAM-NAS-01")
    assert len(fake) == 0


def test_g3_empty_source_returns_source_empty_with_no_dry_run(tmp_path):
    cfg = _config(tmp_path, dest=_canary_dest(tmp_path), source_has_files=False)
    with fake_rclone_lan({"rclone-lan-plan": _clean_spec()}) as fake:
        result = flow.lan_preflight_task.fn(cfg, _artefacts_dir(tmp_path))
    assert result["source_empty"] is True
    assert result["canary_present"] is True
    assert len(fake) == 0


def test_preflight_makes_one_dry_run_and_returns_plan_plus_canary(tmp_path):
    cfg = _config(tmp_path, dest=_canary_dest(tmp_path))
    with fake_rclone_lan({"rclone-lan-plan": _clean_spec()}) as fake:
        result = flow.lan_preflight_task.fn(cfg, _artefacts_dir(tmp_path))
    assert len(fake) == 1
    assert fake[0]["step"] == "rclone-lan-plan"
    assert "--dry-run" in fake[0]["argv"]
    assert result["ok"] is True
    assert result["source_files"] == 12
    assert result["dest_files"] == 0
    assert result["planned_deletes"] == 0
    assert result["moves"] == 0
    assert result["planned_bytes"] == 13062
    assert result["canary_present"] is True


def test_g4_plan_not_ok_fails_lan_01_never_blocks(tmp_path):
    cfg = _config(tmp_path, dest=_canary_dest(tmp_path))
    spec = {"exit_code": 2, "log": "", "stderr": "rclone broke"}
    with fake_rclone_lan({"rclone-lan-plan": spec}):
        with pytest.raises(RuntimeError) as exc:
            flow.lan_preflight_task.fn(cfg, _artefacts_dir(tmp_path))
    _assert_fail(exc, "AAM-LAN-01")


# ── G-3, G-5, G-6, G-2(a), G-8, G-9, G-10: flow.lan_guards_task ──────────

def test_g3_empty_source_is_blocked_by_g1_first(tmp_path):
    cfg = _config(tmp_path, dest=UNC_DEST, source_has_files=False)
    with pytest.raises(g.GuardBlocked) as exc:
        _guards(cfg, {"source_empty": True, "canary_present": True})
    assert _records(exc.value)["G1_SOURCE_HAS_FILES"]["fault_code"] == "AAM-SRC-03"


@pytest.mark.parametrize("dest_files,code", [(0, "AAM-NAS-09"), (5, "AAM-NAS-10")])
def test_g2a_missing_canary_blocks_nas_09_or_10(tmp_path, dest_files, code):
    cfg = _config(tmp_path, dest=UNC_DEST)
    with pytest.raises(g.GuardBlocked) as exc:
        _guards(cfg, _plan(dest_files=dest_files, canary_present=False))
    assert _records(exc.value)["G5_CANARY"]["fault_code"] == code


def test_g5_within_allowance_passes_and_max_delete_is_what_is_left(tmp_path):
    cfg = _config(tmp_path, dest=UNC_DEST)
    rec = _guards(cfg, _plan(dest_files=1000, planned_deletes=40))
    assert _verdict(rec, "G3_MASS_DELETE") == g.PASS
    # 1000 files: 10% = 100, above the floor of 20, below the 500-file cap -> 100 allowed
    assert rec["max_delete"] == 100


def test_g5_over_allowance_blocks_del_01(tmp_path):
    cfg = _config(tmp_path, dest=UNC_DEST)
    with pytest.raises(g.GuardBlocked) as exc:
        _guards(cfg, _plan(dest_files=1000, planned_deletes=101))
    assert _records(exc.value)["G3_MASS_DELETE"]["fault_code"] == "AAM-DEL-01"


def test_g5_allowance_uses_the_min_delete_floor(tmp_path):
    """9 destination files: 10% is 0, so the floor of 20 (guards.min_delete_floor_files) decides."""
    cfg = _config(tmp_path, dest=UNC_DEST)
    assert _verdict(_guards(cfg, _plan(dest_files=9, planned_deletes=20)), "G3_MASS_DELETE") == g.PASS
    with pytest.raises(g.GuardBlocked) as exc:
        _guards(cfg, _plan(dest_files=9, planned_deletes=21))
    assert _records(exc.value)["G3_MASS_DELETE"]["fault_code"] == "AAM-DEL-01"


def test_g6_316_moves_and_zero_deletes_pass(tmp_path):
    cfg = _config(tmp_path, dest=UNC_DEST)
    rec = _guards(cfg, _plan(source_files=316, dest_files=316, moves=316, planned_deletes=0))
    assert _verdict(rec, "G3_MASS_DELETE") == g.PASS


def test_g8_g2_compares_plan_source_files_to_the_baseline(tmp_path):
    cfg = _config(tmp_path, dest=UNC_DEST)
    baseline = {"fy": FY, "source_file_count": 1000, "destination": UNC_DEST}
    with pytest.raises(g.GuardBlocked) as exc:
        _guards(cfg, _plan(source_files=500, dest_files=500), baseline=baseline)
    assert _records(exc.value)["G2_SOURCE_SHRINK"]["fault_code"] == "AAM-SRC-04"


def test_g9_unresolvable_host_with_forbid_local_is_fail_not_block(tmp_path):
    cfg = _config(tmp_path, dest=UNC_DEST)
    with pytest.raises(RuntimeError) as exc:
        _guards(cfg, _plan(), resolve=None)
    _assert_fail(exc, "AAM-NAS-01")


def test_g9_loopback_destination_still_blocks_nas_07(tmp_path):
    cfg = _config(tmp_path, dest="\\\\127.0.0.1\\backup\\FY26-27")
    with pytest.raises(g.GuardBlocked) as exc:
        _guards(cfg, _plan(), resolve={"127.0.0.1"})
    assert _records(exc.value)["G4_DESTINATION_IDENTITY"]["fault_code"] == "AAM-NAS-07"


def test_g10_approved_g3_sets_max_delete_to_planned_deletes(tmp_path):
    cfg = _config(tmp_path, dest=UNC_DEST)
    approved = (frozenset({"G3_MASS_DELETE"}), {"approval_id": "appr-1"})
    with patch("flow._apply_approval", return_value=approved):
        rec = _guards(cfg, _plan(dest_files=1000, planned_deletes=101), approval_id="appr-1")
    assert rec["approved_guards"] == ["G3_MASS_DELETE"]
    assert rec["max_delete"] == 101


# ── G-11: flow.lan_access_task ────────────────────────────────────────────

def test_g11_free_space_check_uses_plan_planned_bytes(tmp_path):
    cfg = _config(tmp_path, dest=str(tmp_path / "dest"))
    with patch("core.permissions.probe_source_read") as read_probe, \
         patch("core.permissions.probe_destination_write") as write_probe, \
         patch("core.permissions.check_destination_free_space") as free_space:
        flow.lan_access_task.fn(cfg, _plan(planned_bytes=13062), RUN_ID)
    read_probe.assert_called_once()
    write_probe.assert_called_once()
    free_space.assert_called_once()
    assert free_space.call_args.kwargs["bytes_needed"] == 13062


def test_g11_planned_bytes_none_fails_nas_05_naming_the_dry_run(tmp_path):
    cfg = _config(tmp_path, dest=str(tmp_path / "dest"))
    with patch("core.permissions.probe_source_read"), \
         patch("core.permissions.probe_destination_write"):
        with pytest.raises(RuntimeError) as exc:
            flow.lan_access_task.fn(cfg, _plan(planned_bytes=None), RUN_ID)
    _assert_fail(exc, "AAM-NAS-05")
    assert "rclone dry-run byte count" in str(exc.value)


# ── G-7, G-12: the pre phase, in the order _run_lan_pipeline will run it ──

def _recorder(module: str, spies: list):
    """A run_tool stand-in that records the call and fails the test."""
    def _spy(*args, **kwargs):
        argv = args[1] if len(args) > 1 else kwargs.get("argv", [])
        spies.append((module, [str(a) for a in argv]))
        raise AssertionError(f"{module} called run_tool; only the rclone plan may run here")
    return _spy


def test_g7_g12_pre_phase_sequence_walks_nothing_and_runs_no_robocopy(tmp_path):
    cfg = _config(tmp_path, dest=_canary_dest(tmp_path))   # local dest: no SMB probe
    art = _artefacts_dir(tmp_path)
    spies: list = []
    db = MagicMock()
    db.guard_baseline.return_value = None
    with ExitStack() as st:
        for module in ("core.lan_preflight", "core.cloud_sync", "core.permissions", "core.health"):
            st.enter_context(patch(f"{module}.run_tool", side_effect=_recorder(module, spies)))
        walk = st.enter_context(patch("flow.walk_lan_destination"))
        snapshot = st.enter_context(patch("flow.lan_snapshot_before_task"))
        st.enter_context(patch("flow.ManifestDB")).from_config.return_value = db
        st.enter_context(patch("flow.guards.resolve_host", return_value=set(NAS_IPS)))
        st.enter_context(patch("flow.guards.local_addresses", return_value=set(LOCAL_IPS)))
        st.enter_context(patch("core.permissions.probe_source_read"))
        st.enter_context(patch("core.permissions.probe_destination_write"))
        st.enter_context(patch("core.permissions.check_destination_free_space"))
        fake = st.enter_context(fake_rclone_lan({"rclone-lan-plan": _clean_spec()}))

        plan = flow.lan_preflight_task.fn(cfg, art)
        flow.lan_guards_task.fn(cfg, FY, plan, approval_id=None, run_id=RUN_ID, already_deleted=0)
        flow.lan_access_task.fn(cfg, plan, RUN_ID)

    assert len(fake) == 1 and fake[0]["step"] == "rclone-lan-plan"
    assert not any("robocopy" in arg.lower() for call in fake for arg in call["argv"])
    assert spies == [], f"run_tool used outside the rclone plan: {spies}"
    assert walk.call_count == 0
    assert snapshot.call_count == 0


def test_g7_lan_pipeline_never_calls_lan_snapshot_before():
    """C8 acceptance: lan_snapshot_before_task is no longer called by the LAN pipeline."""
    source = inspect.getsource(flow._run_lan_pipeline)
    assert "lan_snapshot_before_task(" not in source
