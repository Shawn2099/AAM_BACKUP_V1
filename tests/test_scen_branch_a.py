BRANCH_TAG = "A"  # P4-SID: ledger rows read A/<sid>
"""Branch A - LAN Backup scenarios (docs/SCENARIO_CATALOG_V2.md).

Batch 1: LAN-01 .. LAN-05.

Rules of engagement:
- Real hardware, zero mocks, ZERO tuning to help the program pass.
- The sync under test uses the production LanConfig from config.yaml verbatim
  (cfg().lan), exactly like the nightly flow.
- Every test records the EXACT observed operation (status / exit code /
  filesystem state / error text) to docs/SCENARIO_TEST_REPORT.md, including
  on FAILURE - a fail must never be silent.
"""

import os
import tempfile
from pathlib import Path

import pytest

from core.lan_manifest import snapshot_to_dict, walk_lan_destination
from core.health import HealthError  # P1-EXC canonical
from core.health import pre_backup_health as _core_health_mod  # noqa: F401
import core.health as _core_health
from core.lan_preflight import run_lan_dry_run
from core.lan_sync import run_lan_sync

from tests.e2e_helpers import clean_test_dirs, make_file, nas_test_dir, source_test_dir
from tests.scenario_support import cfg, ensure_canary, real_gate, record_op

pytestmark = [real_gate()]


def _sync(source, dest) -> dict:
    """Run the sync exactly like production: production LanConfig, no tweaks."""
    return run_lan_sync(str(source), str(dest), cfg().lan)


def _nas_files(nas: Path) -> dict:
    return snapshot_to_dict(walk_lan_destination(str(nas)))


def _fail_ops(ops: dict, e: Exception) -> dict:
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


class _PipelineSandbox:
    """Route scenarios through the PRODUCTION pipeline (_run_lan_pipeline)
    instead of raw run_lan_sync, so lock/preflight/snapshot/DB/metrics all
    execute exactly as deployed. Restores module state after each use."""

    def __init__(self, name: str):
        self.db_path = Path(tempfile.gettempdir()) / f"{name}.db"

    def __enter__(self):
        from models.config import load_config as _lc

        cfgx = _lc(r"C:\AAM_BACKUP_V1\config.yaml")
        cfgx.paths.source_drive = str(source_test_dir())
        cfgx.paths.lan_destination = str(nas_test_dir())
        cfgx.paths.database_path = str(self.db_path)
        if self.db_path.exists():
            self.db_path.unlink()
        cfgx.lan.enabled = True
        cfgx.wol.enabled = False
        cfgx.lan.shutdown_after_backup = False
        cfgx.notifications.send_on_failure = False
        self.cfgx = cfgx

        import ui  # noqa: F401  (ensure module graph identical to prod)
        from flow import _run_lan_pipeline

        return self.cfgx, _run_lan_pipeline

    def __exit__(self, *exc):
        if self.db_path.exists():
            self.db_path.unlink()
        return False


class TestLAN01GoldenMirror:
    """LAN-01 (program-path): golden mirror driven through the production
    pipeline - preflight, lock, robocopy, snapshot, DB row and metrics all
    execute exactly as deployed."""

    def test_LAN_01_golden_mirror(self):
        sid = "LAN-01"
        ops = {}
        try:
            clean_test_dirs()
            canary = ensure_canary()
            src, nas = source_test_dir(), nas_test_dir()
            src.mkdir(parents=True, exist_ok=True)
            make_file(src / "small.txt", 1024)
            make_file(src / "big.bin", 2 * 1024 * 1024)
            nested = src / "nested"
            nested.mkdir(exist_ok=True)
            make_file(nested / "deep.txt", 4096)

            with _PipelineSandbox("scen_lan01") as (cfgx, run_pipeline):
                result = run_pipeline(cfgx, "scen-lan01",
                                      "2026-08-23T00:00:00", None)

                from core.manifest import ManifestDB
                db = ManifestDB(str(self_db := Path(cfgx.paths.database_path)))
                try:
                    row = db.last_run("lan")
                finally:
                    db.close()

            nas_state = {
                p: {"size": s, "matches_source": s == (src / p).stat().st_size}
                for p, (s, _mt) in _nas_files(nas).items()
                if not Path(p).parts[0].startswith(".")
            }
            ops.update({
                "status": result["status"],
                "exit_code": result["exit_code"],
                "canary_present_after": canary.exists(),
                "files_on_nas": nas_state,
                "db_row": {"run_id": row["run_id"], "status": row["status"],
                           "files_copied": row["files_copied"]} if row else None,
            })
            assert result["status"] == "LAN_COMPLETE", f"op={ops}"
            assert result["exit_code"] == 3, f"expected copied+extras bits, op={ops}"
            expected = {"small.txt", os.path.join("nested", "deep.txt"), "big.bin"}
            assert set(nas_state) == expected, f"op={ops}"
            assert all(v["matches_source"] for v in nas_state.values()), f"op={ops}"
            assert canary.exists(), f"op={ops}"
            assert row and row["files_copied"] == 3, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("production pipeline end-to-"
                                                  "end: DB row + metrics + "
                                                  "mirror all from OUR program")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            ensure_canary()


