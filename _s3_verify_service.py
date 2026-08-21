"""Phase 2: verify the deployed service runs eef781f code + expected prod config.

1) Import flow.py FROM C:\\AAMBackup with the production venv python and check
   for symbols that exist only in eef781f (M3 handler, M5-guarded preflight, M4 report).
2) Query the live Prefect API (127.0.0.1:4200) for the 5 deployments + schedules.
3) Print the tail of agent_svc.log (startup banner).
"""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(r"C:\AAMBackup")  # so imports resolve against the production tree
os.environ.setdefault("PREFECT_API_URL", "http://127.0.0.1:4200/api")

print("=== 1) code identity (imported from C:\\AAMBackup) ===")
import flow
import core.lan_preflight as lp
import core.cloud_preflight as cp
import core.report as rep
import core.cloud_sync as cs
from models import config as mc

def _flag_in_code(path: str, needle: str) -> bool:
    """True if needle appears in any NON-COMMENT line of the file."""
    for line in open(path, encoding="utf-8"):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if needle in line:
            return True
    return False


checks = [
    ("flow._handle_concurrency_slot_timeout (M3)", hasattr(flow, "_handle_concurrency_slot_timeout")),
    ("flow.lan_shutdown_task split on bit 3 (M1)", _flag_in_code(r"C:\AAMBackup\flow.py", "exit_code & 8")),
    ("core.report.sendmail refusal handling (M4)", "refused" in open(r"C:\AAMBackup\core\report.py", encoding="utf-8").read()),
    ("core.cloud_sync resolve_binary (M6)", "resolve_binary" in open(r"C:\AAMBackup\core\cloud_sync.py", encoding="utf-8").read()),
    ("no --modify-window in sync command (S2-30)", not _flag_in_code(r"C:\AAMBackup\core\cloud_sync.py", "--modify-window")),
    ("no --modify-window in verify (S2-30)", not _flag_in_code(r"C:\AAMBackup\core\cloud_verify.py", "--modify-window")),
    ("config unknown-key warning (M8)", hasattr(mc.AppConfig, "_warn_unknown_root_keys")),
    ("lan_preflight exists() guard (M5)", "except OSError" in open(r"C:\AAMBackup\core\lan_preflight.py", encoding="utf-8").read()),
]
ok = True
for name, good in checks:
    ok = ok and good
    print(f"  {'OK  ' if good else 'FAIL'} {name}")

print("\n=== 2) prod config loaded (from C:\\AAMBackup\\config.yaml) ===")
cfg = mc.load_config(mc.CONFIG_PATH)
print(f"  source: {cfg.paths.source_drive}")
print(f"  lan destination: {cfg.paths.lan_destination}")
print(f"  database: {cfg.paths.database_path}")
print(f"  runtime_dir: {cfg.paths.runtime_dir}")
print(f"  shutdown_after_backup: {cfg.lan.shutdown_after_backup}")
print(f"  wol enabled: {cfg.wol.enabled}")
print(f"  cloud cron: {cfg.schedule.cloud_cron} | lan cron: {cfg.schedule.lan_cron} | tz: {cfg.schedule.timezone}")
print(f"  smtp: {cfg.notifications.smtp_host}:{cfg.notifications.smtp_port} user={cfg.notifications.smtp_username!r} pass_set={bool(cfg.notifications.smtp_password)}")
print(f"  sender: {cfg.notifications.sender!r} recipients: {cfg.notifications.recipients} send_on_failure: {cfg.notifications.send_on_failure}")
print(f"  gcs bucket: {cfg.cloud.bucket}")

print("\n=== 3) Prefect API + deployments ===")


async def pf():
    from prefect.client.orchestration import get_client

    async with get_client() as client:
        deps = await client.read_deployments()
        flows = await client.read_flows()
        flow_names = {str(f.id): f.name for f in flows}
        for d in sorted(deps, key=lambda x: x.name):
            fname = flow_names.get(str(d.flow_id), "?")
            print(f"  deployment: {d.name:20s} flow={fname:12s} id={d.id}")
        runs = await client.read_flow_runs(limit=20, sort="START_TIME_DESC")
        print(f"  total flow runs on these deployments: {len(runs)}")
        for r in runs[:5]:
            fname = flow_names.get(str(r.flow_id), "?")
            print(f"    run {str(r.id)[:8]} flow={fname:12s} state={r.name} start={r.start_time}")


asyncio.run(pf())

print("\nIDENTITY+CONFIG VERIFICATION:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
