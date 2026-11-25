"""Tests for WS-F F1 shadow-trial runner."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.lan_sync import build_lan_rclone_command
from models.config import load_config
from tools.shadow.run_step import run_shadow_step


def _write_cfg(tmp_path: Path) -> Path:
    src = tmp_path / "FY26-27"
    src.mkdir(parents=True, exist_ok=True)
    (src / "a.txt").write_text("hello", encoding="utf-8")
    mirror = r"\\nas01\share\FY26-27"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
firm_name: TEST
paths:
  source_drive: "{src.as_posix()}"
  lan_destination: '{mirror}'
  runtime_dir: "{(tmp_path / 'runtime').as_posix()}"
cloud:
  enabled: false
wol:
  enabled: false
""",
        encoding="utf-8",
    )
    return cfg


def test_refuses_non_dry_run_inside_real_mirror(tmp_path: Path):
    cfg_path = _write_cfg(tmp_path)
    cfg = load_config(str(cfg_path))
    with pytest.raises(ValueError, match="Refusing non-dry-run shadow write"):
        run_shadow_step(
            step="bad",
            config_path=str(cfg_path),
            source=cfg.paths.source_drive,
            dest=str(Path(cfg.paths.lan_destination) / "sub"),
            out_dir=str(tmp_path / "out"),
            dry_run=False,
        )


def test_dry_run_uses_exact_product_argv_and_writes_bundle(tmp_path: Path):
    cfg_path = _write_cfg(tmp_path)
    cfg = load_config(str(cfg_path))
    shadow_dest = tmp_path / "AAM_SHADOW_TEST" / "FY26-27"
    shadow_dest.mkdir(parents=True, exist_ok=True)

    class _FakeProc:
        pid = 12345
        returncode = 0

        def poll(self):
            return 0

        def communicate(self):
            return ("", "")

    with patch("tools.shadow.run_step.subprocess.Popen", return_value=_FakeProc()), \
            patch("tools.shadow.run_step.psutil.Process"):
        res = run_shadow_step(
            step="plan_ro",
            config_path=str(cfg_path),
            source=cfg.paths.source_drive,
            dest=cfg.paths.lan_destination,
            out_dir=str(tmp_path / "out"),
            dry_run=True,
        )

    bundle = Path(res["bundle"])
    expected_cmd = build_lan_rclone_command(
        cfg.lan,
        cfg.paths.source_drive,
        cfg.paths.lan_destination,
        str(bundle / "rclone.log"),
        dry_run=True,
        combined_path=str(bundle / "combined.txt"),
    )
    assert res["cmd"] == expected_cmd
    for name in ("argv.json", "memory.csv", "stats.json", "timings.json"):
        assert (bundle / name).is_file(), name