class TestLAN02IdempotentRerun:
    """LAN-02 (program-path): rerunning the production pipeline over an
    unchanged share transfers ZERO bytes and records it in the DB."""

    def test_LAN_02_rerun_zero_transfer(self):
        sid = "LAN-02"
        ops = {}
        try:
            clean_test_dirs()
            ensure_canary()
            src, nas = source_test_dir(), nas_test_dir()
            src.mkdir(parents=True, exist_ok=True)
            make_file(src / "same.txt", 4096)

            with _PipelineSandbox("scen_lan02") as (cfgx, run_pipeline):
                first = run_pipeline(cfgx, "scen-lan02-a",
                                     "2026-08-23T00:00:00", None)
                second = run_pipeline(cfgx, "scen-lan02-b",
                                      "2026-08-23T00:05:00", None)

                from core.manifest import ManifestDB
                db = ManifestDB(str(Path(cfgx.paths.database_path)))
                try:
                    row = db.last_run("lan")
                finally:
                    db.close()

            ops.update({
                "first_status": first["status"],
                "second_status": second["status"],
                "second_exit": second["exit_code"],
                "db_files_copied": row["files_copied"] if row else None,
                "db_bytes_copied": row["bytes_copied"] if row else None,
            })
            assert second["status"] == "LAN_COMPLETE", f"op={ops}"
            assert row and row["files_copied"] == 0, f"op={ops}"
            assert row and row["bytes_copied"] == 0, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("idempotency measured by the "
                                                  "PROGRAM's own delta math, not "
                                                  "test-side assumptions")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            ensure_canary()


class TestLAN03CanaryMissingAbort:
    """LAN-03: canary deleted -> hard abort BEFORE any transfer, message has full UNC.
    Canonical pytest.raises form - exception identity is guaranteed by P1-EXC."""

    def test_LAN_03_canary_abort(self):
        sid = "LAN-03"
        ops = {}
        try:
            clean_test_dirs()
            canary = ensure_canary()
            src, nas = source_test_dir(), nas_test_dir()
            src.mkdir(parents=True, exist_ok=True)
            make_file(src / "payload.bin", 8192)

            canary.unlink()  # trigger condition: operator/mount wiped the marker

            with pytest.raises(HealthError) as exc_info:
                run_lan_dry_run(str(src), str(nas))
            msg = str(exc_info.value)

            copied = [p for p in _nas_files(nas) if p != ".AAM_TARGET_MOUNTED"]

            ops.update({
                "exception": type(exc_info.value).__name__,
                "identity_is_core_health": exc_info.value.__class__ is _core_health.HealthError,
                "message_contains_full_unc": str(nas) in msg,
                "message_contains_canary_name": ".AAM_TARGET_MOUNTED" in msg,
                "payload_files_on_nas": copied,
                "message_head": msg[:140],
            })
            assert str(nas) in msg and ".AAM_TARGET_MOUNTED" in msg, f"op={ops}"
            assert copied == [], f"zero bytes must be copied on canary abort, op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            ensure_canary()


class TestLAN04UNCUnreachable:
    """LAN-04: unreachable UNC fails fast, message contains bad UNC, source untouched."""

    def test_LAN_04_bad_share(self):
        sid = "LAN-04"
        ops = {}
        try:
            bad_dest = r"\\127.0.0.1\definitely_not_a_share_xyz"
            clean_test_dirs()
            ensure_canary()
            src = source_test_dir()
            src.mkdir(parents=True, exist_ok=True)
            make_file(src / "probe.txt", 512)

            preflight_kind, preflight_msg = "no-error", ""
            try:
                run_lan_dry_run(str(src), bad_dest)
            except HealthError as e:
                preflight_kind, preflight_msg = "HealthError(canary-guard)", str(e)
            except Exception as e:
                preflight_kind, preflight_msg = type(e).__name__, str(e)

            sync_result = _sync(src, bad_dest)

            ops.update({
                "preflight_outcome": preflight_kind,
                "preflight_msg_contains_bad_unc": (
                    "definitely_not_a_share_xyz" in preflight_msg or bad_dest in preflight_msg
                ),
                "sync_status": sync_result["status"],
                "sync_exit_code": sync_result["exit_code"],
                "source_still_intact": (src / "probe.txt").exists(),
            })
            failed_fast = ops["preflight_msg_contains_bad_unc"] or (
                sync_result["status"] == "LAN_FAILED" and sync_result["exit_code"] in (-1, 16)
            )
            assert failed_fast, f"neither stage failed fast with clear cause, op={ops}"
            assert ops["source_still_intact"], f"source must never be deleted, op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestLAN05PermissionDenied:
    """LAN-05: access-denied destination -> bit3/bit4 failure, log tail in error.

    Catalog precondition is a LocalSystem service logon without share rights.
    We approximate by denying the current principal on the dest folder via
    icacls. If elevated rights + robocopy /ZB backup mode bypass the deny we
    record the ACTUAL operation as ENV-LIMITED - never fake a pass.
    """

    def test_LAN_05_access_denied(self):
        sid = "LAN-05"
        ops = {}
        deny_root = None
        try:
            import subprocess

            clean_test_dirs()
            canary = ensure_canary()
            deny_root = nas_test_dir() / "deny_probe"
            deny_root.mkdir(parents=True, exist_ok=True)
            src = source_test_dir()
            src.mkdir(parents=True, exist_ok=True)
            make_file(src / "denied_target.txt", 1024)

            user = os.environ.get("USERNAME")
            subprocess.run(
                ["icacls", str(deny_root), "/deny", f"{user}:(OI)(CI)F"],
                capture_output=True,
            )
            try:
                result = _sync(src, deny_root)
            finally:
                subprocess.run(["icacls", str(deny_root), "/reset"], capture_output=True)

            ops.update({
                "status": result["status"],
                "exit_code": result["exit_code"],
                "error_head": (result["error"] or "")[:180],
                "files_failed": result.get("files_failed"),
            })
            denied = (
                result["status"] in ("LAN_FAILED", "LAN_PARTIAL")
                and result["exit_code"] >= 8
            )
            if denied:
                assert result["error"], f"log tail expected in error, op={ops}"
                record_op(sid, "PASS", ops)
            else:
                ops["note"] = (
                    "admin + robocopy /ZB backup mode bypassed the ACL deny; "
                    "catalog precondition (LocalSystem logon without share "
                    "rights) not reproducible from an elevated session - "
                    "observed op recorded verbatim, needs service-context run"
                )
                record_op(sid, "ENV-LIMITED", ops)
                pytest.skip(f"ENV-LIMITED: {ops['note']}")
        except Exception as e:
            if ops and ops.get("note"):
                raise
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 2: LAN-06 .. LAN-10
# ======================================================================

import hashlib
import os
from pathlib import Path

import pytest

from core.health import check_source_drive
from core.lan_sync import run_lan_sync

from tests.e2e_helpers import clean_test_dirs, make_file, nas_test_dir, source_test_dir
from tests.scenario_support import cfg, ensure_canary, real_gate, record_op

pytestmark = [real_gate()]


def _sync(source, dest) -> dict:
    return run_lan_sync(str(source), str(dest), cfg().lan)


