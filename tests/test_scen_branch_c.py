"""Branch C scenarios (Health Gate, core/health.py) - catalog HL-xx.

Every test calls the program's own pre_backup_health() and asserts the exact
refusal reason the catalog pins. No program code is modified.
"""
import shutil
import time
from pathlib import Path

import pytest

from core.health import HealthError, pre_backup_health

from tests.e2e_helpers import source_test_dir
from tests.scenario_support import real_gate, record_op

pytestmark = [real_gate()]


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


class TestHL01SourceMissing:
    def test_HL_01_source_missing(self):
        sid = "HL-01"
        ops = {}
        try:
            raised = None
            try:
                pre_backup_health(r"C:\BackupData\E2E_NOPE_FY", "lan")
            except HealthError as he:
                raised = str(he)
            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:140],
                "catalog_fragment": "not accessible",
            })
            assert raised is not None, f"op={ops}"
            assert "not accessible" in raised, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestHL02SourceEmpty:
    def test_HL_02_source_empty(self):
        sid = "HL-02"
        ops = {}
        tmp = Path(__import__("tempfile").gettempdir()) / "scen_hl02_empty"
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            assert any(tmp.iterdir()) is False

            raised = None
            try:
                pre_backup_health(str(tmp), "lan")
            except HealthError as he:
                raised = str(he)
            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:140],
                "catalog_fragment": "appears empty",
            })
            assert raised is not None, f"op={ops}"
            assert "appears empty" in raised, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ======================================================================
# Batch 8: HL-03 .. HL-07
# ======================================================================

import inspect
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from core.health import HealthError, check_binary_exists, pre_backup_health
from core.process import resolve_binary

from tests.e2e_helpers import make_file, source_test_dir
from tests.scenario_support import real_gate, record_op

pytestmark = [real_gate()]


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


class TestHL03PermissionDenied:
    """HL-03: DENY ACL on the source -> iterdir raises PermissionError ->
    gate refuses with the exact 'permission denied' reason."""

    def test_HL_03_permission_denied(self):
        sid = "HL-03"
        ops = {}
        tmp = Path(tempfile.gettempdir()) / "scen_hl03_denied"
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            make_file(tmp / "locked_away.txt", 32)

            user = os.environ.get("USERNAME", "Administrator")
            subprocess.run(
                ["icacls", str(tmp), "/deny", f"{user}:(OI)(CI)(F)"],
                check=True, capture_output=True,
            )

            raised = None
            try:
                pre_backup_health(str(tmp), "lan")
            except HealthError as he:
                raised = str(he)

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:150],
                "catalog_fragment": "permission denied",
            })
            assert raised is not None, f"op={ops}"
            assert "permission denied" in raised.lower(), f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            ops["note"] = ("if admin token bypassed the deny ACE this lands "
                           "ENV-LIMITED - recorded verbatim either way")
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            subprocess.run(["icacls", str(tmp), "/reset"],
                           capture_output=True)
            shutil.rmtree(tmp, ignore_errors=True)


class TestHL04LowDiskParameterDriven:
    """HL-04: minimum-free threshold is a REAL operator parameter - drive it
    past actual free space and the gate must refuse with exact numbers."""

    def test_HL_04_low_disk(self):
        sid = "HL-04"
        ops = {}
        try:
            src = source_test_dir()
            make_file(src / "hl04_probe.txt", 16)

            huge_min = 10 * 1024 * 1024  # 10 TiB minimum: any real box fails
            import shutil as sh
            actual_free_gb = sh.disk_usage(str(src)).free / (1024 ** 3)

            raised = None
            try:
                pre_backup_health(str(src), "lan",
                                  min_free_source_gb=huge_min)
            except HealthError as he:
                raised = str(he)

            ops.update({
                "min_free_gb_param": huge_min,
                "actual_free_gb_rounded": round(actual_free_gb, 1),
                "raised": raised is not None,
                "reason": (raised or "")[:170],
            })
            assert raised is not None, f"op={ops}"
            assert "critically low on space" in raised, f"op={ops}"
            assert f"(minimum: {huge_min} GB)" in raised, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("threshold flows from "
                                                  "config-driven parameter to "
                                                  "reason string verbatim")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestHL05RcloneMissing:
    """HL-05: rclone unresolvable (local bin renamed + PATH scrubbed)."""

    def test_HL_05_rclone_missing(self):
        sid = "HL-05"
        ops = {}
        hidden = None
        orig_path = None
        try:
            exe = Path(resolve_binary("rclone"))
            hidden = exe.with_name(exe.name + ".hidden_for_hl05")
            orig_path = os.environ.get("PATH", "")
            os.rename(exe, hidden)
            os.environ["PATH"] = os.environ.get("TEMP", ".")

            assert resolve_binary("rclone") is None
            assert check_binary_exists("rclone") is False

            raised = None
            try:
                pre_backup_health(str(source_test_dir()), "cloud",
                                  gcs_key_path=r"C:\nonexistent\key.json")
            except HealthError as he:
                raised = str(he)

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:120],
                "catalog_fragment": "rclone not found",
            })
            assert raised and "rclone not found" in raised, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            if hidden is not None and hidden.exists():
                os.rename(hidden, hidden.with_suffix("").with_suffix(".exe"))
            if orig_path is not None:
                os.environ["PATH"] = orig_path


