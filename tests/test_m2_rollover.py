"""M2 2.6 — FY rollover: forward-only, through the normal pipelines (plan WP11, DEC-4).

Findings: rollover ran outside the backup slot (concurrent mirrors), rolled
BACKWARDS when the clock was behind (probe P4), left the new FY source
folder empty (every backup failed from April 1), ran at agent boot.
"""

import inspect
import os
import shutil
import tempfile
from unittest.mock import MagicMock, call, patch

import pytest
from pydantic import ValidationError

import flow
from core import approvals as ap
from core import fy_rollover as fr
from core import guards as g
from core import statuses
from core.manifest import ManifestDB
from core.health import CLOCK_OK, CLOCK_SKEWED, CLOCK_UNVERIFIED, ClockResult
from models.config import AppConfig, HealthConfig, RolloverConfig


# ── Pure decisions ────────────────────────────────────────────────────────

@pytest.mark.parametrize("configured,computed,kind", [
    ("FY26-27", "FY26-27", fr.NONE),
    ("FY26-27", "FY27-28", fr.FORWARD),
    ("fy26-27", "FY27-28", fr.FORWARD),
    ("FY99-00", "FY00-01", fr.FORWARD),          # century wrap
    ("FY26-27", "FY25-26", fr.CLOCK_BEHIND),      # probe P4: never roll backwards
    ("FY26-27", "FY20-21", fr.CLOCK_BEHIND),
    ("FY24-25", "FY26-27", fr.JUMP),              # server off for a year
])
def test_classify_rollover(configured, computed, kind):
    assert fr.classify_rollover(configured, computed) == kind


def test_g6_passes_only_forward_with_every_final_backup_complete():
    ok = g.g6_fy_rollover("FY26-27", "FY27-28", fr.FORWARD,
                          {"cloud": "CLOUD_NO_CHANGES_COMPLETE", "lan": "LAN_COMPLETE"})
    assert ok.passed and ok.metrics["from_fy"] == "FY26-27" and ok.metrics["to_fy"] == "FY27-28"
    failed = g.g6_fy_rollover("FY26-27", "FY27-28", fr.FORWARD,
                              {"cloud": "CLOUD_COMPLETE", "lan": "LAN_FAILED"})
    assert not failed.passed and "lan" in failed.reason and "FY26-27" in failed.reason
    jump = g.g6_fy_rollover("FY24-25", "FY26-27", fr.JUMP, {"lan": "LAN_COMPLETE"})
    assert not jump.passed and "more than one" in jump.reason


def test_g6_approval_phrase_and_identity():
    m = {"from_fy": "FY26-27", "to_fy": "FY27-28"}
    assert ap.confirmation_phrase("G6_FY_ROLLOVER", m) == "APPROVE"
    req = {"leg": "rollover", "guard": "G6_FY_ROLLOVER", "approved_metrics": m,
           "approved_at": "2026-04-01T09:00:00+05:30",
           "expires_at": "2026-04-01T21:00:00+05:30"}
    now = "2026-04-01T10:00:00+05:30"
    assert ap.evaluate_approval(req, leg="rollover", guard="G6_FY_ROLLOVER", fresh_metrics=m,
                                now_iso=now, tolerance_percent=0).allowed
    other = {**m, "to_fy": "FY28-29"}
    assert not ap.evaluate_approval(req, leg="rollover", guard="G6_FY_ROLLOVER",
                                    fresh_metrics=other, now_iso=now,
                                    tolerance_percent=0).allowed


def test_rollover_statuses():
    assert statuses.category("ROLLOVER_COMPLETE") == statuses.CATEGORY_SUCCESS
    assert statuses.category("ROLLOVER_BLOCKED") == statuses.CATEGORY_BLOCKED


# ── Config + helpers ──────────────────────────────────────────────────────

def test_rollover_config():
    assert RolloverConfig().source_placeholder_name == "README_FY.txt"
    for bad in ("a/b.txt", "a\\b.txt", "..", "", "x" * 300):
        with pytest.raises(ValidationError):
            RolloverConfig(source_placeholder_name=bad)
    assert "rollover" in AppConfig.model_fields


def test_new_source_folder_gets_placeholder_and_lan_folder_the_marker(tmp_path):
    src_root, lan_root = tmp_path / "src", tmp_path / "nas"
    src_root.mkdir()
    lan_root.mkdir()
    created = fr.create_new_fy_folders(str(src_root), str(lan_root), "FY27-28",
                                       placeholder_name="README_FY.txt")
    placeholder = src_root / "FY27-28" / "README_FY.txt"
    assert placeholder.is_file() and "FY27-28" in placeholder.read_text(encoding="utf-8")
    assert (lan_root / "FY27-28" / ".AAM_TARGET_MOUNTED").is_file()
    assert created["placeholder"] == placeholder
    # G1 passes on the new, otherwise empty FY folder
    assert g.first_regular_file(str(src_root / "FY27-28"), g.SOURCE_IGNORE_NAMES)


