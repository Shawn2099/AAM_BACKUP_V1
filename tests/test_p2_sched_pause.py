"""P2-SCHED - RED tests: disabled legs must not be scheduled.

Evidence: last night backup-lan fired 21:00 and COMPLETED doing nothing
(lan.enabled=false) - zero DB trace. Fix per plan v1.2/R5:
  1. serve._deployments() only creates deployments for ENABLED legs.
  2. launch._reconcile_disabled_legs() PAUSES any still-registered stale
     deployment server-side (pause beats delete - reversible), and resumes
     when the leg is re-enabled.
"""
import os
from pathlib import Path

import pytest

from models.config import load_config

os.environ.setdefault("PREFECT_API_URL", "http://127.0.0.1:4200/api")

_PROJECT = str(Path(__file__).resolve().parent.parent)


def test_desired_deployments_exclude_disabled_legs(monkeypatch, tmp_path):
    """With lan.disabled, _deployments() must NOT contain backup-lan."""
    import yaml

    base = load_config().model_dump()
    base["lan"]["enabled"] = False
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(base), encoding="utf-8")

    import serve

    monkeypatch.setattr(serve, "CONFIG_PATH", str(cfg_path))
    deps = serve.deployments()

    names = [d.name for d in deps]
    assert "backup-lan" not in names, f"disabled leg must not register: {names}"
    assert "backup-cloud" in names
    assert "rollover-check" in names


def test_desired_deployments_exclude_disabled_cloud(monkeypatch, tmp_path):
    import yaml

    base = load_config().model_dump()
    base["cloud"]["enabled"] = False
    base["lan"]["enabled"] = True  # domain rule: at least one leg must stay on
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(base), encoding="utf-8")

    import serve

    monkeypatch.setattr(serve, "CONFIG_PATH", str(cfg_path))
    names = [d.name for d in serve.deployments()]
    assert "backup-cloud" not in names
    assert "backup-lan" in names


_LIVE_PRELUDE = f'''
import os, sys, json
os.environ["PREFECT_TEST_MODE"] = "0"
os.environ["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"
sys.path.insert(0, {repr(_PROJECT)})
'''


def _live(code: str) -> dict:
    import json as _json
    import subprocess as _sp
    import sys as _sys
    import tempfile as _tf

    script = Path(_tf.gettempdir()) / ("p2sched_%d.py" % (abs(hash(code)) % 10 ** 8))
    script.write_text(_LIVE_PRELUDE + code, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PREFECT_")}
    env["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"
    env["PYTHONPATH"] = _PROJECT
    proc = _sp.run([_sys.executable, str(script)],
                   capture_output=True, text=True, timeout=300, env=env)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    result = _json.loads(lines[-1]) if lines else {}
    result["_rc"] = proc.returncode
    if proc.returncode != 0:
        result["_stderr_tail"] = (proc.stderr or "")[-500:]
    script.unlink(missing_ok=True)
    return result


_ENSURE_AND_PAUSE = f'''
import asyncio, json
from prefect.client.orchestration import get_client
from flow import backup
from models.config import load_config

DEP = "aam-backup/backup-lan-p2scen"

async def main():
    out = {{}}
    async with get_client() as client:
        dep = await backup.ato_deployment(
            name="backup-lan-p2scen",
            parameters={{"config_path": {repr(str(Path(_PROJECT) / "config.yaml"))},
                        "mode": "lan"}},
        )
        await dep.aapply()
        d = await client.read_deployment_by_name(DEP)
        out["created_paused_state"] = bool(d.paused)

        cfg = load_config({repr(str(Path(_PROJECT) / "config.yaml"))})
        from launch import _reconcile_disabled_legs
        out["recon"] = _reconcile_disabled_legs(
            cfg, legs={{"backup-lan-p2scen": False}})

        d = await client.read_deployment_by_name(DEP)
        out["paused_after_disable"] = bool(d.paused)

        out["recon_resume"] = _reconcile_disabled_legs(
            cfg, legs={{"backup-lan-p2scen": True}})
        d = await client.read_deployment_by_name(DEP)
        out["paused_after_enable"] = bool(d.paused)

        await client.delete_deployment(d.id)
        out["cleaned"] = True
    print(json.dumps(out))

asyncio.run(main())
'''


def _is_prefect_live() -> bool:
    try:
        import httpx
        return httpx.get("http://127.0.0.1:4200/api/health", timeout=1.0).status_code == 200
    except Exception:
        return False


@pytest.mark.skipif(not _is_prefect_live(), reason="Live Prefect server (http://127.0.0.1:4200) not running")
def test_reconcile_pauses_stale_and_resumes_enabled_live():
    """Live server: stale deployment must end PAUSED when leg disabled,
    RESUMED when re-enabled, then cleaned up."""
    result = _live(_ENSURE_AND_PAUSE)
    assert result.get("_rc") == 0, f"probe failed: {result}"
    assert result["paused_after_disable"] is True, f"op={result}"
    assert result["paused_after_enable"] is False, f"op={result}"
    assert result["cleaned"] is True, f"op={result}"
