from models.config import load_config
from core.lan_sync import build_robocopy_command, classify_exit_code
import subprocess

c = load_config("config.yaml")

# Test 1: our full command
print("=== Test 1: build_robocopy_command ===")
cmd = build_robocopy_command(c.paths.source_drive, c.paths.lan_destination, c.lan)
print("CMD:", " ".join(cmd))

r1 = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
print(f"EXIT: {r1.returncode} -> {classify_exit_code(r1.returncode)}")
if r1.stdout: print("STDOUT:", r1.stdout[:300])
if r1.stderr: print("STDERR:", r1.stderr[:300])

print()
print("=== Test 2: minimal robocopy ===")
cmd2 = ["robocopy", c.paths.source_drive, c.paths.lan_destination,
        "/MIR", "/Z", "/XJ", "/R:3", "/W:10", "/NJH", "/NJS", "/NP"]
print("CMD:", " ".join(cmd2))
r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=60)
print(f"EXIT: {r2.returncode} -> {classify_exit_code(r2.returncode)}")
if r2.stdout: print("STDOUT:", r2.stdout[:300])
if r2.stderr: print("STDERR:", r2.stderr[:300])

print()
print("=== Test 3: our command WITHOUT /LOG ===")
cmd3 = [x for x in cmd if not x.startswith("/LOG")]
print("CMD:", " ".join(cmd3))
r3 = subprocess.run(cmd3, capture_output=True, text=True, timeout=60)
print(f"EXIT: {r3.returncode} -> {classify_exit_code(r3.returncode)}")
if r3.stdout: print("STDOUT:", r3.stdout[:300])
if r3.stderr: print("STDERR:", r3.stderr[:300])
