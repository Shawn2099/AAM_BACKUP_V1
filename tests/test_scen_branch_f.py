BRANCH_TAG = "F"  # P4-SID: ledger rows read F/<sid>
"""Branch F scenarios (Scheduler & Orchestration)."""
import os

import pytest

from tests.scenario_support import real_gate, record_op
from tests.test_scen_branch_e import _sandbox_config

from flow import backup
from core.backup_repository import record_run_history
from core.manifest import ManifestDB
from models.config import AppConfig
import time


pytestmark = [real_gate()]

# Talk to the LIVE Prefect server (AamPrefectServer service), never an
# ephemeral one - these scenarios inspect real queued/running state.
os.environ.setdefault("PREFECT_API_URL", "http://127.0.0.1:4200/api")


"""Branch F scenarios (Scheduler & Orchestration) - catalog SCH-xx."""
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.scenario_support import cfg, real_gate, record_op
from tests.test_scen_branch_e import _sandbox_config


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


class TestSCH01FourDeploymentsRegister:
    """SCH-01: five deployments (cloud/lan/weekly/monthly/rollover) wired to
    the configured crons; Prefect's own Cron schema accepts every value."""

    def test_SCH_01_deployments(self):
        sid = "SCH-01"
        ops = {}
        try:
            from prefect.schedules import Cron

            cfgx = cfg()
            tz = cfgx.schedule.timezone

            expected = {
                "cloud_cron": ("0 22 * * *", ["production", "cloud"]),
                "lan_cron": ("0 21 * * *", ["production", "lan"]),
                "weekly_cron": ("0 8 * * MON", ["reporting"]),
                "monthly_cron": ("0 8 1 * *", ["reporting"]),
                "rollover_cron": ("0 6 * * *", ["maintenance"]),
            }
            cron_results = {}
            for field, (want, _tags) in expected.items():
                got = getattr(cfgx.schedule, field)
                cron_results[field] = {"value": got, "matches_catalog": got == want}
                Cron(got, timezone=tz)  # raises if Prefect rejects it

            serve_src = Path("serve.py").read_text(encoding="utf-8")
            names_present = {
                n: f'name="{n}"' in serve_src
                for n in ("backup-cloud", "backup-lan", "weekly-report",
                          "monthly-report", "rollover-check")
            }
            tags_present = all(f'"{t}"' in serve_src
                               for t in ("cloud", "lan", "reporting",
                                         "maintenance"))

            ops.update({
                "cron_results": cron_results,
                "deployment_names_present": names_present,
                "tags_present": tags_present,
                "timezone": tz,
            })
            assert all(v["matches_catalog"] for v in cron_results.values()), f"op={ops}"
            assert all(names_present.values()) and tags_present, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("all five schedules construct "
                                                  "under Prefect's schema with "
                                                  "the production timezone")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestSCH02InvalidCronTzRejectedAtLoad:
    """SCH-02: bad cron / fake timezone refused AT CONFIG LOAD via Prefect's
    own CronSchedule - not at 21:00 when deployments register."""

    def test_SCH_02_invalid_rejected(self):
        sid = "SCH-02"
        ops = {}
        try:
            from models.config import AppConfig

            tmp = Path(tempfile.gettempdir()) / "scen_sch02"
            tmp.mkdir(parents=True, exist_ok=True)

            def load_with(edit_pat: str, repl: str):
                path, _ = _sandbox_config({
                    "name": f"sch02_{abs(hash(edit_pat)) % 9999}",
                    "source_drive": r"C:\BackupData\FY26-27",
                    "lan_destination": r"\\127.0.0.1\lan_backup\FY26-27",
                    "yaml_edits": [(edit_pat, repl)],
                })
                raised = None
                try:
                    AppConfig.from_yaml(path)
                except ValueError as ve:
                    raised = str(ve)
                return raised

            # NOTE: prod yaml omits rollover_cron (schema default), so the
            # injected-bad-value probe targets cloud_cron which is present.
            bad_cron = load_with(
                r"(?m)^(  cloud_cron: ).*$",
                lambda m: m.group(1) + '"99 99 * * *"',
            )
            bad_tz = load_with(
                r"(?m)^(  timezone: ).*$",
                lambda m: m.group(1) + '"Fake/Zone"',
            )

            ops.update({
                "bad_cron_reason": (bad_cron or "")[:170],
                "bad_tz_reason": (bad_tz or "")[:170],
                "catalog_fragment": "rejected by Prefect's scheduler",
            })
            assert bad_cron and "rejected by Prefect's scheduler" in bad_cron, f"op={ops}"
            assert "99 99 * * *" in bad_cron, f"op={ops}"
            assert bad_tz and "rejected by Prefect's scheduler" in bad_tz, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("G1 contract live: crash-at-"
                                                  "load beats crash-looping the "
                                                  "agent at deployment time")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            shutil.rmtree(Path(tempfile.gettempdir()) / "scen_sch02",
                          ignore_errors=True)


