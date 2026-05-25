"""Test script: full LAN backup pipeline on Windows Server 2016."""

from models.config import load_config
from core.lan_preflight import run_lan_dry_run
from core.lan_sync import run_lan_sync
from core.lan_manifest import walk_lan_destination, snapshot_to_dict, diff_snapshots
from core.manifest import ManifestDB
from core.logging import configure as configure_logging
from core.wol import ensure_server_online
from core.shutdown import shutdown_server

config = load_config("config.yaml")
configure_logging(config.paths.log_directory)

# Step 1: WoL
print("\n[1] Wake-on-LAN...")
try:
    ensure_server_online(config)
    print("    Server online")
except Exception as e:
    print(f"    FAILED: {e}")
    exit(1)

# Step 2: Dry-run preflight
print("\n[2] LAN preflight...")
dry = run_lan_dry_run(
    config.paths.source_drive,
    config.paths.lan_destination,
)
print(f"    ok={dry['ok']}, exit={dry['exit_code']}")
if not dry["ok"]:
    print(f"PREFLIGHT FAILED: {dry['error']}")
    exit(1)

# Step 3: Before snapshot
print("\n[3] Before snapshot...")
before_files = walk_lan_destination(config.paths.lan_destination)
before = snapshot_to_dict(before_files)
print(f"    {len(before_files)} files on LAN destination")

# Step 4: Sync
print("\n[4] LAN sync...")
sync = run_lan_sync(
    source=config.paths.source_drive,
    dest=config.paths.lan_destination,
    lan_config=config.lan,
)
print(f"    status={sync['status']}, exit={sync['exit_code']}")
if sync["status"] == "LAN_FAILED":
    print(f"SYNC FAILED: {sync.get('error')}")
    exit(1)

# Step 5: After snapshot + diff
print("\n[5] LAN manifest + diff...")
after_files = walk_lan_destination(config.paths.lan_destination)
after = snapshot_to_dict(after_files)
diff = diff_snapshots(before, after)
print(f"    +{len(diff['added'])} -{len(diff['removed'])} *{len(diff['modified'])} ={len(diff['unchanged'])}")

# Step 6: Update DB
print("\n[6] Update ManifestDB...")
db = ManifestDB(config.paths.database_path)
for f in after_files:
    db.upsert_file_entry(
        relative_path=f["path"],
        file_size=f["size"],
        mtime=f["mtime"],
        lan_status="synced",
    )
db.mark_lan_synced([f["path"] for f in after_files])
if diff["removed"]:
    db.delete_entries(diff["removed"])

count = db.file_count("lan_status")
print(f"    ManifestDB: {count} files tracked")

# Run history
db.insert_run({
    "run_id": "test-lan-manual",
    "mode": "lan",
    "started_at": "2026-05-26T00:00:00",
    "status": sync["status"],
    "exit_code": sync["exit_code"],
})
db.wal_checkpoint()
db.close()

# Step 7: Shutdown
if config.lan.shutdown_after_backup:
    print("\n[7] Server shutdown...")
    result = shutdown_server(config.wol.server_ip)
    print(f"    initiated={result['shutdown_initiated']}")

print("\n=== LAN PIPELINE COMPLETE ===")