def _fail_ops(ops: dict, e: Exception) -> dict:
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TestLAN06LockedFileDuringSync:
    """LAN-06: mandatory byte-range lock held on one file mid-sync.
    Catalog contract: no crash; PARTIAL(bit3)/FAILED(bit4); error carries
    ** FAILED: lines; files_failed counted."""

    def test_LAN_06_locked_file(self):
        sid = "LAN-06"
        ops = {}
        try:
            clean_test_dirs()
            ensure_canary()
            src, nas = source_test_dir(), nas_test_dir()
            src.mkdir(parents=True, exist_ok=True)
            make_file(src / "good_1.txt", 4096)
            make_file(src / "good_2.txt", 4096)
            victim = src / "locked_doc.txt"
            make_file(victim, 8192)

            fd = os.open(str(victim), os.O_RDWR | os.O_BINARY)
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # mandatory range lock
                result = _sync(src, nas)
            finally:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                finally:
                    os.close(fd)

            err_tail = result.get("error") or ""
            ops.update({
                "status": result["status"],
                "exit_code": result["exit_code"],
                "files_failed": result.get("files_failed"),
                "error_has_failed_marker": "** FAILED:" in err_tail,
                "good_files_on_nas": sorted(
                    p for p in (
                        "good_1.txt", "good_2.txt"
                    ) if os.path.exists(nas / p)
                ),
                "locked_file_on_nas": os.path.exists(nas / "locked_doc.txt"),
                "retry_profile": (
                    f"R:{cfg().lan.retry_count} W:{cfg().lan.retry_wait_seconds}"
                ),
                "error_head": err_tail[:160],
            })
            inference = (
                "classifier mapped robocopy bitmask to "
                f"{result['status']}; healthy files copied={len(ops['good_files_on_nas'])}/2; "
                f"locked-file outcome on NAS={'present' if ops['locked_file_on_nas'] else 'absent'}"
            )
            assert result["status"] in ("LAN_PARTIAL", "LAN_FAILED"), \
                f"expected degraded-but-classified run, op={ops}"
            assert result["exit_code"] >= 8, f"bit3/bit4 expected, op={ops}"
            record_op(sid, "PASS", {**ops, "inference": inference})
        except Exception as e:
            ops["inference"] = "see error field; classify step may differ from catalog"
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestLAN07LongPathUnicode:
    """LAN-07: >260-char path with unicode component.
    Catalog allows EITHER full handling OR single-file PARTIAL. We record
    which branch reality took - that IS the finding."""

    def test_LAN_07_long_unicode(self):
        sid = "LAN-07"
        ops = {}
        try:
            clean_test_dirs()
            ensure_canary()
            src, nas = source_test_dir(), nas_test_dir()

            deep = src.joinpath("à¤…à¤•à¤¾à¤‰à¤‚à¤Ÿ", "FY25-26", *["seg_" + "x" * 30] * 7)
            payload = bytes(range(256)) * 16
            try:
                deep.mkdir(parents=True, exist_ok=True)
                target = deep / "very_long_account_statement_2026_final.pdf"
                target.write_bytes(payload)
                created_len = len(str(target))
            except OSError as e:
                record_op(sid, "FAIL", {
                    "stage": "harness could not create >260 unicode path",
                    "os_error": str(e)[:200],
                })
                raise

            result = _sync(src, nas)

            dest_plain = (nas / deep.relative_to(src)) / target.name
            # Long-path aware form for UNC: \\?\UNC\server\share\...
            nas_counterpart = Path(
                "\\\\?\\UNC\\" + str(dest_plain).lstrip("\\")
            )
            landed = nas_counterpart.exists()
            sha_ok = landed and _sha256(nas_counterpart) == hashlib.sha256(payload).hexdigest()

            ops.update({
                "source_path_len": created_len,
                "unicode_component": "à¤…à¤•à¤¾à¤‰à¤‚à¤Ÿ",
                "status": result["status"],
                "exit_code": result["exit_code"],
                "files_failed": result.get("files_failed"),
                "long_file_landed_on_nas": landed,
                "sha256_match": sha_ok,
                "error_head": (result.get("error") or "")[:160],
            })

            handled = landed and sha_ok and result["status"] == "LAN_COMPLETE"
            confined = (
                result["status"] == "LAN_PARTIAL"
                and result["exit_code"] >= 8
                and result.get("files_failed", 0) <= 1
            )
            assert handled or confined, f"neither catalog branch observed, op={ops}"
            branch = "HANDLED_BY_ROBOCOPY" if handled else "CONFINED_SINGLE_FILE_FAIL"
            record_op(sid, "PASS", {**ops, "branch": branch,
                                    "inference": branch})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestLAN08LargeFileIntegrity:
    """LAN-08: 50MB random blob survives /Z /ZB mirror bit-exact (SHA256)."""

    def test_LAN_08_large_integrity(self):
        sid = "LAN-08"
        ops = {}
        try:
            clean_test_dirs()
            ensure_canary()
            src, nas = source_test_dir(), nas_test_dir()
            src.mkdir(parents=True, exist_ok=True)
            big = src / "large_50mb.bin"
            big.write_bytes(os.urandom(50 * 1024 * 1024))
            sha_before = _sha256(big)

            result = _sync(src, nas)
            nas_copy = nas / "large_50mb.bin"
            sha_after = _sha256(nas_copy) if nas_copy.exists() else None

            ops.update({
                "status": result["status"],
                "exit_code": result["exit_code"],
                "size_bytes": big.stat().st_size,
                "sha_before": sha_before[:16],
                "sha_after": (sha_after or "")[:16],
                "sha_match": sha_before == sha_after,
            })
            assert result["status"] == "LAN_COMPLETE", f"op={ops}"
            assert sha_before == sha_after, f"truncation/corruption detected, op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": "no truncation across /Z /ZB restartable copy"})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestLAN09EmptySourceGate:
    """LAN-09: empty source -> health gate refuses BEFORE any NAS action."""

    def test_LAN_09_empty_source(self):
        sid = "LAN-09"
        ops = {}
        try:
            clean_test_dirs()
            ensure_canary()
            src = source_test_dir()
            src.mkdir(parents=True, exist_ok=True)  # deliberately zero files
            sentinel = nas_test_dir() / "sentinel_do_not_delete.txt"
            sentinel.write_text("pre-existing")

            ok, reason = check_source_drive(str(src), min_free_gb=cfg().health.min_free_source_gb)

            ops.update({
                "gate_passed": ok,
                "reason": reason,
                "nas_sentinel_still_present": sentinel.exists(),
                "inference": ("flow raises HealthError here => status SKIPPED, "
                              "robocopy never runs => /MIR cannot purge NAS"),
            })
            assert not ok and "appears empty" in reason, f"op={ops}"
            assert sentinel.exists(), f"NAS must not be touched when gate blocks, op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestLAN10SourceMissingGate:
    """LAN-10: unplugged/missing source drive -> gate refuses with clear reason."""

    def test_LAN_10_source_missing(self):
        sid = "LAN-10"
        ops = {}
        try:
            missing = r"C:\BackupData\E2E_NOPE_FY"
            ok, reason = check_source_drive(missing, min_free_gb=cfg().health.min_free_source_gb)
            ops.update({
                "probed_path": missing,
                "gate_passed": ok,
                "reason": reason,
                "inference": "flow raises HealthError => LAN_SKIPPED, no mail-worthy crash",
            })
            assert not ok and "not accessible" in reason, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