class TestHL06RobocopyMissing:
    """HL-06: robocopy lives only in System32 here - scrubbing PATH makes it
    unresolvable with zero filesystem changes; fully in-process reversal."""

    def test_HL_06_robocopy_missing(self):
        sid = "HL-06"
        ops = {}
        orig_path = None
        try:
            orig_path = os.environ.get("PATH", "")
            os.environ["PATH"] = os.environ.get("TEMP", ".")

            from core.process import resolve_binary as rb
            assert rb("robocopy") is None
            assert check_binary_exists("robocopy") is False

            raised = None
            try:
                pre_backup_health(str(source_test_dir()), "lan")
            except HealthError as he:
                raised = str(he)

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:120],
                "catalog_fragment": "robocopy not found",
            })
            assert raised and "robocopy not found" in raised, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            if orig_path is not None:
                os.environ["PATH"] = orig_path


class TestHL07GCSKeyMissingAndEmpty:
    """HL-07: both key failure shapes, driven through the same
    gcs_key_path parameter flow.py hands over from config.paths."""

    def test_HL_07_gcs_key(self):
        sid = "HL-07"
        ops = {}
        try:
            missing_err = None
            try:
                pre_backup_health(str(source_test_dir()), "cloud",
                                  gcs_key_path=r"C:\AAM_BACKUP_V1\deploy\keys\absent_hl07.json")
            except HealthError as he:
                missing_err = str(he)

            empty_key = Path(tempfile.gettempdir()) / "scen_hl07_empty.json"
            empty_key.write_bytes(b"")
            empty_err = None
            try:
                pre_backup_health(str(source_test_dir()), "cloud",
                                  gcs_key_path=str(empty_key))
            except HealthError as he:
                empty_err = str(he)

            ops.update({
                "missing_reason": (missing_err or "")[:130],
                "empty_reason": (empty_err or "")[:130],
            })
            assert missing_err and "not found" in missing_err, f"op={ops}"
            assert empty_err and "is empty" in empty_err, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("clock-skew network probe never "
                                                  "ran - key check precedes it")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 9: HL-08 .. HL-11 (closes Branch C)
# ======================================================================

class TestHL08ClockSkewThreshold:
    """HL-08: real Date header from www.googleapis.com; threshold driven via
    the REAL max_skew_seconds parameter below measured skew."""

    def test_HL_08_clock_skew(self):
        sid = "HL-08"
        ops = {}
        try:
            from core.health import check_clock_skew

            # oracle-side measurement using the same public endpoint
            import http.client

            conn = http.client.HTTPSConnection("www.googleapis.com", timeout=10)
            conn.request("HEAD", "/")
            resp = conn.getresponse()
            google_date_str = resp.getheader("Date")
            conn.close()
            assert google_date_str, "no Date header - cannot run scenario"

            import pendulum
            from email.utils import parsedate_to_datetime

            google_time = parsedate_to_datetime(google_date_str)
            actual = abs((pendulum.now("UTC") - google_time).total_seconds())

            tight_limit = max(0, int(actual) - 1)
            ok, reason = check_clock_skew(max_skew_seconds=tight_limit,
                                          connection_timeout=10)

            ops.update({
                "measured_skew_s": round(actual, 1),
                "threshold_s": tight_limit,
                "ok": ok,
                "reason": (reason or "")[:170],
                "catalog_fragment": "w32tm /resync",
            })
            assert ok is False, f"op={ops}"
            assert "System clock skew detected" in reason, f"op={ops}"
            assert "w32tm /resync" in reason, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("skew math + operator remedy "
                                                  "string verified against the "
                                                  "live Google endpoint")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestHL09ClockUnreachableNonBlocking:
    """HL-09: network failure during skew probe must NOT block backup -
    returns (True, '') with only a warning. Trigger: zero-length timeout."""

    def test_HL_09_unreachable(self):
        sid = "HL-09"
        ops = {}
        try:
            from core.health import check_clock_skew

            captured = []
            from loguru import logger
            hid = logger.add(captured.append, level="WARNING")
            try:
                ok, reason = check_clock_skew(max_skew_seconds=600,
                                              connection_timeout=0)
            finally:
                logger.remove(hid)

            warned = any("Could not verify clock skew" in m for m in captured)
            ops.update({
                "ok": ok,
                "reason": reason,
                "warned_only": warned,
                "inference": ("unreachability degrades to warning - backup "
                              "proceeds (availability over strictness here)"),
            })
            assert ok is True and reason == "", f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestHL10InvalidMode:
    def test_HL_10_invalid_mode(self):
        sid = "HL-10"
        ops = {}
        try:
            raised = None
            try:
                pre_backup_health(str(source_test_dir()), "banana")
            except HealthError as he:
                raised = str(he)
            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:140],
                "catalog_fragment": "Invalid mode",
            })
            assert raised and "Invalid mode 'banana'" in raised, f"op={ops}"
            assert "cloud, lan" in raised or "'all'" in raised, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestHL11BadDateHeaderWiring:
    """HL-11: malformed Date -> ValueError path must skip non-blocking.
    The endpoint is hardcoded to Google, so a malformed header cannot be
    produced against production without interception - recorded as
    WIRING-EVIDENCED rather than faked with mocks."""

    def test_HL_11_bad_date(self):
        sid = "HL-11"
        ops = {}
        try:
            from core import health as health_mod
            src = inspect.getsource(health_mod.check_clock_skew)
            has_valueerror_guard = (
                "except ValueError" in src
                and "Could not parse Google Date header" in src
                and 'return True, ""' in src
            )
            ops.update({
                "valueerror_guard_present": has_valueerror_guard,
                "verdict_basis": ("core/health.py check_clock_skew except "
                                  "ValueError branch read-verified"),
            })
            assert has_valueerror_guard, f"op={ops}"
            record_op(sid, "WIRING-EVIDENCED", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise

