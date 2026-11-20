r"""OV-11.5: FY overlap rehearsal on the trial box. NOT production code: nothing here is imported by the app.

Runs ONE cloud flow and ONE LAN flow with the run date patched (fy_overlap.run_date, in this process only)
to 15 January of a rehearsal year, so the two-folder overlap happens for real: next year's folder is
activated, both folders are backed up to the cloud and to the NAS, the INFO "now backed up as well" mail is
sent and the dashboard shows two cards. Then every test folder and prefix is removed.

  python tools/fy_overlap_rehearsal.py                 # DRY RUN (default): safety checks + what it WOULD do
  python tools/fy_overlap_rehearsal.py --execute       # really do it
  python tools/fy_overlap_rehearsal.py --execute --keep   # do not clean up afterwards (inspect, then --cleanup)
  python tools/fy_overlap_rehearsal.py --cleanup       # only remove the leftovers of an earlier run

Safe by construction:
* The system clock is never read for a decision it did not make before and never changed; only
  `fy_overlap.run_date` is replaced (module attribute, this process only, restored in `finally`).
* No date switch in config.yaml: the production config is only READ. The flows run on a throwaway sandbox
  config (own runtime dir, own manifest.db, own backup.lock, own logs) under <scratch>.
* Test names only: the rehearsal years are FY90-91 (configured) and FY91-92 (next). They are folders and
  prefixes that no production year can have (the script refuses if production is one of them).
  Cloud: the same bucket, prefixes FY90-91 / FY91-92 only. NAS: <share>\FY90-91 and <share>\FY91-92 only.
  Local source: <scratch>\src\FY90-91 and FY91-92. The production FY folders are never opened for writing.
* Refuses to run when a backup may be running: production backup.lock present, the Prefect slot
  'aam-backup' taken or unreadable, a scheduled backup or audit starting soon, or a test folder / prefix
  already exists (leftover: run --cleanup after looking at it).
* The NAS is never shut down: shutdown is off in the sandbox config and the shutdown task is replaced.
  (The LAN flow wakes the NAS like every night; that is the only thing it does to its power state.)
* Cleanup only removes paths that pass `is_rehearsal_path` / `is_rehearsal_prefix`.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REHEARSAL_FY = "FY90-91"          # the configured year of the rehearsal (leaf of its source folder)
REHEARSAL_NEXT_FY = "FY91-92"     # the next year: the early folder
REHEARSAL_FYS = (REHEARSAL_FY, REHEARSAL_NEXT_FY)
REHEARSAL_DATE = (2091, 1, 15)    # 15 January 2091: inside FY90-91, month >= early_start_month
SCRATCH_NAME = "_overlap_rehearsal"
SLOT_NAME = "aam-backup"
DEFAULT_MIN_GAP_MINUTES = 90      # refuse when a scheduled backup / audit is due within this time
SEED_FILES_PER_FOLDER = 6
CLOUD_REMOTE = "aam_gcs"


class Refusal(Exception):
    """The rehearsal must not run (or clean up) like this."""


@dataclass
class Plan:
    scratch: Path
    source_parent: Path
    sources: dict[str, Path]
    nas_parent: str
    nas_folders: dict[str, str]
    bucket: str
    cloud_prefixes: dict[str, str]
    sandbox_config: Path
    run_date: tuple[int, int, int] = REHEARSAL_DATE

    def lines(self) -> list[str]:
        out = [f"Run date patched to {self.run_date[2]:02d}-{self.run_date[1]:02d}-{self.run_date[0]} "
               f"(this process only; the system clock is not touched)",
               f"Sandbox config: {self.sandbox_config} (own database, lock, logs under {self.scratch})"]
        for fy in REHEARSAL_FYS:
            out.append(f"Seed {SEED_FILES_PER_FOLDER} small files in {self.sources[fy]}")
        out.append("Run ONE cloud flow (mode=cloud) then ONE LAN flow (mode=lan) on the sandbox config")
        for fy in REHEARSAL_FYS:
            out.append(f"Cloud copy: {CLOUD_REMOTE}:{self.bucket}/{self.cloud_prefixes[fy]}")
            out.append(f"NAS copy:   {self.nas_folders[fy]}")
        out += ["Check: next year activated; INFO mail recorded; /status folders[] shows two cards; both "
                "folders hold files in the cloud and on the NAS; run history has 4 rows (2 FYs x 2 legs)",
                "Clean up: delete the two cloud prefixes, the two NAS folders and the scratch folder"]
        return out


# ---- name safety (pure) ------------------------------------------------------------------------

def is_rehearsal_name(name: str) -> bool:
    return name.strip().upper() in REHEARSAL_FYS


def is_rehearsal_path(path: str | os.PathLike, *, parent: str | os.PathLike | None = None) -> bool:
    """A folder the rehearsal may create / delete: its leaf is a rehearsal FY (and, when given, its parent is
    exactly `parent`). Never a share root, never a drive root."""
    p = str(path).replace("/", "\\").rstrip("\\")
    leaf = p.rpartition("\\")[2]
    if not is_rehearsal_name(leaf):
        return False
    if parent is not None:
        want = str(parent).replace("/", "\\").rstrip("\\").lower()
        if p.rpartition("\\")[0].lower() != want:
            return False
    return True


def is_rehearsal_prefix(prefix: str) -> bool:
    """Cloud prefix exactly FY90-91 / FY91-92 (no sub-path, no bucket root, no wildcard)."""
    return prefix.strip("/").upper() in REHEARSAL_FYS and "/" not in prefix.strip("/")


def _fy_leaf(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\").rpartition("\\")[2].upper()


# ---- plan --------------------------------------------------------------------------------------

def build_plan(prod_cfg, scratch: Path | None = None) -> Plan:
    """The paths of this rehearsal. Raises Refusal when production could collide with it."""
    configured = _fy_leaf(prod_cfg.paths.source_drive)
    if configured in REHEARSAL_FYS or _fy_leaf(prod_cfg.paths.lan_destination) in REHEARSAL_FYS:
        raise Refusal(f"Production is configured on {configured}, a rehearsal year: refusing.")
    if not prod_cfg.cloud.enabled or not prod_cfg.lan.enabled:
        raise Refusal("The rehearsal needs both the cloud leg and the LAN leg switched on in the config.")
    if not prod_cfg.rollover.overlap_enabled:
        raise Refusal("rollover.overlap_enabled is off: the overlap cannot be rehearsed.")
    scratch = scratch or Path(prod_cfg.paths.runtime_dir) / SCRATCH_NAME
    nas_parent = prod_cfg.paths.lan_destination.rstrip("\\").rpartition("\\")[0]
    if len([p for p in nas_parent.replace("/", "\\").split("\\") if p]) < 2:
        raise Refusal(f"The NAS share of {prod_cfg.paths.lan_destination} cannot be worked out.")
    src_parent = scratch / "src"
    return Plan(
        scratch=scratch, source_parent=src_parent,
        sources={fy: src_parent / fy for fy in REHEARSAL_FYS},
        nas_parent=nas_parent,
        nas_folders={fy: f"{nas_parent}\\{fy}" for fy in REHEARSAL_FYS},
        bucket=prod_cfg.cloud.bucket,
        cloud_prefixes={fy: fy for fy in REHEARSAL_FYS},
        sandbox_config=scratch / "config.rehearsal.yaml")


def build_sandbox_raw(raw: dict, plan: Plan) -> dict:
    """The production config (raw mapping, read only) as the rehearsal flows see it. Same mail, bucket,
    key and NAS settings; everything that is state (DB, lock, logs, sources) is the scratch one; the
    configured year is the rehearsal year; NAS shutdown is off."""
    cfg = json.loads(json.dumps(raw))                       # deep copy, never mutate the caller's
    paths = cfg.setdefault("paths", {})
    paths["source_drive"] = str(plan.sources[REHEARSAL_FY])
    paths["lan_destination"] = plan.nas_folders[REHEARSAL_FY]
    paths["runtime_dir"] = str(plan.scratch / "runtime")
    paths.pop("database_path", None)
    paths.pop("log_directory", None)
    if isinstance(cfg.get("run_logs"), dict):
        cfg["run_logs"].pop("dir", None)
    lan = cfg.setdefault("lan", {})
    lan["shutdown_after_backup"] = False
    lan["shutdown_on_all_retries_exhausted"] = False
    rollover = cfg.setdefault("rollover", {})
    rollover["overlap_enabled"] = True
    rollover["early_start_month"] = 1
    return cfg


# ---- safety checks (each returns a list of problems; empty = fine) -----------------------------

def check_production_lock(prod_cfg, read_lock=None) -> list[str]:
    """Any production backup.lock refuses: a live holder is a running backup; a stale one is something
    the owner should look at before a rehearsal."""
    lock = Path(prod_cfg.paths.backup_lock_path)
    if not lock.exists():
        return []
    try:
        alive, pid = (read_lock or _read_lock)(lock)
    except Exception as e:  # noqa: BLE001 - unreadable = unsafe
        return [f"{lock} exists and cannot be read ({e})"]
    return [f"{lock} exists ({'a backup is running, pid ' + str(pid) if alive else 'stale lock, pid ' + str(pid)})"]


def _read_lock(lock: Path):
    from core.process import read_lock_alive
    return read_lock_alive(lock)


def check_slot(get_active_slots=None) -> list[str]:
    """The Prefect slot 'aam-backup' must be readable and empty (fail closed on any doubt)."""
    try:
        active = (get_active_slots or _prefect_active_slots)()
    except Exception as e:  # noqa: BLE001
        return [f"the Prefect slot '{SLOT_NAME}' could not be read ({type(e).__name__}: {e}); is PREFECT_API_URL set?"]
    if active is None:
        return [f"the Prefect slot '{SLOT_NAME}' does not exist or could not be read"]
    if active != 0:
        return [f"the Prefect slot '{SLOT_NAME}' is taken ({active} active)"]
    return []


def _prefect_active_slots():
    import asyncio

    async def _read():
        from prefect.client.orchestration import get_client
        async with get_client() as client:
            limit = await client.read_global_concurrency_limit_by_name(SLOT_NAME)
            return getattr(limit, "active_slots", None)
    return asyncio.run(_read())


def check_schedule(prod_cfg, now: datetime, min_gap_minutes: int) -> list[str]:
    """No scheduled backup or audit within `min_gap_minutes` (the rehearsal would hold the slot)."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from cronsim import CronSim

    tz = ZoneInfo(prod_cfg.schedule.timezone)
    local = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    horizon = local + timedelta(minutes=min_gap_minutes)
    problems = []
    for name, cron in (("cloud", prod_cfg.schedule.cloud_cron), ("LAN", prod_cfg.schedule.lan_cron),
                       ("audit", prod_cfg.schedule.audit_cron)):
        try:
            nxt = next(iter(CronSim(cron, local)))
        except Exception as e:  # noqa: BLE001
            problems.append(f"the {name} schedule {cron!r} could not be read ({e})")
            continue
        if nxt <= horizon:
            problems.append(f"the {name} job is scheduled for {nxt:%Y-%m-%d %H:%M}, within {min_gap_minutes} min")
    return problems


