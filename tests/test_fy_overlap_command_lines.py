"""B3 review F6 (docs/PLAN_BASELINE_OVERLAP_2026-11-23.md, sections 2.3 and 9): the REAL tool command lines of a
next-FY pass.

The pipelines run for real around a faked `run_tool` (`test_support.fake_tools.fake_run_tool`); only the checks and
bookkeeping tasks around the sync are mocks (as in tests/test_fy_overlap_pipelines.py). The owner's layout is mapped
onto temp folders (`E:\\FY26-27` -> `<tmp>\\E\\FY26-27`, `\\\\nas\\backup` -> `<tmp>\\nas\\backup`). The next
year's pass must read the next year's source folder and write the next year's destinations; the configured year's
folders and prefix must never appear as a destination in it.
"""

import contextlib
import time
from pathlib import Path
from unittest.mock import patch

import pendulum

import flow
from core import fy_overlap
from models.config import CONFIG_PATH, load_config
from test_fy_overlap_pipelines import CLOUD_BASE, LAN_BASE, STARTED, cloud_env, lan_env
from core.run_artefacts import RunArtefacts
from test_support.fake_tools import fake_rclone_lan, fake_run_tool

# captured before any test patches the module attributes: the real task bodies
REAL_CLOUD_SYNC = flow.cloud_sync_task.fn
REAL_LAN_SYNC = flow.lan_sync_task.fn


def next_year_pass(tmp_path):
    """The next-FY pass built by the real rule (`active_passes` -> `config_for_fy`) from a real AppConfig."""
    cur_source = tmp_path / "E" / "FY26-27"
    next_source = tmp_path / "E" / "FY27-28"
    nas_root = tmp_path / "nas" / "backup"
    for folder in (cur_source, next_source, nas_root):
        folder.mkdir(parents=True)
    (next_source / "a.txt").write_text("next year", encoding="utf-8")
    (cur_source / "b.txt").write_text("this year", encoding="utf-8")
    base = load_config(CONFIG_PATH)
    paths = base.paths.model_copy(update={"source_drive": str(cur_source),
                                          "lan_destination": str(nas_root / "FY26-27")})
    rollover = base.rollover.model_copy(update={"overlap_enabled": True})
    cfg = base.model_copy(update={"paths": paths, "rollover": rollover})
    record = {"activated_at": "2026-12-31T06:00:00+05:30", "from_fy": "FY26-27",
              "source_folder": str(next_source), "clock": {}}
    passes = fy_overlap.active_passes(cfg, lambda fy: record if fy == "FY27-28" else None)
    assert [p.fy for p in passes] == ["FY26-27", "FY27-28"]
    return passes[1], cur_source, next_source, nas_root


@contextlib.contextmanager
def real_rclone_config():
    yield "rclone.conf"


def test_cloud_next_pass_syncs_the_next_folder_to_the_next_prefix(tmp_path):
    nxt, cur_source, next_source, _ = next_year_pass(tmp_path)
    with cloud_env() as env, \
            patch("core.cloud_sync.temp_rclone_config", side_effect=lambda *a, **k: real_rclone_config()), \
            fake_run_tool("core.cloud_sync", exit_code=0, log="") as calls:
        env.sync.with_options.return_value.side_effect = REAL_CLOUD_SYNC
        with contextlib.suppress(Exception):   # the empty fake log is not a verdict; the command line is the point
            flow._run_cloud_pipeline(nxt.config, CLOUD_BASE, STARTED, time.monotonic(),
                                     budget_start=time.monotonic(), fy_pass=nxt)
    [sync] = [c for c in calls if c["name"] == "rclone-sync"]
    argv = sync["argv"]
    bucket = nxt.config.cloud.bucket
    assert argv[argv.index("sync") + 1] == str(next_source)
    assert argv[argv.index("sync") + 2] == f"aam_gcs:{bucket}/FY27-28"
    joined = " ".join(argv)
    assert "FY26-27" not in joined and str(cur_source) not in joined


def test_lan_next_pass_mirrors_the_next_folder_to_the_next_nas_folder(tmp_path):
    nxt, cur_source, next_source, nas_root = next_year_pass(tmp_path)
    clean = (Path(__file__).resolve().parents[1] / "test_support" / "rclone_logs"
             / "lan_clean.jsonl").read_text(encoding="utf-8")
    artefacts = RunArtefacts(tmp_path / "runlogs", LAN_BASE, started=pendulum.datetime(2027, 1, 10, 2, 0))
    with lan_env() as env,             fake_rclone_lan({"rclone-lan-sync": {"exit_code": 0, "log": clean}}) as fake:
        env.sync.with_options.return_value.side_effect = (
            lambda config, artefacts_dir, **kw: REAL_LAN_SYNC(config, str(artefacts.dir), **kw))
        with contextlib.suppress(Exception):
            flow._run_lan_pipeline(nxt.config, LAN_BASE, STARTED, time.monotonic(),
                                   budget_start=time.monotonic(), allow_shutdown=False, fy_pass=nxt)
    [mirror] = [c for c in fake.calls if c["step"] == "rclone-lan-sync"]
    argv = mirror["argv"]
    assert argv[argv.index("sync") + 1] == str(next_source)
    assert argv[argv.index("sync") + 2] == str(nas_root / "FY27-28")
    assert (nas_root / "FY27-28").is_dir()   # made by the pass itself (plan 2.3)
    joined = " ".join(argv)
    assert "FY26-27" not in joined and str(cur_source) not in joined
