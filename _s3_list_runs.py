"""Inspect all flow runs on the prod Prefect server: state, times, deployment."""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(r"C:\AAMBackup")
os.environ.setdefault("PREFECT_API_URL", "http://127.0.0.1:4200/api")


async def main():
    from prefect.client.orchestration import get_client

    async with get_client() as client:
        deps = await client.read_deployments()
        dep_names = {str(d.id): d.name for d in deps}
        flows = await client.read_flows()
        flow_names = {str(f.id): f.name for f in flows}
        runs = await client.read_flow_runs(limit=100)
        print(f"total runs returned: {len(runs)}")
        from collections import Counter
        c = Counter()
        for r in runs:
            st = r.state_type.value if r.state_type else "?"
            c[st] += 1
            dep = dep_names.get(str(r.deployment_id), "?") if r.deployment_id else "-"
            fn = flow_names.get(str(r.flow_id), "?")
            st = r.state
            stype = st.type.value if st and st.type else "?"
            stname = st.name if st else "?"
            stts = st.timestamp if st else None
            print(
                f"{str(r.id)[:8]} {stype:10s} ({stname:22s}) {fn:16s} dep={dep:15s} "
                f"start={r.start_time} end={r.end_time} state_ts={stts}"
            )
        print("state counts:", dict(c))


asyncio.run(main())