def test_placeholder_never_overwrites(tmp_path):
    new = tmp_path / "src" / "FY27-28"
    new.mkdir(parents=True)
    (new / "README_FY.txt").write_text("client notes", encoding="utf-8")
    fr.create_new_fy_folders(str(tmp_path / "src"), str(tmp_path / "nas"), "FY27-28",
                             placeholder_name="README_FY.txt")
    assert (new / "README_FY.txt").read_text(encoding="utf-8") == "client notes"


def test_config_backup_is_an_exact_timestamped_copy(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("paths:\n  source_drive: E:\\\\DATA\\\\FY26-27\n", encoding="utf-8")
    backup = fr.backup_config_file(str(cfg))
    assert backup.parent == tmp_path and backup.name.startswith("config.yaml.bak-")
    assert backup.read_bytes() == cfg.read_bytes()


def test_bypassing_final_backup_and_boot_rollover_are_gone():
    import launch
    assert not hasattr(fr, "run_final_backup")
    assert "fy_rollover" not in inspect.getsource(launch)


# ── The flow ──────────────────────────────────────────────────────────────

def _cfg():
    cfg = MagicMock()
    cfg.paths.source_drive = "E:\\DATA\\FY26-27"
    cfg.paths.lan_destination = "\\\\nas\\backup\\FY26-27"
    cfg.paths.database_path = ":memory:"
    cfg.cloud.enabled = True
    cfg.lan.enabled = True
    cfg.rollover = RolloverConfig()
    cfg.health = HealthConfig()
    return cfg


class _Rig:
    def __init__(self, computed="FY27-28", cloud="CLOUD_COMPLETE", lan="LAN_COMPLETE",
                 approved=False, clock=None):
        self.cfg = _cfg()
        self.order = MagicMock()
        self._tmp = tempfile.mkdtemp()
        self.db = ManifestDB(os.path.join(self._tmp, "m.db"))
        self.db.close = lambda: None            # the flow closes it; the test reads it afterwards
        self.patches = [
            patch("flow.check_clock", return_value=clock or ClockResult(CLOCK_OK, 1.0, "google", "")),
            patch("flow.load_config", return_value=self.cfg),
            patch("flow.log_retention_task"),
            patch("flow.get_fy_prefix", return_value=computed),
            patch("flow._run_cloud_pipeline", side_effect=self._leg("cloud", cloud)),
            patch("flow._run_lan_pipeline", side_effect=self._leg("lan", lan)),
            patch("flow._record_run"),
            patch("flow._open_block_request"),
            patch("flow.notify"),
            patch("flow._apply_approval", return_value=(
                frozenset({"G6_FY_ROLLOVER"}) if approved else frozenset(),
                {"approval_id": "a1"} if approved else {})),
            patch("flow._backup_slot"),
            patch("flow.fy_rollover.create_source_folder"),
            patch("flow.fy_rollover.create_nas_folder"),
            patch("flow.fy_rollover.update_config_yaml", return_value="cfg.bak"),
            patch("flow.fy_rollover.run_archive_transition", return_value=True),
            # plan A: the journal lives in the manifest DB (one real DB per rig)
            patch("flow.ManifestDB.from_config", side_effect=lambda cfg: self.db),
            patch("flow._record_running"),
            patch("flow.ensure_server_online"),
            patch("flow.lan_shutdown_task"),
        ]

    def _leg(self, leg, status):
        def run(config, run_id, started_at, monotonic_start=None, approval_id=None, **kwargs):
            if status.endswith("COMPLETE"):
                return {"status": status}
            raise RuntimeError(f"{leg} final backup ended {status}")
        return run

    def __enter__(self):
        self.m = {p.attribute: p.start() for p in self.patches}
        for name in ("create_source_folder", "create_nas_folder", "update_config_yaml",
                     "run_archive_transition", "_backup_slot"):
            self.order.attach_mock(self.m[name], name)
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def run(self, **kw):
        return flow.rollover_check_flow.fn(config_path="cfg.yaml", **kw)

    def statuses(self):
        return [c.args[4] for c in self.m["_record_run"].call_args_list]


def test_same_fy_is_a_no_op():
    with _Rig(computed="FY26-27") as r:
        assert r.run() == "NO_ROLLOVER_NEEDED"
        r.m["_run_cloud_pipeline"].assert_not_called()
        r.m["update_config_yaml"].assert_not_called()


def test_forward_rollover_runs_pipelines_then_switches_inside_the_slot():
    with _Rig() as r:
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["_run_cloud_pipeline"].assert_called_once()
        r.m["_run_lan_pipeline"].assert_called_once()
        names = [c[0] for c in r.order.mock_calls if not c[0].startswith("_backup_slot().")]
        # FY-7: the new source folder is created first (before the final backups, no slot needed)
        assert names[:5] == ["create_source_folder", "_backup_slot", "create_nas_folder",
                             "update_config_yaml", "run_archive_transition"]
        r.m["create_source_folder"].assert_called_once_with(
            "E:\\DATA", "FY27-28", placeholder_name="README_FY.txt")
        r.m["create_nas_folder"].assert_called_once_with("\\\\nas\\backup", "FY27-28")
        r.m["update_config_yaml"].assert_called_once_with(
            "cfg.yaml", "E:\\DATA", "\\\\nas\\backup", "FY27-28", expect_fy="FY26-27")
        assert r.statuses() == ["ROLLOVER_COMPLETE"]
        r.m["_open_block_request"].assert_not_called()


def test_failed_final_backup_blocks_and_asks_a_human():
    with _Rig(lan="LAN_FAILED") as r:
        with pytest.raises(fr.RolloverError, match="lan"):
            r.run()
        r.m["update_config_yaml"].assert_not_called()
        r.m["create_source_folder"].assert_called_once()     # FY-7: created early, so the office can save
        assert r.statuses() == ["ROLLOVER_BLOCKED"]
        leg, record = r.m["_open_block_request"].call_args.args[1], \
            r.m["_open_block_request"].call_args.args[3]
        assert leg == "rollover"
        assert record["results"][0]["guard"] == "G6_FY_ROLLOVER"


def test_approved_rerun_rolls_over_despite_a_failed_final_backup():
    with _Rig(lan="LAN_FAILED", approved=True) as r:
        assert r.run(approval_id="a1") == "ROLLOVER_COMPLETED"
        assert r.m["_apply_approval"].call_args.args[1:3] == ("rollover", "a1")
        r.m["update_config_yaml"].assert_called_once()


def test_jump_of_more_than_one_fy_blocks():
    with _Rig(computed="FY28-29") as r:
        with pytest.raises(fr.RolloverError, match="more than one"):
            r.run()
        r.m["update_config_yaml"].assert_not_called()


def test_clock_behind_never_rolls_back_and_is_not_approvable():
    with _Rig(computed="FY25-26") as r:
        with pytest.raises(fr.RolloverError, match="clock"):
            r.run(approval_id="a1")
        r.m["_run_cloud_pipeline"].assert_not_called()
        r.m["_run_lan_pipeline"].assert_not_called()
        r.m["update_config_yaml"].assert_not_called()
        r.m["_open_block_request"].assert_not_called()
        r.m["_apply_approval"].assert_not_called()
        event = r.m["notify"].call_args.args[1]
        assert event.kind == "ACTION_REQUIRED" and "clock" in event.message.lower()
        assert r.statuses() == ["ROLLOVER_BLOCKED"]


@pytest.mark.parametrize("clock", [
    ClockResult(CLOCK_SKEWED, 15_552_000.0, "google",
                "System clock skew detected: 15552000s difference from google"),
    ClockResult(CLOCK_UNVERIFIED, None, None, "Google unreachable; w32tm: no sample"),
], ids=["skewed", "unverified"])
@pytest.mark.parametrize("computed", ["FY27-28", "FY28-29"], ids=["forward", "jump"])
def test_rollover_never_acts_on_a_clock_that_is_not_proven(clock, computed):
    # Code review 2026-10-16: the rollover trusted the local clock. A clock
    # exactly one FY ahead (e.g. 2 Apr 2027 in Oct 2026) rolled over on its
    # own; the nightly backups then covered only the new, empty FY folder while
    # staff kept working in FY26-27 - unprotected, and nothing alerted.
    with _Rig(computed=computed, clock=clock) as r:
        with pytest.raises(fr.RolloverError, match="clock"):
            r.run(approval_id="a1")
        r.m["_run_cloud_pipeline"].assert_not_called()
        r.m["_run_lan_pipeline"].assert_not_called()
        r.m["create_source_folder"].assert_not_called()
        r.m["update_config_yaml"].assert_not_called()
        r.m["run_archive_transition"].assert_not_called()
        r.m["_apply_approval"].assert_not_called()       # an unproven date is not approvable
        r.m["_open_block_request"].assert_not_called()
        assert r.statuses() == ["ROLLOVER_BLOCKED"]
        [c] = r.m["notify"].call_args_list
        event = c.args[1]
        assert event.kind == "ACTION_REQUIRED" and event.status == "ROLLOVER_BLOCKED"
        assert clock.reason in event.message and "FY26-27" in event.message


def test_rollover_checks_the_clock_only_when_a_rollover_is_due():
    with _Rig(computed="FY26-27") as r:
        assert r.run() == "NO_ROLLOVER_NEEDED"
        r.m["check_clock"].assert_not_called()       # the 364 ordinary days: no network call
    with _Rig() as r:
        assert r.run() == "ROLLOVER_COMPLETED"
        kw = r.m["check_clock"].call_args.kwargs
        assert kw["max_skew_seconds"] == HealthConfig().max_clock_skew_seconds
        assert kw["ntp_server"] == HealthConfig().ntp_server


def test_disabled_leg_needs_no_final_backup():
    with _Rig() as r:
        r.cfg.cloud.enabled = False
        assert r.run() == "ROLLOVER_COMPLETED"
        r.m["_run_cloud_pipeline"].assert_not_called()
        r.m["run_archive_transition"].assert_not_called()
