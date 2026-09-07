"""Per-scenario evidence packaging. Collects existing runtime outputs
(app logs, DB exports, tool logs, hashes) into a deterministic
per-run directory. Never fabricates results — only copies/captures
what the real code and tools produced.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

from chaos_harness import safety

EVIDENCE_ROOT = Path(r"C:\ChaosTest\evidence\CHAOS_HARNESS")


class EvidencePack:
    def __init__(self, scenario_id: str, run_name: str | None = None):
        self.scenario_id = scenario_id
        self.stamp = time.strftime("%Y%m%d_%H%M%S")
        self.run_name = run_name or self.stamp
        self.dir = EVIDENCE_ROOT / scenario_id / self.run_name
        self._manifest: list[dict] = []

    def open(self) -> "EvidencePack":
        safety.assert_chaos_path(str(self.dir))
        self.dir.mkdir(parents=True, exist_ok=True)
        return self

    def note(self, name: str, payload: dict) -> Path:
        p = self.dir / f"{name}.json"
        p.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        self._manifest.append({"type": "note", "name": name, "file": p.name})
        return p

    def capture_file(self, src: str, dest_name: str | None = None) -> Path:
        safety.assert_chaos_path(src)
        src_p = Path(src)
        dest = self.dir / (dest_name or src_p.name)
        shutil.copy2(src_p, dest)
        self._manifest.append({"type": "file", "src": str(src_p), "file": dest.name})
        return dest

    def capture_command(self, name: str, cmd: list[str], timeout: int = 120) -> Path:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        p = self.dir / f"{name}.txt"
        p.write_text(f"$ {' '.join(cmd)}\nrc={r.returncode}\n"
                     f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}",
                     encoding="utf-8")
        self._manifest.append({"type": "command", "name": name, "rc": r.returncode,
                               "file": p.name})
        return p

    def close(self, verdict: str) -> Path:
        p = self.dir / "manifest.json"
        p.write_text(json.dumps({
            "scenario": self.scenario_id, "run": self.run_name,
            "stamp": self.stamp, "verdict": verdict,
            "artifacts": self._manifest,
        }, indent=1), encoding="utf-8")
        return p
