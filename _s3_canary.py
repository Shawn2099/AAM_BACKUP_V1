"""Canary trigger: run a production deployment through the live Prefect service.

Usage: python _s3_canary.py <deployment-id>
Blocks until the flow run reaches a terminal state; prints final state + run id.
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(r"C:\AAMBackup")
os.environ["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"

dep_id = sys.argv[1]
t0 = time.time()
print(f"triggering deployment {dep_id} ...", flush=True)

from prefect.deployments import run_deployment

run = run_deployment(dep_id, timeout=3600)
dt = time.time() - t0

print(f"run id: {run.id}")
print(f"flow:   {run.flow_id}")
print(f"state:  {run.state.type.value} ({run.state.name})" if run.state and run.state.type else "state: <none>")
if run.state and run.state.message:
    print(f"message: {run.state.message}")
print(f"wall time: {dt:.1f} s")
sys.exit(0 if run.state and run.state.type.value == "COMPLETED" else 1)
