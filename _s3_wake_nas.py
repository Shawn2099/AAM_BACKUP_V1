"""Wake the NAS via the app's own WoL mechanism (same as every production run)."""
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(r"C:\AAMBackup")

from models.config import load_config
from core.wol import ensure_server_online

cfg = load_config("config.yaml")
t0 = time.time()
print(f"sending WoL to {cfg.wol.mac_address} ({cfg.wol.server_ip}) ...", flush=True)
ok = ensure_server_online(cfg)
print(f"ensure_server_online -> {ok} in {time.time()-t0:.0f}s")
sys.exit(0 if ok else 1)