def check_targets(plan: Plan, *, path_exists, cloud_prefix_exists, nas_share_reachable) -> list[str]:
    """No test folder / prefix may exist yet (a leftover is never reused or deleted blindly) and the NAS
    share must be reachable."""
    problems = []
    if not nas_share_reachable(plan.nas_parent):
        problems.append(f"the NAS share {plan.nas_parent} cannot be reached (wake the NAS first; this script "
                        "never shuts it down)")
    for fy in REHEARSAL_FYS:
        if path_exists(str(plan.sources[fy])):
            problems.append(f"{plan.sources[fy]} already exists")
        if path_exists(plan.nas_folders[fy]):
            problems.append(f"{plan.nas_folders[fy]} already exists")
        try:
            if cloud_prefix_exists(plan.bucket, plan.cloud_prefixes[fy]):
                problems.append(f"cloud prefix {plan.bucket}/{plan.cloud_prefixes[fy]} already holds objects")
        except Exception as e:  # noqa: BLE001
            problems.append(f"cloud prefix {plan.bucket}/{plan.cloud_prefixes[fy]} could not be checked ({e})")
    if plan.scratch.exists() and any(plan.scratch.iterdir()):
        problems.append(f"{plan.scratch} already exists and is not empty")
    return problems


def check_sandbox(sandbox_cfg, prod_cfg, plan: Plan) -> list[str]:
    """The sandbox config the flows will run on cannot touch production state or shut the NAS down."""
    problems = []
    norm = lambda p: os.path.normcase(os.path.normpath(str(p)))  # noqa: E731
    if norm(sandbox_cfg.paths.database_path) == norm(prod_cfg.paths.database_path):
        problems.append("the sandbox would use the production manifest.db")
    if norm(sandbox_cfg.paths.backup_lock_path) == norm(prod_cfg.paths.backup_lock_path):
        problems.append("the sandbox would use the production backup.lock")
    if norm(sandbox_cfg.paths.runtime_dir) == norm(prod_cfg.paths.runtime_dir):
        problems.append("the sandbox would use the production runtime folder")
    if not norm(sandbox_cfg.paths.database_path).startswith(norm(plan.scratch)):
        problems.append("the sandbox database is not under the scratch folder")
    if sandbox_cfg.lan.shutdown_after_backup or sandbox_cfg.lan.shutdown_on_all_retries_exhausted:
        problems.append("NAS shutdown is not switched off in the sandbox config")
    if sandbox_cfg.cloud.bucket != prod_cfg.cloud.bucket:
        problems.append("the sandbox bucket differs from the configured one")
    if _fy_leaf(sandbox_cfg.paths.source_drive) != REHEARSAL_FY or _fy_leaf(sandbox_cfg.paths.lan_destination) != REHEARSAL_FY:
        problems.append("the sandbox source / NAS folder is not the rehearsal year")
    if not is_rehearsal_path(sandbox_cfg.paths.lan_destination, parent=plan.nas_parent):
        problems.append("the sandbox NAS folder is not a rehearsal folder directly in the share")
    return problems


