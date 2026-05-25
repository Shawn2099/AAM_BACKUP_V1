"""Quick robocopy debug."""
from models.config import load_config
from core.lan_sync import build_robocopy_command
import subprocess

c = load_config("config.yaml")
cmd = build_robocopy_command(c.paths.source_drive, c.paths.lan_destination, c.lan)
print("CMD:", " ".join(cmd[:6]), "...")

r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
print(f"EXIT: {r.returncode}")
print(f"STDOUT (last 500): {r.stdout[-500:]}")
print(f"STDERR: {r.stderr[:200]}")
