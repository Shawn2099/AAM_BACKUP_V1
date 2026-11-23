"""U17 (agent_runs/TEST_DESIGN_RELEASE.md) - the mirror commands for every active FY folder stay inside it.

During the FY overlap two folders are backed up in the same run: the configured FY and the next one. For
each active folder the test takes the real folder derivation (fy_overlap.active_folders + folder_view), then
builds the real robocopy /MIR command (lan_sync.build_robocopy_command) and the real rclone sync command
(cloud_sync.build_rclone_sync_command), and checks the argument lists.

Oracle (from the card, written here, not computed by the code under test): the robocopy destination is the
FY folder itself (its last element is the FY name), /MIR is present, the mount marker and the probe files
are excluded with /XF, and /MOV, /MOVE and /PURGE never appear. The rclone destination ends with
:<bucket>/<FY> and --max-delete is present.
"""


import pendulum
import pytest

from core import cloud_sync, fy_overlap as ov, fy_rollover, lan_sync
from core.manifest import ManifestDB
from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig
from tests.test_config_cross_field import _quiet_legs

CFG_FY, NEXT = "FY26-27", "FY27-28"
BUCKET = "aam-test-bucket"
MAX_DELETE = 50
ACTIVATED = "2027-01-01T02:01:00+05:30"
RUN_DATE = pendulum.date(2027, 1, 20)          # inside the overlap window for the configured FY
PROBE_GLOB = ".aam_write_probe_*"


@pytest.fixture
def overlap(tmp_path):
    """Config for FY26-27 on a real local tree (the G1 source check needs real files), the next FY
    activated in the manifest DB, so the overlap has two active folders."""
    parent = tmp_path / "data"
    for fy in (CFG_FY, NEXT):
        (parent / fy).mkdir(parents=True)
        (parent / fy / "ledger.xlsx").write_bytes(b"x" * 16)
    key = tmp_path / "key.json"
    key.write_text('{"type": "service_account"}')
    cfg = AppConfig(
        paths=PathsConfig(source_drive=str(parent / CFG_FY), lan_destination=f"\\\\nas\\share\\{CFG_FY}",
                          gcs_key_path=str(key)),
        lan=LanConfig(enabled=True),
        **_quiet_legs(cloud=CloudConfig(enabled=True, bucket=BUCKET, project_number="123")),
    )
    db = ManifestDB(tmp_path / "manifest.db")
    db.record_activation(NEXT, activated_at=ACTIVATED, source_path=str(parent / NEXT), created_by_app=True,
                         clock_state="OK")
    try:
        yield cfg, db
    finally:
        db.close()


def _argv(cfg: AppConfig, fy: str) -> tuple[list[str], list[str]]:
    """The robocopy and rclone argument lists the pipeline would build for one active folder."""
    view = ov.folder_view(cfg, fy)
    robocopy = lan_sync.build_robocopy_command(view.paths.source_drive, view.paths.lan_destination, view.lan)
    rclone = cloud_sync.build_rclone_sync_command(
        view.paths.source_drive, view.cloud.bucket, fy_rollover.effective_fy_prefix(view.paths.source_drive),
        "C:\\aam\\rclone.conf", "STANDARD", max_delete=MAX_DELETE)
    return robocopy, rclone


def test_both_overlap_folders_are_active_on_this_run_date(overlap):
    cfg, db = overlap
    assert sorted(ov.active_folders(cfg, db, RUN_DATE)) == [CFG_FY, NEXT]


@pytest.mark.parametrize("fy", [CFG_FY, NEXT])
def test_robocopy_mirror_targets_only_its_own_fy_folder(overlap, fy):
    cfg, db = overlap
    assert fy in ov.active_folders(cfg, db, RUN_DATE)
    robocopy, _ = _argv(cfg, fy)
    source, dest = robocopy[1], robocopy[2]
    assert source.endswith("\\" + fy) and dest.endswith("\\" + fy)
    assert dest == ov.folder_view(cfg, fy).paths.lan_destination
    assert "/MIR" in robocopy
    xf = robocopy.index("/XF")
    assert robocopy[xf + 1: xf + 3] == [".AAM_TARGET_MOUNTED", PROBE_GLOB]
    for forbidden in ("/MOV", "/MOVE", "/PURGE"):
        assert forbidden not in robocopy


@pytest.mark.parametrize("fy", [CFG_FY, NEXT])
def test_rclone_sync_targets_only_its_own_prefix_and_keeps_the_delete_backstop(overlap, fy):
    cfg, db = overlap
    assert fy in ov.active_folders(cfg, db, RUN_DATE)
    _, rclone = _argv(cfg, fy)
    assert rclone[1] == "sync"
    assert rclone[3] == f"aam_gcs:{BUCKET}/{fy}"
    assert rclone[3].endswith(f":{BUCKET}/{fy}")
    assert "--max-delete" in rclone
    assert rclone[rclone.index("--max-delete") + 1] == str(MAX_DELETE)


def test_the_two_folders_never_share_a_destination(overlap):
    cfg, db = overlap
    dests = []
    for fy in ov.active_folders(cfg, db, RUN_DATE):
        robocopy, rclone = _argv(cfg, fy)
        dests += [robocopy[2], rclone[3]]
    assert len(set(dests)) == len(dests)