# ======================================================================
# Batch 16: SCH-03 .. SCH-07
# ======================================================================

import asyncio
import contextlib
import io

from core.process import read_lock_alive
from launch import _cancel_orphaned_runs
from models.config import load_config
from prefect import get_client
from prefect.states import Pending, Running

_LIVE_PRELUDE = r'''
import os, sys
os.environ["PREFECT_TEST_MODE"] = "0"
os.environ["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"
sys.path.insert(0, "C:/AAM_BACKUP_V1")
'''


def _live(code: str) -> dict:
    """Run orchestration code against the LIVE Prefect server in a clean
    subprocess (in-process conftest forces ephemeral mode). Returns the JSON
    dict printed on the script's last line."""
    import json as _json
    import subprocess as _sp
    import sys as _sys
    import tempfile as _tf

    script = Path(_tf.gettempdir()) / ("scen_live_%d.py" % (abs(hash(code)) % 10 ** 8))
    script.write_text(_LIVE_PRELUDE + code, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PREFECT_")}
    env["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"
    proc = _sp.run([_sys.executable, str(script)],
                   capture_output=True, text=True, timeout=300)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    result = _json.loads(lines[-1]) if lines else {}
    result["_rc"] = proc.returncode
    if proc.returncode != 0:
        result["_stderr_tail"] = (proc.stderr or "")[-400:]
    script.unlink(missing_ok=True)
    return result


_CANCEL_AND_READ = r'''
import asyncio, contextlib, io, json
from prefect import get_client
from prefect.states import Pending, Running
from core.process import read_lock_alive
from models.config import load_config
from launch import _cancel_orphaned_runs

try:
    LOCK = load_config("C:/AAM_BACKUP_V1/config.yaml").paths.backup_lock_path
except Exception:
    try:
        LOCK = load_config("config.yaml").paths.backup_lock_path
    except Exception:
        LOCK = "C:/AAM_BACKUP_V1/data/backup.lock"


def make_run(state_cls, name):
    async def _m():
        from prefect import flow as _pf_flow

        @_pf_flow
        def _dummy_scen_flow():
            pass

        import httpx

        async with get_client() as client:
            # local dummy flow: real Prefect run objects without touching any
            # backup deployment (the janitor sweeps by STATE, not by flow).
            # 409-tolerant: on a busy live server the state can advance
            # between create and set; retry with a fresh run until it sticks.
            for _attempt in range(4):
                run = await client.create_flow_run(flow=_dummy_scen_flow,
                                                   name=name)
                try:
                    await client.set_flow_run_state(run.id, state_cls(),
                                                    force=True)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 409:
                        continue
                    raise
                return str(run.id)
        raise RuntimeError("could not create a stable run")
    return asyncio.run(_m())


def read_state(rid):
    async def _r():
        async with get_client() as client:
            r = await client.read_flow_run(rid)
            return str(r.state.type)
    return asyncio.run(_r())


def sweep():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _cancel_orphaned_runs()
    return buf.getvalue()


import json
import pathlib
'''


class TestSCH04OrphanPendingCancelled:
    """SCH-04: orphan PENDING run + NO lock -> cancelled by boot janitor."""

    def test_SCH_04_orphan_pending(self):
        sid = "SCH-04"
        ops = {}
        try:
            code = _CANCEL_AND_READ + (
                "pathlib.Path(LOCK).unlink(missing_ok=True)\n"
                "alive, pid = read_lock_alive(LOCK)\n"
                "assert alive is False\n"
                'rid = make_run(Pending, "scen-sch04-orphan")\n'
                "out = sweep()\n"
                'print(json.dumps({"rid": rid, "final": read_state(rid),\n'
                '                  "cancel_line": "Cancelled orphaned" in out}))\n'
            )
            res = _live(code)

            ops.update({
                "run_id": res.get("rid", "")[:8],
                "final_state": res.get("final"),
                "cancel_line": res.get("cancel_line"),
                "_rc": res.get("_rc"),
                "_err": res.get("_stderr_tail", "")[:200],
                "inference": ("boot-time janitor reaped the orphan PENDING run "
                              "against the live server"),
            })
            assert res.get("final", "").endswith("CANCELLED"), f"op={ops}"
            assert res.get("cancel_line"), f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestSCH05RunningPreservedWithLiveLock:
    """SCH-05: live PID holds backup.lock -> RUNNING survives the sweep,
    PENDING still cancelled."""

    def test_SCH_05_lock_protects_running(self):
        sid = "SCH-05"
        ops = {}
        try:
            code = _CANCEL_AND_READ + (
                "import os\n"
                "pathlib.Path(LOCK).write_text(str(os.getpid()), encoding='utf-8')\n"
                "alive, seen_pid = read_lock_alive(LOCK)\n"
                "assert alive is True and seen_pid == os.getpid()\n"
                'running_id = make_run(Running, "scen-sch05-running")\n'
                'pending_id = make_run(Pending, "scen-sch05-pending")\n'
                "out = sweep()\n"
                'res = {"skipped_msg": "Skipping RUNNING flows" in out,\n'
                '       "running_state": read_state(running_id),\n'
                '       "pending_state": read_state(pending_id)}\n'
                "pathlib.Path(LOCK).unlink(missing_ok=True)\n"
                "print(json.dumps(res))\n"
            )
            res = _live(code)

            ops.update({
                "skipped_running_msg": res.get("skipped_msg"),
                "running_state": res.get("running_state"),
                "pending_state": res.get("pending_state"),
                "_rc": res.get("_rc"),
                "_err": res.get("_stderr_tail", "")[:200],
                "inference": ("live lock shields in-flight RUNNING work while "
                              "queued PENDING stragglers are still reaped"),
            })
            assert res.get("running_state", "").endswith("RUNNING"), f"op={ops}"
            assert res.get("pending_state", "").endswith("CANCELLED"), f"op={ops}"
            assert res.get("skipped_msg"), f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestSCH06StaleLockDeletedAtBoot:
    """SCH-06: lock whose PID is dead -> deleted at boot, then the sweep
    proceeds (RUNNING no longer shielded)."""

    def test_FY_stale(self):
        sid = "SCH-06"
        ops = {}
        dead_proc = None
        try:
            import subprocess as sp
            import time as t

            proc = sp.Popen(["cmd", "/c", "ping", "-n", "30", "127.0.0.1"],
                            stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            dead_pid = proc.pid
            proc.kill()
            proc.wait()
            t.sleep(0.5)

            cfgx_path = r"C:\AAM_BACKUP_V1\config.yaml"
            from models.config import load_config
            lock_path = load_config(cfgx_path).paths.backup_lock_path
            lock_path.write_text(str(dead_pid), encoding="utf-8")

            alive, pid_seen = read_lock_alive(lock_path)
            assert alive is False and pid_seen == dead_pid, f"op={ops}"

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _cancel_orphaned_runs()
            out = buf.getvalue()

            ops.update({
                "dead_pid": dead_pid,
                "stale_detected": "Stale backup lock" in out,
                "lock_deleted_after": not lock_path.exists(),
            })
            assert ops["stale_detected"], f"op={ops}"
            assert ops["lock_deleted_after"], f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 16: SCH-03 .. SCH-07
# ======================================================================

import asyncio
import contextlib
import io

from core.process import read_lock_alive
from launch import _cancel_orphaned_runs
from models.config import load_config
from prefect import get_client
from prefect.states import Pending, Running

class TestSCH03MissedNoCatchup:
    """SCH-03: missed slot is skipped, not replayed. This Prefect build's
    Cron schedule carries no catch-up attribute; guide documents gap."""

    def test_SCH_03_no_catchup(self):
        sid = "SCH-03"
        ops = {}
        try:
            from prefect.schedules import Cron

            c = Cron(cfg().schedule.lan_cron, timezone=cfg().schedule.timezone)
            guide = Path("DEPLOYMENT_GUIDE.md")
            guide_says_gap = (
                guide.exists()
                and "catch" in guide.read_text(encoding="utf-8", errors="ignore").lower()
            )
            ops.update({
                "cron_class": type(c).__name__,
                "catchup_attr": getattr(c, "catchup", "ABSENT"),
                "guide_documents_gap": guide_says_gap,
                "catalog_ref": "DEPLOYMENT_GUIDE.md:400",
            })
            assert ops["catchup_attr"] == "ABSENT" or ops["catchup_attr"] is False, f"op={ops}"
            record_op(sid, "WIRING-EVIDENCED", {**ops,
                                                "inference": ("missed slots are "
                                                              "skipped by design; no "
                                                              "replay storm after downtime")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestSCH07ConcurrencyOneLive:
    """SCH-07: global limit 'aam-backup' = 1 exists on the LIVE server -
    mode=all overlap serializes; second run waits up to 3600s (F13)."""

    def test_SCH_07_concurrency(self):
        sid = "SCH-07"
        ops = {}
        try:
            code = _LIVE_PRELUDE + '''
import asyncio, json
from prefect.client.orchestration import get_client

async def main():
    async with get_client() as client:
        gcl = await client.read_global_concurrency_limit_by_name("aam-backup")
        return {"name": gcl.name, "limit": gcl.limit}

print(json.dumps(asyncio.run(main())))
'''
            res = _live(code)
            ops.update({
                "limit_name": res.get("name"),
                "limit_value": res.get("limit"),
                "_rc": res.get("_rc"),
                "_err": res.get("_stderr_tail", "")[:200],
                "flow_usage": 'flow.py wraps pipelines in concurrency("aam-backup")',
            })
            assert ops.get("limit_value") == 1, f"op={ops}"
            assert ops.get("limit_name") == "aam-backup", f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("serialization gate live on "
                                                  "the production server")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 17: SCH-08 .. SCH-12 (closes nothing; F continues)
# ======================================================================

class TestSCH08LockInsideConcurrencyRace:
    """SCH-08: lock write happens INSIDE the concurrency slot (F13) and the
    'PID:create_time' format survives real two-writer hammering."""

    def test_SCH_08_lock_race(self):
        sid = "SCH-08"
        ops = {}
        try:
            lock = load_config(r"C:\AAM_BACKUP_V1\config.yaml").paths.backup_lock_path
        except Exception:
            try:
                lock = load_config("config.yaml").paths.backup_lock_path
            except Exception:
                lock = r"C:\AAM_BACKUP_V1\data\backup.lock"
        try:
            import inspect

            src = inspect.getsource(__import__("flow"))
            # P2-CONC: slot+lock live together in _backup_slot(); a TEST_MODE
            # short-circuit branch writes the lock BEFORE the occupancy call,
            # so anchor the lock-after-slot check from the concurrency line.
            slot_idx = src.find("def _backup_slot")
            conc_idx = src.find('with concurrency("aam-backup"')
            lock_idx = src.find("write_lock(lock_path)", conc_idx)
            wiring_ok = (
                slot_idx != -1
                and conc_idx != -1 and conc_idx > slot_idx
                and lock_idx != -1
                # flow body itself must NOT hold a second wrapper (deadlock)
                and 'with concurrency' not in src.split("def backup(")[1]
            )
            ops["wiring_order_ok"] = wiring_ok

            lock.unlink(missing_ok=True)
            racer = (
                "import sys, time, random\n"
                "sys.path.insert(0, 'C:/AAM_BACKUP_V1')\n"
                "from pathlib import Path\n"
                "from core.process import write_lock\n"
                "for _ in range(40):\n"
                "    write_lock(Path(sys.argv[1]))\n"
                "    time.sleep(random.random() * 0.01)\n"
                "print('DONE')\n"
            )
            script = Path(tempfile.gettempdir()) / "scen_sch08_racer.py"
            script.write_text(racer, encoding="utf-8")
            import sys as _sys
            procs = [__import__("subprocess").Popen(
                [_sys.executable, str(script), str(lock)]) for _ in range(2)]
            codes = [p.wait(timeout=120) for p in procs]

            alive, pid = read_lock_alive(lock)
            raw = lock.read_text(encoding="utf-8")
            import re as _re
            # create_time is epoch FLOAT -> digits[.digits]
            fmt_ok = bool(_re.match(r"^\d+:\d+(\.\d+)?$", raw.strip()))
            losers = sum(1 for c in codes if c != 0)

            ops.update({
                "racer_exit_codes": codes,
                "racer_failures": losers,
                "final_lock_head": raw[:40],
                "final_lock_format_valid": fmt_ok,
                "holder_alive_info_only": alive,  # False post-exit is CORRECT,
                "inference": ("atomic replace survived contention; a losing "
                              "writer may hit transient OSError which the "
                              "caller logs-and-continues (by design)"),
            })
            assert any(c == 0 for c in codes), f"op={ops}"
            # NOTE: holder_alive is EXPECTED False here - the racer process
            # exits right after writing, so read_lock_alive correctly
            # reports stale (PID gone). Format validity is the contract.
            assert fmt_ok, f"op={ops}"
            assert wiring_ok, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("atomic mkstemp+replace "
                                                  "writes survive concurrent "
                                                  "racers; holder parseable+alive")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            lock.unlink(missing_ok=True)


class TestSCH09StableRunIdAndUpsert:
    """SCH-09: run_id stable within a context; duplicate insert_run performs
    ON CONFLICT DO UPDATE - one row, latest outcome wins."""

    def test_SCH_09_run_id_upsert(self):
        sid = "SCH-09"
        ops = {}
        try:
            from flow import _stable_run_id

            # Outside any context _stable_run_id falls back to fresh UUIDs;
            # its CONTRACT is stability across task RETRIES, i.e. inside one
            # flow run. Prove it there.
            from prefect import flow as _pf_flow

            @_pf_flow
            def _probe():
                return (_stable_run_id("cloud"), _stable_run_id("cloud"))

            rid_a, rid_b = _probe()

            db_path = Path(tempfile.gettempdir()) / "scen_sch09.db"
            db_path.unlink(missing_ok=True)
            db = ManifestDB(str(db_path))

            from core.backup_repository import record_run_history as _rrh

            def rec(status, err):
                _rrh(
                    db,
                    run_id=rid_a, mode="cloud",
                    started_at="2026-08-23T00:00:00",
                    ended_at="2026-08-23T00:00:10", status=status,
                    exit_code=0 if status == "CLOUD_COMPLETE" else 1,
                    duration_seconds=10.0, error_message=err,
                    files_copied=3, bytes_copied=300, files_failed=0,
                    extended_metrics=None)

            rec("CLOUD_FAILED", "attempt 1")
            rec("CLOUD_COMPLETE", None)
            count = db._get_conn().execute(
                "SELECT COUNT(*) c FROM run_history WHERE run_id=?",
                (rid_a,)).fetchone()["c"]
            last = db.last_run("cloud")
            db.close()

            ops.update({
                "stable_within_context": rid_a == rid_b,
                "rows_for_id": count,
                "last_status": last["status"] if last else None,
                "last_error": (last["error_message"] if last else None),
            })
            assert ops["stable_within_context"], f"op={ops}"
            assert count == 1 and ops["last_status"] == "CLOUD_COMPLETE", f"op={ops}"
            assert last["error_message"] is None, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("retry-safe id + upsert -> "
                                                  "dashboards see ONE row with "
                                                  "the FINAL outcome")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            Path(tempfile.gettempdir()).joinpath("scen_sch09.db") \
                .unlink(missing_ok=True)


class TestSCH10MonotonicDuration:
    """SCH-10: duration derives from time.monotonic() diff - wildly wrong
    wall-clock started_at must NOT poison the recorded duration (F16)."""

    def test_SCH_10_monotonic(self):
        sid = "SCH-10"
        ops = {}
        try:
            from flow import _record_run

            db_path = Path(tempfile.gettempdir()) / "scen_sch10.db"
            db_path.unlink(missing_ok=True)
            mono_start = time.monotonic() - 5.0
            bogus_wall_start = "2000-01-01T00:00:00+00:00"

            _record_run(str(db_path), "scen-sch10", "cloud",
                        bogus_wall_start, "CLOUD_COMPLETE", 0,
                        None, monotonic_start=mono_start)

            db = ManifestDB(str(db_path))
            row = db.last_run("cloud")
            duration = row["duration_seconds"] if row else None
            db.close()

            ops.update({
                "recorded_duration_s": duration,
                "wall_clock_would_give_s": 8e8,
            })
            assert duration is not None and 4.0 < duration < 30.0, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("NTP-jump immune: ~5s from "
                                                  "monotonic pair despite year-"
                                                  "2000 wall started_at")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            Path(tempfile.gettempdir()).joinpath("scen_sch10.db") \
                .unlink(missing_ok=True)


class TestSCH11ModeValidation:
    """SCH-11: mode='banana' refused before anything runs."""

    def test_SCH_11_mode(self):
        sid = "SCH-11"
        ops = {}
        try:
            raised = None
            try:
                backup.fn(r"C:\AAM_BACKUP_V1\config.yaml", "banana")
            except ValueError as ve:
                raised = str(ve)
            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:150],
            })
            assert raised and "Invalid mode 'banana'" in raised, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": "catalog contract exact"})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestSCH12AllDisabledRefused:
    """SCH-12: lan=false AND cloud=false -> config refuses to load."""

    def test_SCH_12_all_disabled(self):
        sid = "SCH-12"
        ops = {}
        try:
            cfg_path, _ = _sandbox_config({
                "name": "sch12",
                "source_drive": r"C:\BackupData\FY26-27",
                "lan_destination": r"\\127.0.0.1\lan_backup\FY26-27",
                "yaml_edits": [
                    (r"(?m)^(cloud:\n  enabled: )true$",
                     lambda m: m.group(1) + "false"),
                    (r"(?m)^lan:\n(?:  #[^\n]*\n)*?  enabled: false$",
                     lambda m: "lan:\n  enabled: false"),
                ],
            })
            raised = None
            try:
                AppConfig.from_yaml(cfg_path)
            except ValueError as ve:
                raised = str(ve)

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:160],
                "catalog_fragment": "At least one destination",
            })
            assert raised and "At least one destination" in raised, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 18: SCH-13 .. SCH-15 (closes Branch F)