def run_safety_checks(prod_cfg, plan: Plan, *, now: datetime, min_gap_minutes: int,
                      read_lock=None, get_active_slots=None, path_exists=None,
                      cloud_prefix_exists=None, nas_share_reachable=None,
                      sandbox_cfg=None) -> list[str]:
    problems = []
    problems += check_production_lock(prod_cfg, read_lock)
    problems += check_slot(get_active_slots)
    problems += check_schedule(prod_cfg, now, min_gap_minutes)
    problems += check_targets(
        plan, path_exists=path_exists or (lambda p: os.path.exists(p)),
        cloud_prefix_exists=cloud_prefix_exists or (lambda b, p: _cloud_prefix_exists(prod_cfg, b, p)),
        nas_share_reachable=nas_share_reachable or (lambda p: os.path.isdir(p)))
    if sandbox_cfg is not None:
        problems += check_sandbox(sandbox_cfg, prod_cfg, plan)
    return problems


# ---- cloud (rclone through core.tool_runner) -----------------------------------------------------

def _rclone(prod_cfg, name: str, args: list[str], timeout_s: int = 600):
    from core.process import resolve_binary
    from core.rclone_config import temp_rclone_config
    from core.run_artefacts import RunArtefacts
    from core.tool_runner import run_tool

    artefacts = RunArtefacts.ephemeral("overlap-rehearsal")
    try:
        with temp_rclone_config(prod_cfg.paths.gcs_key_path, prod_cfg.cloud.location,
                                prod_cfg.cloud.project_number, prod_cfg.cloud.storage_class) as conf:
            res = run_tool(name, [resolve_binary("rclone") or "rclone", *args, "--config", conf,
                                  "--gcs-no-check-bucket"], timeout_s=timeout_s, artefacts=artefacts)
        out = Path(res.stdout_path).read_text(encoding="utf-8", errors="replace") if res.stdout_path else ""
        return res, out
    finally:
        shutil.rmtree(artefacts.root, ignore_errors=True)


