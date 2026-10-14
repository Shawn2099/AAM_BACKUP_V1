"""Sandbox configuration for every test session (plan WP16 — test isolation).

Production evidence (2026-08..09 logs): test sessions run on the server
wrote into the production watchdog/backup logs, the real GCS bucket and the
source drive, because code under test resolved the real `config.yaml`.

`install()` must run from a conftest BEFORE any project module is imported:
it writes a valid, harmless config into a fresh temp directory and points
`AAM_CONFIG` at it, so every `CONFIG_PATH` default resolves to the sandbox:

* runtime_dir / logs / database / lock live under the temp directory;
* the bucket name is obviously fake and the key file is a dummy;
* the LAN destination is a non-existent loopback share (any accidental real
  transfer fails safely at preflight);
* WoL, shutdown and email are disabled.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

SANDBOX_BUCKET = "aam-test-sandbox-not-a-real-bucket"
SANDBOX_MARKER = "aam-test-sandbox-"
_LAN_DEST = "\\\\127.0.0.1\\aam-test-sandbox-no-such-share\\FY26-27"


def _config_dict(root: Path) -> dict:
    source = root / "source" / "FY26-27"
    source.mkdir(parents=True, exist_ok=True)
    key = root / "keys" / "sandbox-key.json"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text('{"type": "service_account", "sandbox": true}', encoding="utf-8")
    return {
        "firm_name": "AAM Test Sandbox",
        "paths": {
            "source_drive": str(source),
            "lan_destination": _LAN_DEST,
            "runtime_dir": str(root / "runtime"),
            "gcs_key_path": str(key),
        },
        "lan": {"enabled": True, "shutdown_after_backup": False,
                "shutdown_on_all_retries_exhausted": False},
        "wol": {"enabled": False},
        "cloud": {"enabled": True, "bucket": SANDBOX_BUCKET, "project_number": "0"},
        "notifications": {"send_on_failure": False, "weekly_enabled": False,
                          "monthly_enabled": False},
        "dashboard": {"auth_enabled": False},
    }


def install() -> Path:
    """Create the sandbox config and export AAM_CONFIG. Idempotent per process."""
    existing = os.environ.get("AAM_CONFIG", "")
    if existing and SANDBOX_MARKER in existing and Path(existing).exists():
        return Path(existing)
    root = Path(tempfile.mkdtemp(prefix=SANDBOX_MARKER))
    cfg_path = root / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(_config_dict(root)), encoding="utf-8")
    os.environ["AAM_CONFIG"] = str(cfg_path)
    return cfg_path


def is_sandbox_path(path: str | os.PathLike) -> bool:
    return SANDBOX_MARKER in str(path)