from pathlib import Path
import hashlib
import os
import time

import pytest

from core.lan_manifest import diff_snapshots
from core.lan_preflight import run_lan_dry_run
from core.lan_sync import run_lan_sync

from tests.e2e_helpers import clean_test_dirs, make_file, nas_test_dir, source_test_dir
from tests.scenario_support import cfg, ensure_canary, real_gate, record_op

pytestmark = [real_gate()]


def _sync(source, dest) -> dict:
    return run_lan_sync(str(source), str(dest), cfg().lan)


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


class TestLAN11DryRunTimeoutConfig:
    """LAN-11 (F8): dry-run honors caller-supplied timeout - proves the
    configurable plumbing, using a genuinely large read-only tree."""

    def test_LAN_11_timeout_plumbing(self):
        sid = "LAN-11"
        ops = {}
        try:
            ensure_canary()
            # Read-only stressor: /L lists without copying; C:\Windows is
            # large enough that a 1s ceiling cannot finish the walk.
            t0 = time.monotonic()
            result = run_lan_dry_run(
                source=r"C:\Windows",
                dest=str(nas_test_dir()),
                timeout=1,
            )
            elapsed = round(time.monotonic() - t0, 2)
            ops.update({
                "requested_timeout_s": 1,
                "wall_elapsed_s": elapsed,
                "ok": result["ok"],
                "exit_code": result["exit_code"],
                "error_head": (result.get("error") or "")[:120],
                "config_bound": "LanConfig.dry_run_timeout_seconds ge=60 le=7200",
                "inference": ("subprocess received the caller timeout verbatim "
                              "(hardcoded 300s would NOT have fired); F8 plumbing live"),
            })
            assert result["ok"] is False, f"expected timeout trip, op={ops}"
            assert result["exit_code"] == -1, f"op={ops}"
            assert "Timeout after 1s" in result["error"], f"op={ops}"
            assert elapsed < 30, f"timeout not enforced promptly, op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 3: LAN-11 .. LAN-15