def _cloud_prefix_exists(prod_cfg, bucket: str, prefix: str) -> bool:
    if not is_rehearsal_prefix(prefix):
        raise Refusal(f"{prefix!r} is not a rehearsal prefix")
    res, out = _rclone(prod_cfg, "rclone-rehearsal-ls",
                       ["lsjson", f"{CLOUD_REMOTE}:{bucket}/{prefix}", "-R", "--files-only"])
    if res.exit_code not in (0, 3):                 # 3 = directory not found: nothing there
        raise RuntimeError(f"rclone lsjson exit {res.exit_code}")
    return bool(json.loads(out or "[]")) if res.exit_code == 0 else False


def _cloud_file_count(prod_cfg, bucket: str, prefix: str) -> int:
    res, out = _rclone(prod_cfg, "rclone-rehearsal-count",
                       ["lsjson", f"{CLOUD_REMOTE}:{bucket}/{prefix}", "-R", "--files-only"])
    return len(json.loads(out or "[]")) if res.exit_code == 0 else 0


def _cloud_purge(prod_cfg, bucket: str, prefix: str) -> None:
    if not is_rehearsal_prefix(prefix):
        raise Refusal(f"refusing to purge {prefix!r}: not a rehearsal prefix")
    res, _ = _rclone(prod_cfg, "rclone-rehearsal-purge", ["purge", f"{CLOUD_REMOTE}:{bucket}/{prefix}"])
    if res.exit_code not in (0, 3):
        raise RuntimeError(f"rclone purge of {bucket}/{prefix} failed (exit {res.exit_code})")