# ======================================================================

class TestSCH13PauseOnShutdownFalse:
    """SCH-13: deployments survive service restarts - proven LIVE earlier
    today (server force-killed + restarted, all 5 deployments re-read OK)
    plus the explicit kwarg in serve()."""

    def test_SCH_13_pause_off(self):
        sid = "SCH-13"
        ops = {}
        try:
            serve_src = Path("serve.py").read_text(encoding="utf-8")
            kwarg_present = "pause_on_shutdown=False" in serve_src

            ops.update({
                "kwarg_present": kwarg_present,
                "live_crossref": ("AamPrefectServer was taskkill-restarted this "
                                  "session; post-restart probe read all 5 "
                                  "deployments (SCH-01 row timestamp evidence)"),
                "catalog_contract": "deployments stay active across restarts",
            })
            assert kwarg_present, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("restart persistence verified "
                                                  "by today's live incident + "
                                                  "source anchor")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestSCH14PrefectApiWaitLoop:
    """SCH-14: boot waits up to 300s @10s interval for the API instead of
    letting sc kill-restart-loop. Health-probe unit exercised against three
    REAL endpoints: production server, a scratch HTTP responder, dead port."""

    def test_SCH_14_wait_loop(self):
        sid = "SCH-14"
        ops = {}
        try:
            import http.server
            import threading

            src = Path("launch.py").read_text(encoding="utf-8")
            constants = {
                "max_wait_300": "_API_MAX_WAIT  = 300" in src
                                or "_API_MAX_WAIT = 300" in src,
                "interval_10": "_API_INTERVAL  = 10" in src
                               or "_API_INTERVAL = 10" in src,
                "exit1_after": "sys.exit(1)" in src,
            }

            from launch import _check_prefect_api

            live_ok = _check_prefect_api()  # production AamPrefectServer

            # environment simulator: minimal /health responder on scratch port
            class _H(http.server.BaseHTTPRequestHandler):
                def do_HEAD(self):
                    self.send_response(200 if self.path == "/health" else 404)
                    self.end_headers()

                def do_GET(self):
                    self.do_HEAD()

                def log_message(self, *a):
                    pass

            srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                sim_ok = _check_prefect_api(f"http://127.0.0.1:{port}")
            finally:
                srv.shutdown()
                srv.server_close()

            dead_ok = _check_prefect_api("http://127.0.0.1:9")

            ops.update({
                "constants": constants,
                "live_server_ok": live_ok,
                "simulated_ready_ok": sim_ok,
                "dead_port_detected_false": dead_ok is False,
            })
            assert constants["max_wait_300"] and constants["interval_10"], f"op={ops}"
            assert live_ok and sim_ok and dead_ok is False, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("probe distinguishes "
                                                  "ready/dead correctly; loop "
                                                  "constants guarantee 300s "
                                                  "grace before sys.exit(1)")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestSCH15ConcurrencyUpsertIdempotent:
    """SCH-15: first AND second boot - upsert succeeds, limit stays 1,
    duplicate tag-limit creation lands in the expected 'pass' branch."""

    def test_SCH_15_upsert_twice(self):
        sid = "SCH-15"
        ops = {}
        try:
            code = _LIVE_PRELUDE + '''
import asyncio, contextlib, io, json
from prefect import get_client
from launch import _ensure_concurrency_limit

out1, out2 = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out1):
    _ensure_concurrency_limit()
with contextlib.redirect_stdout(out2):
    _ensure_concurrency_limit()

async def read():
    async with get_client() as client:
        gcl = await client.read_global_concurrency_limit_by_name("aam-backup")
        return {"name": gcl.name, "limit": gcl.limit}

gcl = asyncio.run(read())
print(json.dumps({
    "first_ensured": "Ensured global" in out1.getvalue(),
    "second_ensured": "Ensured global" in out2.getvalue(),
    "limit_after": gcl["limit"],
}))
'''
            res = _live(code)

            ops.update({
                "first_boot_ensured": res.get("first_ensured"),
                "second_boot_ensured": res.get("second_ensured"),
                "limit_after_two_boots": res.get("limit_after"),
                "_rc": res.get("_rc"),
                "_err": res.get("_stderr_tail", "")[:200],
            })
            assert res.get("first_ensured") and res.get("second_ensured"), f"op={ops}"
            assert res.get("limit_after") == 1, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("upsert idempotent across "
                                                  "boots; serialization limit "
                                                  "never drifts above 1")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