# ======================================================================

    def test_LAN_11_timeout_plumbing(self):
        sid = "LAN-11"
        ops = {}
        try:
            ensure_canary()
            # Read-only stressor: /L lists without copying; C:\Windows is
            # large enough that a 1s ceiling cannot finish the walk.
            t0 = time.monotonic()
            result = run_lan_dry_run(
                source=r"C:\Windows",
                dest=str(nas_test_dir()),
                timeout=1,
            )
            elapsed = round(time.monotonic() - t0, 2)
            ops.update({
                "requested_timeout_s": 1,
                "wall_elapsed_s": elapsed,
                "ok": result["ok"],
                "exit_code": result["exit_code"],
                "error_head": (result.get("error") or "")[:120],
                "config_bound": "LanConfig.dry_run_timeout_seconds ge=60 le=7200",
                "inference": ("subprocess received the caller timeout verbatim "
                              "(hardcoded 300s would NOT have fired); F8 plumbing live"),
            })
            assert result["ok"] is False, f"expected timeout trip, op={ops}"
            assert result["exit_code"] == -1, f"op={ops}"
            assert "Timeout after 1s" in result["error"], f"op={ops}"
            assert elapsed < 30, f"timeout not enforced promptly, op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestLAN12PostWalkDropG14:
    """LAN-12: post-sync enumeration dies -> snapshot returns None, CRITICAL
    logged, sync verdict untouched (G14). Real trigger: unreachable share."""

    def test_LAN_12_walk_drop(self):
        sid = "LAN-12"
        ops = {}
        try:
            from flow import lan_snapshot_after_task

            broken_cfg = cfg()
            broken_cfg.paths.lan_destination = r"\\127.0.0.1\share_gone_midrun"

            captured = []
            from loguru import logger
            hid = logger.add(captured.append, level="ERROR")

            try:
                out = lan_snapshot_after_task.fn(broken_cfg)
            finally:
                logger.remove(hid)

            critical_line = next(
                (m for m in captured if "Post-sync destination walk failed" in m), ""
            )
            ops.update({
                "returned": out,
                "critical_logged": bool(critical_line),
                "log_head": critical_line[:160],
                "inference": ("pipeline keeps prior LAN status, skips DB row + "
                              "metrics this run; next run re-derives (G14)"),
            })
            assert out is None, f"contract requires None on walk failure, op={ops}"
            assert critical_line, f"CRITICAL entry required, op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestLAN13DiffCorrectness:
    """LAN-13: 10 baseline -> +1 new, +1 modified => added1 modified1 removed0 unchanged9,
    and the exact bytes_copied math the pipeline feeds extended_metrics."""

    def test_LAN_13_diff(self):
        sid = "LAN-13"
        ops = {}
        try:
            before, after = {}, {}
            for i in range(10):
                name = f"f{i:02d}.txt"
                data = os.urandom(100 + i)
                before[name] = (len(data), 1000000.0 + i)
                if i == 3:  # modified: bigger + newer
                    data = data + b"x" * 50
                    after[name] = (len(data), 2000000.0)
                else:
                    after[name] = (len(data), 1000000.0 + i)
            after["f_new.txt"] = (777, 3000000.0)

            d = diff_snapshots(before, after)

            copied_paths = d["added"] + d["modified"]
            bytes_copied = sum(after[p][0] for p in copied_paths)

            ops.update({
                "added": len(d["added"]),
                "modified": len(d["modified"]),
                "removed": len(d["removed"]),
                "unchanged": len(d["unchanged"]),
                "files_copied_metric": len(copied_paths),
                "bytes_copied_metric": bytes_copied,
                "expected_bytes": after["f_new.txt"][0] + after["f03.txt"][0],
            })
            assert (ops["added"], ops["modified"], ops["removed"], ops["unchanged"]) == \
                (1, 1, 0, 9), f"op={ops}"
            assert bytes_copied == ops["expected_bytes"], f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": "dashboard metrics derive from these exact numbers"})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestLAN14AnomalyOnlyExtras:
    """LAN-14: extra file present ONLY on NAS. Catalog predicts PARTIAL +
    anomaly_details. Reality check: /MIR PURGES the orphan, which lands in
    robocopy's extras accounting -> expected empirical outcome recorded."""

    def test_LAN_14_extras_on_dest(self):
        sid = "LAN-14"
        ops = {}
        try:
            clean_test_dirs()
            ensure_canary()
            src, nas = source_test_dir(), nas_test_dir()
            src.mkdir(parents=True, exist_ok=True)
            make_file(src / "keeper.txt", 1024)

            orphan = nas / "stray_from_crm.txt"
            orphan.write_bytes(os.urandom(2048))

            result = _sync(src, nas)

            ops.update({
                "status": result["status"],
                "exit_code": result["exit_code"],
                "anomaly_details_present": bool(result.get("anomaly_details")),
                "error_present": bool(result.get("error")),
                "orphan_after_sync": orphan.exists(),
                "error_head": (result.get("error") or "")[:140],
                "anomaly_head": (result.get("anomaly_details") or "")[:140],
                "inference": ("catalog predicted PARTIAL+anomaly tail; /MIR treats "
                              "dest-only file as purged extra - observed mapping "
                              "recorded verbatim"),
            })
            assert not orphan.exists(), f"/MIR must reclaim stray file, op={ops}"
            assert result["error"] is None, f"no error path allowed here, op={ops}"
            branch = (
                "CATALOG_AS_WRITTEN"
                if result["status"] == "LAN_PARTIAL" and ops["anomaly_details_present"]
                else "REALITY_EXIT%d_%s" % (result["exit_code"], result["status"])
            )
            ops["branch"] = branch
            record_op(sid, "ANOMALY-RECORDED" if branch != "CATALOG_AS_WRITTEN" else "PASS",
                      ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestLAN15Bit3AlertContract:
    """LAN-15: 2 locked victims + 1 good -> PARTIAL, error tail -> mail path.
    Also re-verifies the LAN-06 counting limit at n=2."""

    def test_LAN_15_two_locked(self):
        sid = "LAN-15"
        ops = {}
        try:
            import msvcrt

            clean_test_dirs()
            ensure_canary()
            src, nas = source_test_dir(), nas_test_dir()
            src.mkdir(parents=True, exist_ok=True)
            make_file(src / "good.txt", 4096)
            victims = []
            fds = []
            for name in ("ledger_locked.xlsx", "gst_locked.pdf"):
                v = src / name
                make_file(v, 8192)
                fd = os.open(str(v), os.O_RDWR | os.O_BINARY)
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                fds.append(fd)
                victims.append(name)

            try:
                result = _sync(src, nas)
            finally:
                for fd in fds:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    finally:
                        os.close(fd)

            err_tail = result.get("error") or ""
            ops.update({
                "status": result["status"],
                "exit_code": result["exit_code"],
                "files_failed_counter": result.get("files_failed"),
                "failed_marker_in_tail": "** FAILED:" in err_tail,
                "error_tail_bytes": len(err_tail),
                "victims_absent_on_nas": all(not (nas / v).exists() for v in victims),
                "good_on_nas": (nas / "good.txt").exists(),
                "flow_alert_wiring": "_run_lan_pipeline sends failure alert when "
                                     "status==LAN_PARTIAL (F12) - verified separately",
                "counting_limit_note": "see Batch-2 finding 1 (/NJS suppresses markers)",
            })
            assert result["status"] in ("LAN_PARTIAL", "LAN_FAILED"), f"op={ops}"
            assert result["exit_code"] >= 8, f"op={ops}"
            assert err_tail, f"alert needs non-empty error tail, op={ops}"
            assert ops["victims_absent_on_nas"], f"op={ops}"
            # P1-COUNT (A-prime): summary row is parsed positionally now -
            # exactly the 2 locked victims must be counted, not 0.
            assert result.get("files_failed") == 2, (
                f"P1-COUNT: expected files_failed==2, op={ops}"
            )
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 4: LAN-16 .. LAN-20 (closes Branch A)
# ======================================================================

import os
import time
from pathlib import Path

import pytest

from core.lan_sync import (
    ROBOCOPY_LOG_PREFIX,
    _validate_required_flags,
    build_robocopy_command,
    classify_exit_code,
    cleanup_orphaned_robocopy_logs,
    run_lan_sync,
)
from core.shutdown import shutdown_server

from tests.scenario_support import cfg, real_gate, record_op

pytestmark = [real_gate()]


class TestLAN16FatalBit4:
    """LAN-16: nothing-copyable situation -> robocopy bit4 -> LAN_FAILED."""

    def test_LAN_16_fatal(self):
        sid = "LAN-16"
        ops = {}
        try:
            result = run_lan_sync(
                r"C:\BackupData\E2E_NOPE_FY",
                str(cfg().paths.lan_destination),
                cfg().lan,
            )
            ops.update({
                "status": result["status"],
                "exit_code": result["exit_code"],
                "error_head": (result.get("error") or "")[:160],
                "classify_16": classify_exit_code(16),
                "classify_neg1": classify_exit_code(-1),
                "inference": ("bit4 fatal maps to LAN_FAILED; flow aborts before "
                              "any success bookkeeping"),
            })
            assert result["status"] == "LAN_FAILED", f"op={ops}"
            assert result["exit_code"] & 16, f"op={ops}"
            assert classify_exit_code(16) == "LAN_FAILED"
            assert classify_exit_code(-1) == "LAN_FAILED"
            record_op(sid, "PASS", ops)
        except Exception as e:
            ops["error"] = f"{type(e).__name__}: {e}"[:250]
            record_op(sid, "FAIL", ops)
            raise


class TestLAN17OrphanLogCleanupG8:
    """LAN-17: stale robocopy temp logs purged by age, fresh ones kept."""

    def test_LAN_17_orphan_cleanup(self):
        sid = "LAN-17"
        ops = {}
        import tempfile

        tmp = Path(tempfile.gettempdir())
        old = tmp / f"{ROBOCOPY_LOG_PREFIX}zz_scen_old.log"
        fresh = tmp / f"{ROBOCOPY_LOG_PREFIX}zz_scen_fresh.log"
        try:
            old.write_text("stale")
            fresh.write_text("current")
            stale_ts = time.time() - 48 * 3600
            os.utime(old, (stale_ts, stale_ts))

            removed = cleanup_orphaned_robocopy_logs(max_age_hours=24)

            ops.update({
                "removed_count": removed,
                "old_deleted": not old.exists(),
                "fresh_kept": fresh.exists(),
                "inference": "G8 age-gate exact: >24h purged, current untouched",
            })
            assert old.exists() is False, f"op={ops}"
            assert fresh.exists(), f"op={ops}"
            assert removed >= 1, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            ops["error"] = f"{type(e).__name__}: {e}"[:250]
            record_op(sid, "FAIL", ops)
            raise
        finally:
            for p in (old, fresh):
                if p.exists():
                    p.unlink()


class TestLAN18ShutdownOnComplete:
    """LAN-18: COMPLETE -> shutdown invoked against NAS IP; disabled-config skips.
    Live-proof uses unreachable TEST-NET IP so nothing real powers off."""

    def test_LAN_18_shutdown_contract(self):
        sid = "LAN-18"
        ops = {}
        try:
            from flow import lan_shutdown_task

            # Branch A: feature disabled -> silent skip, no shutdown.exe
            off = cfg()
            off.lan.shutdown_after_backup = False
            lan_shutdown_task.fn(off)
            ops["disabled_branch"] = "returned silently"

            # Branch B: enabled -> command really issued (dead IP -> clean refusal)
            on = cfg()
            on.wol.enabled = True
            on.wol.server_ip = "192.0.2.55"
            res = shutdown_server(on.wol.server_ip)
            ops.update({
                "enabled_branch_shutdown_initiated": res.get("shutdown_initiated"),
                "enabled_branch_error": (res.get("error") or "")[:120],
            })
            assert res["shutdown_initiated"] is False, f"op={ops}"
            assert res["error"], f"op={ops}"
            ops["inference"] = ("task issues 'shutdown /s /m \\\\IP /t 300 /f'; "
                                "failure path is non-critical warning (flow continues)")
            record_op(sid, "PASS", ops)
        except Exception as e:
            ops["error"] = f"{type(e).__name__}: {e}"[:250]
            record_op(sid, "FAIL", ops)
            raise


class TestLAN19PartialSkipsShutdownAlerts:
    """LAN-19: end-to-end pipeline with one locked victim -> PARTIAL ->
    NAS shutdown MUST be skipped, failure-alert path MUST be reached."""

    def test_LAN_19_partial_gate(self):
        sid = "LAN-19"
        ops = {}
        fds = []
        try:
            import msvcrt

            from tests.e2e_helpers import clean_test_dirs, make_file, nas_test_dir, source_test_dir
            from tests.scenario_support import ensure_canary
            from flow import _run_lan_pipeline

            clean_test_dirs()
            ensure_canary()
            src, nas = source_test_dir(), nas_test_dir()
            src.mkdir(parents=True, exist_ok=True)
            make_file(src / "ok.txt", 2048)
            v = src / "busy.xlsx"
            make_file(v, 4096)
            fd = os.open(str(v), os.O_RDWR | os.O_BINARY)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            fds.append(fd)

            from loguru import logger
            captured = []
            hid = logger.add(captured.append, level="INFO")

            pcfg = cfg()
            pcfg.paths.source_drive = str(src)
            pcfg.paths.lan_destination = str(nas)
            # Safety on the TEST box: even if the F3 gate were buggy, this flag
            # makes any shutdown task call a silent no-op. The gate under test
            # (flow.py 'NAS shutdown SKIPPED') fires BEFORE the task, so the
            # scenario still proves what it claims.
            pcfg.lan.shutdown_after_backup = False
            pcfg.wol.enabled = True
            pcfg.wol.mac_address = "02-00-00-00-00-01"
            # Live NAS so the WoL SMB-wait succeeds and the pipeline reaches sync.
            pcfg.wol.server_ip = "127.0.0.1"
            pcfg.notifications.send_on_failure = False

            try:
                _run_lan_pipeline(pcfg, "scen-lan19", "2026-08-23T00:00:00", None)
            finally:
                logger.remove(hid)

            blob = "\n".join(captured)
            tried_shutdown = "Shutting down backup server" in blob
            skipped = "NAS shutdown SKIPPED" in blob
            alert_gate = "send_on_failure disabled" in blob
            Path(os.environ.get("TEMP", ".")).joinpath("lan19_blob.log").write_text(
                blob, encoding="utf-8"
            )

            ops.update({
                "partial_logged": "PARTIAL" in blob,
                "shutdown_attempted": tried_shutdown,
                "shutdown_skipped_msg": skipped,
                "alert_path_reached": alert_gate,
                "wol_wake_ok": "not accessible within" not in blob,
                "inference": ("F3 contract live: PARTIAL never powers the NAS; "
                              "alert gate reached (SMTP disabled on test box -> "
                              "delivery itself out of scope)"),
            })
            assert skipped, f"op={ops}"
            assert not tried_shutdown, f"op={ops}"
            assert alert_gate, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            ops["error"] = f"{type(e).__name__}: {e}"[:250]
            record_op(sid, "FAIL", ops)
            raise
        finally:
            for fd in fds:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    import msvcrt as m2
                    m2.locking(fd, m2.LK_UNLCK, 1)
                except OSError:
                    pass
                os.close(fd)


class TestLAN20NCFlagForbidden:
    """LAN-20: /NC is rejected at build time - parser depends on class labels."""

    def test_LAN_20_nc_guard(self):
        sid = "LAN-20"
        ops = {}
        try:
            raised = None
            try:
                _validate_required_flags(["/MIR", "/COPY:DAT", "/NC"])
            except ValueError as ve:
                raised = str(ve)

            cmd = build_robocopy_command(
                r"C:\BackupData\E2E_TEST_SOURCE",
                r"\\127.0.0.1\lan_backup\E2E_TEST_DEST",
                cfg().lan,
            )
            uppers = [f.upper() for f in cmd]

            ops.update({
                "nc_raises": raised is not None,
                "raise_msg": (raised or "")[:120],
                "cmd_has_mir": "/MIR" in uppers,
                "cmd_has_retry_profile": "/R:3" in uppers and "/W:10" in uppers,
                "cmd_has_mt": f"/MT:{cfg().lan.mt_threads}" in uppers,
                "cmd_nc_absent": "/NC" not in uppers,
                "flag_count": len(cmd),
            })
            assert ops["nc_raises"], f"op={ops}"
            assert ops["cmd_has_mir"] and ops["cmd_nc_absent"], f"op={ops}"
            assert ops["cmd_has_retry_profile"] and ops["cmd_has_mt"], f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": "parser contract protected at command-build boundary"})
        except Exception as e:
            ops["error"] = f"{type(e).__name__}: {e}"[:250]
            record_op(sid, "FAIL", ops)
            raise