# ---- execution ---------------------------------------------------------------------------------

@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))
        print(f"  [{'OK' if ok else 'FAIL'}] {name}{(': ' + detail) if detail else ''}")

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(ok for _n, ok, _d in self.checks)


def seed_sources(plan: Plan) -> None:
    for fy in REHEARSAL_FYS:
        folder = plan.sources[fy]
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(1, SEED_FILES_PER_FOLDER + 1):
            (folder / f"rehearsal_{fy}_{i:02d}.txt").write_text(
                f"FY overlap rehearsal file {i} of {fy}\n" * 20, encoding="utf-8")


def remove_nas_folder(path: str, plan: Plan) -> bool:
    """Delete one rehearsal folder on the NAS (only a rehearsal leaf directly in the share)."""
    if not is_rehearsal_path(path, parent=plan.nas_parent):
        raise Refusal(f"refusing to delete {path}: not a rehearsal folder in {plan.nas_parent}")
    if not os.path.isdir(path):
        return False
    shutil.rmtree(path)
    return True


def cleanup(prod_cfg, plan: Plan, report: Report | None = None) -> bool:
    """Remove every test folder and prefix. Returns True when nothing is left."""
    clean = True
    for fy in REHEARSAL_FYS:
        for what, fn in (("NAS folder", lambda f=fy: remove_nas_folder(plan.nas_folders[f], plan)),
                         ("cloud prefix", lambda f=fy: _cloud_purge(prod_cfg, plan.bucket, plan.cloud_prefixes[f]))):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - keep going, report what is left
                clean = False
                print(f"  cleanup of {what} {fy} FAILED: {e}")
    if plan.scratch.name == SCRATCH_NAME and plan.scratch.exists():
        shutil.rmtree(plan.scratch, ignore_errors=True)
        clean = clean and not plan.scratch.exists()
    if report is not None:
        report.add("test folders and prefixes removed", clean)
    return clean


