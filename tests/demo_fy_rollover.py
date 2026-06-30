import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from ruamel.yaml import YAML
import time

# Create a temporary environment
temp_dir = tempfile.mkdtemp(prefix="aam_rollover_demo_")
print(f"--- Setting up demo environment at {temp_dir} ---")

# 1. Load the REAL production config schema and modify paths for testing
real_config_path = "client server data/config.yaml"
config_path = os.path.join(temp_dir, "config.yaml")

yaml = YAML()
with open(real_config_path, "r", encoding="utf-8") as f:
    initial_config = yaml.load(f)

# Modify paths to point to our isolated temp directory for FY25-26
initial_config["paths"]["source_drive"] = os.path.join(temp_dir, "SOURCE", "FY25-26")
initial_config["paths"]["lan_destination"] = "\\\\DUMMY_NAS\\share\\FY25-26"

# Ensure the "old" source directory actually exists
os.makedirs(initial_config["paths"]["source_drive"], exist_ok=True)

with open(config_path, "w", encoding="utf-8") as f:
    yaml.dump(initial_config, f)

print("1. Initial config.yaml created:")
print(f"   Source Drive: {initial_config['paths']['source_drive']}")
print(f"   LAN Dest:     {initial_config['paths']['lan_destination']}")

# 2. Import the rollover logic
import core.fy_rollover as fy_rollover

print("\n--- Running Rollover Simulation ---")
print("Simulating that the calendar date has advanced to April 2026 (FY26-27).")

# We use mocking to bypass actual backups, focusing on the structural logic
with patch("core.fy_rollover.get_fy_prefix", return_value="FY26-27"), \
     patch("core.fy_rollover.run_cloud_sync", return_value={"exit_code": 0}), \
     patch("core.fy_rollover.run_lan_sync", return_value={"exit_code": 0}), \
     patch("core.fy_rollover.run_archive_transition", return_value=True), \
     patch("core.wol.ensure_server_online", return_value=True):
    
    # Run the rollover logic
    fy_rollover.rollover(config_path=config_path)

print("\n--- Verifying Rollover Results ---")

# 3. Verify config.yaml was updated correctly
with open(config_path, "r", encoding="utf-8") as f:
    updated_config = yaml.load(f)
o
print("2. Updated config.yaml reads:")
print(f"   Source Drive: {updated_config['paths']['source_drive']}")
print(f"   LAN Dest:     {updated_config['paths']['lan_destination']}")

if "FY26-27" in updated_config['paths']['source_drive']:
    print("\n✅ SUCCESS: config.yaml automatically updated to the new fiscal year.")
else:
    print("\n❌ FAILED: config.yaml was not updated.")

# 4. Verify new folders were created (only checking source since LAN is a dummy UNC path)
expected_new_source = os.path.join(temp_dir, "SOURCE", "FY26-27")

if os.path.exists(expected_new_source):
    print(f"✅ SUCCESS: New local source folder created at {expected_new_source}")
else:
    print("❌ FAILED: New source folder not created.")

# Cleanup
shutil.rmtree(temp_dir)
print("\n--- Demo Complete ---")
