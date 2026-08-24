"""P2-SCHED live rollout - pause stale disabled-leg deployment on prod server.

Idempotent; safe to re-run. Uses the same code path as agent boot.
Usage: PREFECT_API_URL=http://127.0.0.1:4200/api .venv\\Scripts\\python.exe scripts\\p2_reconcile_live.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launch import _reconcile_disabled_legs  # noqa: E402
from models.config import load_config  # noqa: E402


async def _snapshot() -> dict:
    from prefect.client.orchestration import get_client

    async with get_client() as client:
        out = {}
        for name in ("backup-lan", "backup-cloud"):
            try:
                d = await client.read_deployment_by_name(f"aam-backup/{name}")
                out[name] = "paused" if d.paused else "active"
            except Exception:
                out[name] = "absent"
        return out


def main() -> int:
    cfg = load_config()
    print(f"config lan.enabled={cfg.lan.enabled} cloud.enabled={cfg.cloud.enabled}")
    print("BEFORE:", asyncio.run(_snapshot()))
    results = _reconcile_disabled_legs(cfg)
    print("RECONCILE:", results)
    print("AFTER:", asyncio.run(_snapshot()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