def _run_flows(prod_cfg, plan: Plan, report: Report) -> None:
    """Patch the run date and the shutdown task, run one cloud and one LAN flow, then verify."""
    import pendulum

    from core import fy_overlap

    os.environ["AAM_CONFIG"] = str(plan.sandbox_config)
    import models.config as mc
    mc.CONFIG_PATH = str(plan.sandbox_config)
    import flow
    from core.manifest import ManifestDB

    original_run_date = fy_overlap.run_date
    original_shutdown = flow.lan_shutdown_task
    shutdown_calls: list = []
    fy_overlap.run_date = lambda scheduled=None: pendulum.date(*plan.run_date)
    flow.lan_shutdown_task = lambda *a, **k: shutdown_calls.append((a, k))        # never shuts the NAS down
    try:
        for mode in ("cloud", "lan"):
            print(f"Running the {mode} flow ...")
            try:
                flow.backup(config_path=str(plan.sandbox_config), mode=mode)
                report.add(f"{mode} flow finished", True)
            except BaseException as e:  # noqa: BLE001 - the checks below say what is missing
                report.add(f"{mode} flow finished", False, f"{type(e).__name__}: {e}")
        report.add("NAS shutdown never called", not shutdown_calls)
        sandbox_cfg = mc.load_config(str(plan.sandbox_config))
        db = ManifestDB.from_config(sandbox_cfg)
        try:
            _verify(prod_cfg, sandbox_cfg, plan, db, report)
        finally:
            db.close()
    finally:
        fy_overlap.run_date = original_run_date
        flow.lan_shutdown_task = original_shutdown


def _verify(prod_cfg, sandbox_cfg, plan: Plan, db, report: Report) -> None:
    import pendulum

    from core.fy_overlap import active_folders

    run_date = pendulum.date(*plan.run_date)
    report.add("next year activated", db.get_activation(REHEARSAL_NEXT_FY) is not None)
    report.add("active folders are [next, configured]",
               list(active_folders(sandbox_cfg, db, run_date)) == [REHEARSAL_NEXT_FY, REHEARSAL_FY])
    mail = db.last_notification(f"overlap:start:{REHEARSAL_NEXT_FY}")
    report.add("INFO mail recorded", bool(mail) and mail.get("kind") == "INFO",
               f"status {mail.get('status')}" if mail else "no notification row")
    report.add("INFO mail sent", bool(mail) and mail.get("status") == "SENT")
    for fy in REHEARSAL_FYS:
        for leg in ("cloud", "lan"):
            rows = [r for r in db.get_recent_runs(50, fy=fy) if r.get("mode") == leg]
            statuses = ",".join(str(r.get("status")) for r in rows)
            report.add(f"{leg} run of {fy}", bool(rows), statuses or "no run row")
    # two-card dashboard: the real /status builder on the sandbox
    import ui
    ui._config = None
    ui._DB_INSTANCE = None
    folders = ui._folders(db, sandbox_cfg, {"cloud": False, "lan": False})
    report.add("dashboard shows two cards, new year first",
               [f["fy"] for f in folders] == [REHEARSAL_NEXT_FY, REHEARSAL_FY]
               and [f["role"] for f in folders] == ["early", "current"],
               ",".join(f"{f['fy']}:{f['role']}" for f in folders))
    for fy in REHEARSAL_FYS:
        nas = plan.nas_folders[fy]
        files = [n for n in os.listdir(nas) if not n.startswith(".")] if os.path.isdir(nas) else []
        report.add(f"files on the NAS in {fy}", len(files) >= SEED_FILES_PER_FOLDER, f"{len(files)} files")
        try:
            count = _cloud_file_count(prod_cfg, plan.bucket, plan.cloud_prefixes[fy])
        except Exception as e:  # noqa: BLE001
            count, e_text = 0, str(e)
            report.add(f"files in the cloud under {fy}", False, e_text)
            continue
        report.add(f"files in the cloud under {fy}", count >= SEED_FILES_PER_FOLDER, f"{count} objects")


