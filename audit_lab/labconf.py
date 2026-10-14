"""Lab configuration + shared helpers for the AAM audit lab.

Hard guarantees enforced in code (see assert_lab_path):
  * every destructive/filesystem operation is confined to LAB_ROOT (= %TEMP%\\opencode\\aam_lab)
  * the real config.yaml is never read or written by lab tests
  * cloudsync/verify/reporter/integrity modules get their rclone calls pointed at
    a LOCAL backend remote (config section named [aam_gcs]) -> zero network, zero cost
  * WoL and shutdown paths are disabled AND monkeypatched to raise if ever called
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The lab prints evidence with Unicode filenames; make the console UTF-8 safe.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Make the repo importable when pytest is started from anywhere.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

LAB_ROOT = Path(os.environ.get("AAM_LAB_ROOT", str(Path(tempfile.gettempdir()) / "opencode" / "aam_lab")))
RCLONE_EXE = str(REPO_ROOT / "deploy" / "bin" / "rclone.exe")
ROBOCOPY_EXE = r"C:\Windows\System32\Robocopy.exe"

FAKE_REMOTE = "aam_gcs"  # rclone config section name the production code hardcodes


# ── safety guard ────────────────────────────────────────────────────────────────
def assert_lab_path(p: str | Path, *, must_exist: bool = False) -> Path:
    """Refuse any path outside LAB_ROOT. Raises AssertionError."""
    path = Path(str(p))
    resolved = path.resolve() if path.exists() else Path(os.path.abspath(str(path)))
    lab_resolved = LAB_ROOT.resolve()
    if lab_resolved not in resolved.parents and resolved != lab_resolved:
        raise AssertionError(f"LAB SAFETY: refusing to touch non-lab path {path!r}")
    if must_exist and not path.exists():
        raise AssertionError(f"LAB SAFETY: expected path to exist: {path}")
    return path


# ── evidence ledger ────────────────────────────────────────────────────────────
_EVIDENCE_PATH = LAB_ROOT.parent / "evidence.jsonl"


def record(exp_id: str, hypothesis: str, evidence: str, verdict: str) -> None:
    """Append one evidence row to audit_lab/raw/evidence.jsonl."""
    import json
    import time

    _EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "id": exp_id,
        "hypothesis": hypothesis,
        "evidence": evidence,
        "verdict": verdict,
    }
    with _EVIDENCE_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    safe = lambda s: s.encode("ascii", "replace").decode("ascii")
    print(f"\n[EVIDENCE] {safe(exp_id)}: {safe(verdict)}\n    {safe(evidence)[:600]}\n")


# ── fixtures ───────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def _lab_root():
    LAB_ROOT.mkdir(parents=True, exist_ok=True)
    return LAB_ROOT


@pytest.fixture()
def sandbox():
    """A pristine per-test directory tree under LAB_ROOT.

    Layout:
      <case>/source/FY26-27/...   robocopy/rclone source
      <case>/gcs/FY26-27/...      fake bucket (local backend target)
      <case>/nas/FY26-27/...      fake LAN destination
      <case>/runtime/             logs/, manifest.db, backup.lock
    """
    name = tempfile.mkdtemp(prefix="case_", dir=str(LAB_ROOT))
    base = Path(name)
    for sub in (
        "source/FY26-27/Clients",
        "gcs/FY26-27",
        "nas/FY26-27",
        "runtime/logs",
    ):
        (base / sub).mkdir(parents=True, exist_ok=True)
    yield base
    # keep failures for forensics; successful cases are removed
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture()
def unc_nas(sandbox):
    """Expose the sandbox NAS dir over the loopback admin share (\\127.0.0.1\\C$\\...).

    Returns (local_path, unc_path). Skips the test if the loopback share is
    unreachable (e.g. restricted environment) so UNC-only tests degrade cleanly.
    """
    import pytest as _pytest

    local = sandbox / "nas" / "FY26-27"
    unc = Path(str(local).replace("C:\\", "\\\\127.0.0.1\\C$\\", 1))
    if not local.exists():
        _pytest.skip(f"unexpected missing sandbox path {local}")
    if local.drive.upper() != "C:":
        _pytest.skip("loopback admin-share mapping only implemented for C:")
    try:
        if not unc.exists():
            _pytest.fail(f"loopback UNC not reachable: {unc}")
    except OSError as exc:  # pragma: no cover
        _pytest.skip(f"loopback UNC unreachable: {exc}")
    return local, unc


# ── config builder ─────────────────────────────────────────────────────────────
def _to_unc(p: str | Path) -> str:
    """Convert C:\\x\\y -> \\\\127.0.0.1\\C$\\x\\y (loopback admin share = our fake NAS)."""
    s = str(p)
    if s.startswith("\\\\"):
        return s
    if len(s) > 1 and s[1] == ":":
        drive, rest = s[0], s[2:].replace("/", "\\")
        return f"\\\\127.0.0.1\\{drive}$\\{rest.lstrip(chr(92))}"
    return s


# For lab tests of MIRROR MECHANICS (convergence, metamorphic mutations) on
# tiny random trees, where one stray file is already > the 10 % delete
# limit. The guards themselves are proven in tests/test_m2_guard_wiring.py
# and the W1A/W1C data-loss regressions, which keep the default limits.
# Production LAN command (/ZB) for the C-1/C-2 experiments.
PRODUCTION_LAN = {"use_backup_mode": True}

MIRROR_MECHANICS_GUARDS = {"max_delete_files": 100_000_000, "max_delete_percent": 100,
                           "max_source_shrink_percent": 100,
                           "forbid_local_destination": False}


def build_config(base: Path, *, source: Path, lan_dest: str, runtime: Path,
                 lan_enabled: bool = True, cloud_enabled: bool = True,
                 bucket: str = "lab-bucket", **overrides) -> "AppConfig":
    """Build a real, fully validated AppConfig pointed at lab paths.

    Never reads the repo config.yaml. WoL/shutdown are force-disabled.
    A non-UNC ``lan_dest`` is mapped onto the loopback admin share so the real
    UNC validators and SMB code paths accept it.
    """
    from models.config import AppConfig

    data = {
        "firm_name": "LAB",
        "paths": {
            "source_drive": str(source),
            "lan_destination": _to_unc(lan_dest),
            "runtime_dir": str(runtime),
            "gcs_key_path": str(base / "lab-key.json"),  # never read (local backend)
        },
        "lan": {
            "enabled": lan_enabled,
            "retry_count": 1,
            "retry_wait_seconds": 1,
            "subprocess_timeout_seconds": 3600,
            "shutdown_after_backup": False,          # hard-off in lab
            "shutdown_on_all_retries_exhausted": False,
            "max_attempts": 1,
            "retry_delay_seconds": 60,
            "mt_threads": 2,
            "dry_run_timeout_seconds": 120,
            # The lab account has no SeBackupPrivilege (experiment C1), so the
            # lab runs the documented no-/ZB mode; experiments that need the
            # production command pass lan=dict(PRODUCTION_LAN).
            "use_backup_mode": False,
        },
        "wol": {"enabled": False, "mac_address": "", "server_ip": "127.0.0.1",
                "broadcast_address": "", "wake_timeout_seconds": 60,
                "ping_interval_seconds": 5, "stability_wait_seconds": 0,
                "wake_retry_count": 1, "wake_retry_interval_seconds": 2},
        "cloud": {
            "enabled": cloud_enabled,
            "bucket": bucket,
            "project_number": "000000000000",
            "location": "us-central1",
            "storage_class": "STANDARD",
            "bandwidth_limit": "1000M",
            "retry_count": 1,
            "subprocess_timeout_seconds": 3600,
            "max_attempts": 1,
            "retry_delay_seconds": 60,
            "verify_timeout_seconds": 120,
            "preflight_timeout_seconds": 60,
            "diff_timeout_seconds": 60,
            "manifest_timeout_seconds": 60,
            "cloud_size_timeout_seconds": 30,
            "transfers": 2,
            "checkers": 2,
            "buffer_size": "16M",
            "max_duration_seconds": 0,  # cap off for lab runs
        },
        "schedule": {
            "cloud_cron": "0 18 * * *", "lan_cron": "0 1 * * *",
            "weekly_cron": "0 8 * * MON", "monthly_cron": "30 8 1 * *",
            "rollover_cron": "0 6 * * *", "audit_cron": "0 3 * * SUN",
            "timezone": "Asia/Kolkata",
        },
        "dashboard": {"auth_enabled": False, "api_key": "", "bind_address": "127.0.0.1", "port": 8080},
        "notifications": {"smtp_host": "", "smtp_port": 587, "smtp_username": "",
                          "smtp_password": "", "sender": "", "recipients": [],
                          "send_on_failure": False, "weekly_enabled": False,
                          "monthly_enabled": False},
        "maintenance": {"db_retention_days": 90, "log_retention_days": 30,
                        "sqlite_busy_timeout_ms": 5000, "sqlite_vacuum_freelist_threshold": 10000,
                        "sqlite_synchronous": "normal", "concurrency_wait_seconds": 30},
        "health": {"max_clock_skew_seconds": 600, "clock_check_timeout_seconds": 5,
                   "min_free_source_gb": 1, "rollover_auth_timeout_seconds": 30,
                   "rollover_archive_timeout_seconds": 60},
        # The lab mirrors to the loopback admin share by design, so G4's
        # "destination is this machine" check is off; G1-G3 stay at defaults.
        "guards": {"forbid_local_destination": False},
    }
    # nested override support: {"lan": {"mt_threads": 4}}
    for key, val in overrides.items():
        if isinstance(val, dict) and isinstance(data.get(key), dict):
            data[key].update(val)
        else:
            data[key] = val

    cfg = AppConfig(**data)
    # belt & braces: nothing in the lab may ever shut a machine down or wake one
    import core.shutdown as _shutdown
    # keep the REAL function reachable for contract tests: W16 calls it back
    # with subprocess.run faked, so no shutdown.exe can execute from the lab.
    # build_config() runs many times per session - only save the original the
    # first time, or the second call would store the guard lambda itself.
    if not hasattr(_shutdown, "_LAB_ORIG_SHUTDOWN"):
        _shutdown._LAB_ORIG_SHUTDOWN = _shutdown.shutdown_server
    _shutdown.shutdown_server = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("LAB: shutdown_server called - refusing")
    )
    return cfg


# ── cloud (fake GCS) wiring ────────────────────────────────────────────────────
def write_local_rclone_config(path: Path) -> str:
    """An rclone config whose [aam_gcs] remote is the LOCAL filesystem."""
    path.write_text(f"[{FAKE_REMOTE}]\ntype = local\n", encoding="utf-8")
    return str(path)


def patch_cloud_modules_to_local(config_path: str):
    """Point every cloud module's rclone invocation at the local backend.

    * replaces temp_rclone_config (which writes a GCS config needing a real key)
      with one that returns our local-backend config
    * keeps production command builders and subprocess calls 100% untouched
    """
    import contextlib

    import core.cloud_preflight as cpre
    import core.cloud_reporter as crep
    import core.cloud_sync as csync
    import core.cloud_verify as cver

    @contextlib.contextmanager
    def _local_cfg(*_a, **_k):
        yield config_path

    saved = {}
    for mod, attr in ((csync, "temp_rclone_config"),
                      (cpre, "temp_rclone_config")):
        saved[(mod, attr)] = getattr(mod, attr)
        setattr(mod, attr, _local_cfg)
    return saved


# ── warehouse helpers ───────────────────────────────────────────────────────────
def write_file(path: Path, content: bytes = b"x", *, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def md5(path: Path) -> str:
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()


def run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def robocopy_local(source: Path, dest: Path, flags: list[str], *, timeout: int = 120):
    """Run real robocopy.exe with local (non-UNC) paths."""
    cmd = [ROBOCOPY_EXE, str(source), str(dest), *flags]
    return run(cmd, timeout=timeout)


# ── helpers used by scenario tests ──────────────────────────────────────────────
def strip_zb(cmd: list[str]) -> list[str]:
    """Drop the /ZB flag (needs SeBackupPrivilege — see experiment C1) so the
    scenario can actually run on an unprivileged account."""
    return [a for a in cmd if a.upper() != "/ZB"]


def make_canary(dest: Path) -> Path:
    """Create the LAN destination + the .AAM_TARGET_MOUNTED canary."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".AAM_TARGET_MOUNTED").touch(exist_ok=True)
    return dest


def lan_cfg(base: Path, source: Path, lan_dest: Path, runtime: Path, **kw):
    """build_config() for a LAN run + monkeypatched /ZB-free robocopy command."""
    cfg = build_config(base, source=source, lan_dest=str(lan_dest), runtime=str(runtime), **kw)
    return cfg
