"""Dump raw JSON for all SCHEDULED flow runs (when will they fire?)."""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(r"C:\AAMBackup")
os.environ.setdefault("PREFECT_API_URL", "http://127.0.0.1:4200/api")


async def main():
    import httpx

    async with httpx.AsyncClient(base_url="http://127.0.0.1:4200/api") as c:
        r = await c.post("/flow_runs/filter", json={"limit": 100, "sort": "START_TIME_DESC"})
        data = r.json()
        for fr in data:
            st = fr.get("state", {}) or {}
            if st.get("type") != "SCHEDULED":
                continue
            dep_id = fr.get("deployment_id")
            print(
                f"id={fr['id'][:8]} dep={dep_id} "
                f"name={fr.get('name')!r} "
                f"scheduled={fr.get('scheduled_time')} "
                f"state_ts={st.get('timestamp')} "
                f"params={fr.get('parameters')}"
            )
        # deployment ids for reference
        deps = (await c.get("/deployments")).json()
        for d in deps:
            print(f"deployment {d['name']:16s} id={d['id']}")


asyncio.run(main())