def execute(prod_cfg, raw: dict, plan: Plan, report: Report, *, keep: bool) -> bool:
    """Create the sandbox, run the flows, verify, clean up. Assumes the safety checks passed."""
    from models.config import AppConfig, _drop_deprecated_keys

    plan.scratch.mkdir(parents=True, exist_ok=True)
    try:
        sandbox_raw = build_sandbox_raw(_drop_deprecated_keys(raw), plan)
        sandbox_cfg = AppConfig(**sandbox_raw)
        problems = check_sandbox(sandbox_cfg, prod_cfg, plan)
        if problems:
            raise Refusal("; ".join(problems))
        import yaml
        plan.sandbox_config.write_text(yaml.safe_dump(sandbox_raw, sort_keys=False), encoding="utf-8")
        seed_sources(plan)
        # the configured year's NAS folder is created here (marker as the mirror expects); the next year's
        # NAS folder is created by the flow itself (fy_overlap.ensure_overlap_nas_folder)
        from core.guards import MOUNT_MARKER
        nas = plan.nas_folders[REHEARSAL_FY]
        os.mkdir(nas)
        Path(nas, MOUNT_MARKER).touch()
        _run_flows(prod_cfg, plan, report)
    finally:
        if keep:
            print(f"--keep: nothing removed. Look around, then run with --cleanup.  Scratch: {plan.scratch}")
        else:
            cleanup(prod_cfg, plan, report)
    return report.ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--execute", action="store_true", help="really run (default is a dry run)")
    ap.add_argument("--keep", action="store_true", help="with --execute: leave the test folders in place")
    ap.add_argument("--cleanup", action="store_true", help="only remove the leftovers of an earlier run")
    ap.add_argument("--config", default=None, help="production config.yaml (read only); default: AAM_CONFIG / repo")
    ap.add_argument("--scratch", default=None, help="scratch folder (default: <runtime>\\_overlap_rehearsal)")
    ap.add_argument("--min-gap-minutes", type=int, default=DEFAULT_MIN_GAP_MINUTES,
                    help="refuse when a scheduled backup/audit is due within this many minutes")
    args = ap.parse_args(argv)

    import yaml

    from models.config import CONFIG_PATH, AppConfig, _drop_deprecated_keys
    config_path = args.config or CONFIG_PATH
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    prod_cfg = AppConfig(**_drop_deprecated_keys(json.loads(json.dumps(raw))))
    try:
        plan = build_plan(prod_cfg, Path(args.scratch) if args.scratch else None)
    except Refusal as e:
        print(f"REFUSED: {e}")
        return 2

    if args.cleanup:
        print("Cleanup of rehearsal leftovers:" + ("" if args.execute else "  (dry run: add --execute)"))
        for fy in REHEARSAL_FYS:
            print(f"  would remove {plan.nas_folders[fy]} and cloud prefix {plan.bucket}/{fy}")
        print(f"  would remove {plan.scratch}")
        if not args.execute:
            return 0
        return 0 if cleanup(prod_cfg, plan) else 1

    print("FY overlap rehearsal - " + ("EXECUTE" if args.execute else "DRY RUN (nothing is changed)"))
    print("It WOULD do:" if not args.execute else "It will do:")
    for line in plan.lines():
        print("  - " + line)

    problems = run_safety_checks(prod_cfg, plan, now=datetime.now().astimezone(),
                                 min_gap_minutes=args.min_gap_minutes)
    if problems:
        print("\nSAFETY CHECKS FAILED, it would NOT run:")
        for p in problems:
            print("  - " + p)
        return 2
    print("\nSafety checks passed (no backup running, targets free, NAS share reachable).")
    if not args.execute:
        print("Dry run only. Add --execute to run it.")
        return 0

    report = Report()
    ok = execute(prod_cfg, raw, plan, report, keep=args.keep)
    print("\nRESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
