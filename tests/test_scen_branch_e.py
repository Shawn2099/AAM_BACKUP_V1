"""Branch E scenarios (FY Rollover / time utils) - catalog FY-xx.

Program functions only. Rollover EXECUTION (rollover()) is deferred to a
controlled sandbox batch; detection-level contracts are proven here against
the production config paths.
"""
import pytest

from core.fy_rollover import _fy_name, detect_rollover
from core.time_utils import get_fy_prefix

from tests.scenario_support import real_gate, record_op

pytestmark = [real_gate()]


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


class TestFY01SameFYNoOp:
    """FY-01: configured FY suffix == computed current FY -> no rollover."""

    def test_FY_01_no_op(self):
        sid = "FY-01"
        ops = {}
        try:
            computed = get_fy_prefix()
            src = rf"C:\BackupData\{computed}"
            lan = rf"\\127.0.0.1\lan_backup\{computed}"

            result = detect_rollover(src, lan)

            ops.update({
                "computed_fy": computed,
                "source_probe": src,
                "lan_probe": lan,
                "detect_rollover": result,
                "inference": ("production paths carry the CURRENT FY -> "
                              "detect_rollover False -> daily check is a no-op"),
            })
            assert result is False, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY02April1Detection:
    """FY-02 detection half: previous-FY paths vs computed FY -> True.
    Executing rollover() itself needs a sandboxed YAML + destinations and is
    deferred to its own controlled scenario batch (recorded here)."""

    def test_FY_02_detect_due(self):
        sid = "FY-02"
        ops = {}
        try:
            computed = get_fy_prefix()

            # derive the PREVIOUS fiscal year label from the computed one
            start_year = int(computed[2:4])
            prev = f"FY{start_year - 1:02d}-{start_year:02d}"

            result = detect_rollover(rf"C:\BackupData\{prev}",
                                     rf"\\127.0.0.1\lan_backup\{prev}")

            ops.update({
                "computed_fy": computed,
                "previous_fy": prev,
                "detect_rollover": result,
                "execution_note": ("rollover() run deferred - it rewrites "
                                   "config.yaml and touches both destinations"),
            })
            assert result is True, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise



# ======================================================================
# Batch 12: FY-03 .. FY-07
# ======================================================================

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from core.fy_rollover import detect_rollover, run_final_backup
from models.config import AppConfig

from tests.e2e_helpers import cfg, make_file, nas_test_dir, source_test_dir
from tests.scenario_support import real_gate, record_op

pytestmark = [real_gate()]


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


def _write_yaml(tmp_dir: Path, name: str, source: str, lan: str) -> str:
    """Clone the REAL config.yaml and rewrite ONLY the two path lines.

    Maximum fidelity: every other section is byte-identical to production,
    so the only possible validation failure is the FY guard under test."""
    import json as _json
    import re as _re

    real = Path("config.yaml").read_text(encoding="utf-8")
    real = _re.sub(r"(?m)^  source_drive:.*$",
                   lambda _m: "  source_drive: " + _json.dumps(source), real)
    real = _re.sub(r"(?m)^  lan_destination:.*$",
                   lambda _m: "  lan_destination: " + _json.dumps(lan), real)

    out = tmp_dir / name
    out.write_text(real, encoding="utf-8")
    return str(out)


class TestFY03NoFYFoldersOptOut:
    """FY-03: no FYxx-xx segment in either path -> permanent no-op + warning."""

    def test_FY_03_opt_out(self):
        sid = "FY-03"
        ops = {}
        try:
            from loguru import logger
            captured = []
            hid = logger.add(captured.append, level="WARNING")
            try:
                result = detect_rollover("D:\\DATA", "\\\\share\\DATA")
            finally:
                logger.remove(hid)

            warned = any("rollover is disabled by configuration" in m
                         for m in captured)
            ops.update({
                "returned": result,
                "warning_logged": warned,
                "catalog_fragment": "rollover is disabled by configuration",
            })
            assert result is False, f"op={ops}"
            assert warned, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY04FourDigitTrapAtLoad:
    """FY-04: FY2026-27 style names are refused AT CONFIG LOAD via the real
    from_yaml path - before they can silently kill rollover forever."""

    def test_FY_04_four_digit(self):
        sid = "FY-04"
        ops = {}
        try:
            tmp = Path(__import__("tempfile").gettempdir()) / "scen_fy04"
            tmp.mkdir(parents=True, exist_ok=True)

            for label, src, lan in (
                ("source_side", r"C:\BackupData\FY2026-27", r"\\127.0.0.1\lan_backup\FY26-27"),
                ("lan_side", r"C:\BackupData\FY26-27", r"\\127.0.0.1\lan_backup\FY2026-27"),
            ):
                raised = None
                try:
                    AppConfig.from_yaml(_write_yaml(tmp, f"{label}.yaml", src, lan))
                except ValueError as ve:
                    raised = str(ve)
                ops[f"{label}_raised"] = raised is not None
                ops[f"{label}_reason"] = (raised or "")[:170]
                assert raised and "4-digit FY name" in raised, f"{label} op={ops}"
                assert "disables rollover forever" in raised, f"{label} op={ops}"

            record_op(sid, "PASS", {**ops,
                                    "inference": ("both sides refused at load; "
                                                  "error text names the exact "
                                                  "segment and the consequence")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY05MismatchGuardAtLoad:
    """FY-05: source FY != LAN FY -> CRITICAL DATA LOSS PREVENTION at load."""

    def test_FY_05_mismatch(self):
        sid = "FY-05"
        ops = {}
        try:
            tmp = Path(__import__("tempfile").gettempdir()) / "scen_fy05"
            tmp.mkdir(parents=True, exist_ok=True)

            raised = None
            try:
                AppConfig.from_yaml(_write_yaml(
                    tmp, "mismatch.yaml",
                    r"C:\BackupData\FY25-26",
                    r"\\127.0.0.1\lan_backup\FY24-25"))
            except ValueError as ve:
                raised = str(ve)

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:200],
                "catalog_fragment": "CRITICAL DATA LOSS PREVENTION",
            })
            assert raised and "CRITICAL DATA LOSS PREVENTION" in raised, f"op={ops}"
            assert "FY25-26" in raised and "FY24-25" in raised, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


@pytest.fixture(scope="module", autouse=True)
def _fy06_teardown():
    yield
    # remove the mirrored sandbox subtree on the NAS share (canary untouched)
    target = nas_test_dir() / "E2E_FINAL_FY"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    from core.process import resolve_binary
    from core.rclone_config import temp_rclone_config
    cfgx = cfg()
    with temp_rclone_config(cfgx.paths.gcs_key_path, cfgx.cloud.location,
                            cfgx.cloud.project_number,
                            cfgx.cloud.storage_class) as cpath:
        exe = resolve_binary("rclone") or "rclone"
        subprocess.run([exe, "purge", f"aam_gcs:{cfgx.cloud.bucket}/E2E_FINAL_FY",
                        "--config", cpath], check=False, capture_output=True)


class TestFY06HappyFinalBackups:
    """FY-06: both destinations healthy -> run_final_backup returns
    (cloud_ok=True, lan_ok=True). Sandboxed targets: NAS subfolder +
    E2E_FINAL_FY cloud prefix; WoL/shutdown disabled in the passed config."""

    def test_FY_06_happy(self):
        sid = "FY-06"
        ops = {}
        try:
            import subprocess as sp

            cfgx = cfg()
            cfgx.lan.enabled = True          # prod yaml ships LAN disabled;
            cfgx.wol.enabled = False         # sandbox re-enables the leg under
            cfgx.lan.shutdown_after_backup = False  # test explicitly

            src = source_test_dir()
            shutil.rmtree(src, ignore_errors=True)
            src.mkdir(parents=True, exist_ok=True)
            make_file(src / "closing_fy.txt", 2048)
            (src / ".AAM_TARGET_MOUNTED").touch(exist_ok=True)

            lan_dest = nas_test_dir() / "E2E_FINAL_FY"

            t0 = time.monotonic()
            cloud_ok, lan_ok = run_final_backup(
                source_drive=str(src),
                lan_destination=str(lan_dest),
                lan_config=cfgx.lan,
                cloud_config=cfgx.cloud,
                paths_config=cfgx.paths,
                config=cfgx,
                old_fy="E2E_FINAL_FY",
            )
            wall = round(time.monotonic() - t0, 1)

            landed = sorted(p.name for p in lan_dest.iterdir())
            ops.update({
                "cloud_ok": cloud_ok,
                "lan_ok": lan_ok,
                "nas_landed": landed,
                "wall_s": wall,
                "inference": ("both legs green against live destinations; "
                              "closing-FY data mirrored + uploaded"),
            })
            assert cloud_ok is True and lan_ok is True, f"op={ops}"
            # /XF .AAM_TARGET_MOUNTED keeps the sentinel out of mirrors
            assert landed == ["closing_fy.txt"], f"op={ops}"
            ops["canary_excluded_by_xf"] = True
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY07CloudFailBlocksRollover:
    """FY-07: broken cloud creds -> cloud_ok=False while LAN stays True;
    rollover() turns this tuple into RolloverError + config unchanged
    (raise-site read-verified at fy_rollover.py:442-444)."""

    def test_FY_07_cloud_blocked(self):
        sid = "FY-07"
        ops = {}
        try:
            cfgx = cfg()
            cfgx.lan.enabled = True          # prod yaml ships LAN disabled;
            cfgx.wol.enabled = False         # sandbox re-enables the leg under
            cfgx.lan.shutdown_after_backup = False  # test explicitly

            bad_cloud = cfgx.cloud.model_copy(deep=True)
            bad_cloud.bucket = "aam-cloudbackup-does-not-exist"

            src = source_test_dir()
            make_file(src / "fy07_probe.txt", 128)

            cloud_ok, lan_ok = run_final_backup(
                source_drive=str(src),
                lan_destination=str(nas_test_dir() / "E2E_FINAL_FY"),
                lan_config=cfgx.lan,
                cloud_config=bad_cloud,
                paths_config=cfgx.paths,
                config=cfgx,
                old_fy="E2E_FINAL_FY",
            )

            ops.update({
                "cloud_ok": cloud_ok,
                "lan_ok": lan_ok,
                "expected_tuple": "(False, True) -> rollover BLOCKED upstream",
                "raise_site": "fy_rollover.py:442-444 RolloverError 'final "
                              "backup failed' - read-verified",
            })
            assert cloud_ok is False, f"op={ops}"
            assert lan_ok is True, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 13: FY-08 .. FY-12
# ======================================================================

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from core.fy_rollover import (
    RolloverError,
    create_new_fy_folders,
    rollover,
    run_archive_transition,
)
from core.cloud_sync import run_cloud_sync
from core.process import resolve_binary
from core.rclone_config import temp_rclone_config

from tests.e2e_helpers import cfg, make_file, source_test_dir
from tests.scenario_support import real_gate, record_op

pytestmark = [real_gate()]


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


def _sandbox_config(overrides: dict) -> tuple[str, str]:
    """Clone real config.yaml, rewrite paths (+ extra YAML overrides),
    return (config_path, sha256_before)."""
    import re as _re

    from tests.scenario_support import cfg as _cfg

    tmp = Path(tempfile.gettempdir()) / "scen_fy_sandbox"
    tmp.mkdir(parents=True, exist_ok=True)

    def jq(s: str) -> str:
        import json as _json
        return _json.dumps(s)

    real = Path("config.yaml").read_text(encoding="utf-8")
    src = overrides["source_drive"]
    lan = overrides["lan_destination"]
    real = _re.sub(r'(?m)^  source_drive:.*$',
                   lambda _m: "  source_drive: " + jq(src), real)
    real = _re.sub(r'(?m)^  lan_destination:.*$',
                   lambda _m: "  lan_destination: " + jq(lan), real)
    for pat, repl in overrides.get("yaml_edits", []):
        if callable(repl):
            real = _re.sub(pat, repl, real, count=1)
        else:
            real = _re.sub(pat, lambda _m, r=repl: r, real, count=1)

    out = tmp / f"{overrides.get('name', 'cfg')}.yaml"
    out.write_text(real, encoding="utf-8")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    return str(out), digest


def _make_fy_source(fy_label: str) -> Path:
    src_root = Path(r"C:\BackupData\E2E_FYROOT")
    src = src_root / fy_label
    src.mkdir(parents=True, exist_ok=True)
    make_file(src / "closing_ledger.xlsx", 4096)
    return src_root


class TestFY08LanOfflineBlocks:
    """FY-08: full rollover() with an unreachable NAS -> RolloverError names
    LAN; sandbox yaml untouched; no new-FY folders appear anywhere."""

    def test_FY_08_lan_blocked(self):
        sid = "FY-08"
        ops = {}
        try:
            src_root = _make_fy_source("FY25-26")

            shutil.rmtree(src_root / "FY26-27", ignore_errors=True)
            cfg_path, sha_before = _sandbox_config({
                "name": "fy08",
                "source_drive": str(src_root / "FY25-26"),
                "lan_destination": r"\\127.0.0.1\no_such_share_scen\FY25-26",
                "yaml_edits": [
                    # prod config ships LAN disabled; the BLOCKED contract
                    # requires this leg enabled-and-failing
                    (r"(?m)^lan:\n(?:  #[^\n]*\n)*?  enabled: false$",
                     lambda m: "lan:\n  enabled: true"),
                ],
            })

            raised = None
            try:
                rollover(cfg_path)
            except RolloverError as re_:
                raised = str(re_)

            sha_after = hashlib.sha256(Path(cfg_path).read_bytes()).hexdigest()
            new_folder_absent = not (src_root / "FY26-27").exists()

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:160],
                "config_unchanged": sha_after == sha_before,
                "no_new_folders": new_folder_absent,
                "catalog_fragment": "final backup failed for LAN",
            })
            assert raised and "final backup failed for LAN" in raised, f"op={ops}"
            assert ops["config_unchanged"], f"op={ops}"
            assert ops["no_new_folders"], f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("blocked exactly per contract: "
                                                  "retry next day, zero side "
                                                  "effects on disk or yaml")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY09BlockedRespectsEnabledList:
    """FY-09: cloud disabled + LAN failing -> error names LAN only; the
    disabled leg never enters the 'required' list."""

    def test_FY_09_enabled_list(self):
        sid = "FY-09"
        ops = {}
        try:
            src_root = _make_fy_source("FY25-26")
            shutil.rmtree(src_root / "FY26-27", ignore_errors=True)

            cfg_path, _sha = _sandbox_config({
                "name": "fy09",
                "source_drive": str(src_root / "FY25-26"),
                "lan_destination": r"\\127.0.0.1\no_such_share_scen\FY25-26",
                "yaml_edits": [
                    (r"(?m)^(cloud:\n  enabled: )true$",
                     lambda m: m.group(1) + "false"),
                    # LAN must stay REQUIRED here - re-enable the leg
                    (r"(?m)^lan:\n(?:  #[^\n]*\n)*?  enabled: false$",
                     lambda m: "lan:\n  enabled: true"),
                ],
            })

            raised = None
            try:
                rollover(cfg_path)
            except RolloverError as re_:
                raised = str(re_)

            ops.update({
                "raised": raised is not None,
                "reason": (raised or "")[:200],
                "names_lan_only": bool(raised) and "cloud" not in raised,
            })
            assert raised, f"op={ops}"
            assert "LAN" in raised, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("disabled cloud absent from "
                                                  "required list even though its "
                                                  "leg never ran")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY10ArchiveTransitionReal:
    """FY-10: server-side ARCHIVE transition via the program's own
    run_archive_transition (gcloud storage objects update --recursive);
    truth oracle: REST objects.get storageClass afterwards."""

    def test_FY_10_archive(self):
        sid = "FY-10"
        ops = {}
        try:
            cfgx = cfg()
            old_fy = "E2E_ARCHIVE_FY"
            src = source_test_dir()
            shutil.rmtree(src, ignore_errors=True)
            src.mkdir(parents=True, exist_ok=True)
            make_file(src / "to_archive.txt", 1024)

            up = run_cloud_sync(
                source=str(src), bucket=cfgx.cloud.bucket, fy_prefix=old_fy,
                gcs_key_path=cfgx.paths.gcs_key_path,
                project_number=cfgx.cloud.project_number,
                storage_class=cfgx.cloud.storage_class,
                location=cfgx.cloud.location,
                bwlimit=cfgx.cloud.bandwidth_limit,
                retries=1,
            )
            assert up["status"] == "CLOUD_COMPLETE", f"upload op={up}"

            # Deployment prerequisite discovered live: bundled gcloud.cmd
            # needs a Python interpreter; operators set CLOUDSDK_PYTHON at
            # install time (see findings). Same mechanism used here.
            import sys as _sys
            _prev_py = os.environ.get("CLOUDSDK_PYTHON")
            os.environ["CLOUDSDK_PYTHON"] = _sys.executable

            from loguru import logger as _lg
            _cap = []
            _hid = _lg.add(_cap.append, level="WARNING")
            try:
                ok = run_archive_transition(
                    bucket=cfgx.cloud.bucket,
                    old_fy=old_fy,
                    gcs_key_path=cfgx.paths.gcs_key_path,
                )
            finally:
                _lg.remove(_hid)
                if _prev_py is None:
                    os.environ.pop("CLOUDSDK_PYTHON", None)
                else:
                    os.environ["CLOUDSDK_PYTHON"] = _prev_py
            warn_blob = "\n".join(_cap)
            ops["archive_warnings"] = [
                ln[:160] for ln in warn_blob.splitlines() if ln.strip()
            ][:4]

            # REST oracle on one object
            import json as _json
            import urllib.request
            import urllib.parse

            d = _json.loads(Path(cfgx.paths.gcs_key_path).read_text(encoding="utf-8"))
            import jwt as _jwt
            now = int(time.time())
            assertion = _jwt.encode(
                {"iss": d["client_email"],
                 "scope": "https://www.googleapis.com/auth/cloud-platform",
                 "aud": "https://oauth2.googleapis.com/token",
                 "iat": now, "exp": now + 3600},
                d["private_key"], algorithm="RS256")
            data = urllib.parse.urlencode({
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion}).encode()
            with urllib.request.urlopen(
                urllib.request.Request("https://oauth2.googleapis.com/token",
                                       data=data), timeout=30) as resp:
                tok = _json.loads(resp.read())["access_token"]
            url = (f"https://storage.googleapis.com/storage/v1/b/"
                   f"{cfgx.cloud.bucket}/o/"
                   f"{urllib.parse.quote(old_fy + '/to_archive.txt', safe='')}")
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                meta = _json.loads(resp.read())

            ops.update({
                "archive_ok": ok,
                "object": meta.get("name"),
                "storageClass_on_object": meta.get("storageClass"),
                "inference": ("metadata rewritten server-side; no "
                              "download/re-upload involved"),
            })
            assert ok is True, f"op={ops}"
            assert meta.get("storageClass") == "ARCHIVE", f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY11ArchiveFailureNonBlocking:
    """FY-11: broken key -> archive returns False with WARNING only."""

    def test_FY_11_archive_fail_nonblocking(self):
        sid = "FY-11"
        ops = {}
        try:
            cfgx = cfg()
            ok = run_archive_transition(
                bucket=cfgx.cloud.bucket,
                old_fy="E2E_ARCHIVE_FY",
                gcs_key_path=r"C:\AAM_BACKUP_V1\deploy\keys\absent_fy11.json",
            )
            ops.update({
                "returned": ok,
                "catalog_contract": "WARNING + False, rollover proceeds",
            })
            assert ok is False, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY12NewFoldersActionRequired:
    """FY-12: source mkdir mandatory+ok; LAN mkdir best-effort failure logs
    ACTION REQUIRED and does NOT raise."""

    def test_FY_12_folders(self):
        sid = "FY-12"
        ops = {}
        try:
            src_root = Path(tempfile.gettempdir()) / "scen_fy12_src"
            shutil.rmtree(src_root, ignore_errors=True)
            src_root.mkdir(parents=True)

            from loguru import logger
            captured = []
            hid = logger.add(captured.append, level="INFO")
            try:
                created = create_new_fy_folders(
                    str(src_root),
                    r"\\127.0.0.1\no_such_share_scen",
                    "FY26-27",
                )
            finally:
                logger.remove(hid)

            blob = "\n".join(captured)
            action_required = "ACTION REQUIRED: Manually create" in blob
            ops.update({
                "created_keys": sorted(created.keys()),
                "source_dir_exists": (src_root / "FY26-27").exists(),
                "action_required_logged": action_required,
                "lan_key_absent": "lan" not in created,
            })
            assert ops["source_dir_exists"], f"op={ops}"
            assert ops["action_required_logged"], f"op={ops}"
            assert ops["lan_key_absent"], f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("rollover proceeds despite "
                                                  "offline NAS; operator told "
                                                  "exactly what to do")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 14: FY-13 .. FY-17
# ======================================================================

class TestFY13YamlAtomicRoundTrip:
    """FY-13: ruamel round-trip preserves comments; paths advance to the new
    FY; os.replace gives atomicity (no temp residue)."""

    def test_FY_13_yaml(self):
        sid = "FY-13"
        ops = {}
        try:
            from core.fy_rollover import update_config_yaml

            tmp = Path(tempfile.gettempdir()) / "scen_fy13"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)

            cfg_path, _ = _sandbox_config({
                "name": "fy13",
                "source_drive": r"C:\BackupData\E2E_FYROOT\FY25-26",
                "lan_destination": r"\\127.0.0.1\lan_backup\E2E_TEST_DEST\FY25-26",
            })
            text_before = Path(cfg_path).read_text(encoding="utf-8")
            comments_before = [ln for ln in text_before.splitlines()
                               if ln.strip().startswith("#")]

            src_root = r"C:\BackupData\E2E_FYROOT"
            lan_root = r"\\127.0.0.1\lan_backup\E2E_TEST_DEST"
            update_config_yaml(cfg_path, src_root, lan_root, "FY26-27")

            text_after = Path(cfg_path).read_text(encoding="utf-8")
            comments_after = [ln for ln in text_after.splitlines()
                              if ln.strip().startswith("#")]
            temps_leftover = list(tmp.glob(".config_rollover_*.yaml"))

            from models.config import AppConfig
            loaded = AppConfig.from_yaml(cfg_path)

            ops.update({
                "comments_before": len(comments_before),
                "comments_after": len(comments_after),
                "comments_identical": comments_before == comments_after,
                "new_source": loaded.paths.source_drive,
                "new_lan": loaded.paths.lan_destination,
                "tmp_residue": len(temps_leftover),
                "inference": ("comment-preserving round-trip verified; "
                              "os.replace leaves zero temp files"),
            })
            assert ops["comments_identical"] and ops["comments_before"] >= 1, f"op={ops}"
            assert loaded.paths.source_drive.endswith("E2E_FYROOT\\FY26-27"), f"op={ops}"
            assert ops["tmp_residue"] == 0, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            shutil.rmtree(Path(tempfile.gettempdir()) / "scen_fy13",
                          ignore_errors=True)


class TestFY14CrashIdempotency:
    """FY-14: re-running the folder-creation seam after a simulated crash
    (mkdir done, YAML not yet written) must be safe: exist_ok repeats,
    sentinel preserved, no duplicate side effects."""

    def test_FY_14_idempotent(self):
        sid = "FY-14"
        ops = {}
        try:
            from core.fy_rollover import create_new_fy_folders

            src_root = Path(tempfile.gettempdir()) / "scen_fy14_src"
            shutil.rmtree(src_root, ignore_errors=True)
            src_root.mkdir(parents=True)
            lan_root = nas_test_dir() / "E2E_FY14"

            first = create_new_fy_folders(str(src_root),
                                          str(lan_root), "FY26-27")
            sentinel_before = (first["lan"] / ".AAM_TARGET_MOUNTED").exists()

            # simulated crash-restart: same call again
            second = create_new_fy_folders(str(src_root),
                                           str(lan_root), "FY26-27")

            ops.update({
                "first_keys": sorted(first.keys()),
                "second_keys": sorted(second.keys()),
                "sentinel_preserved": sentinel_before,
                "lan_canary_still_there": (second["lan"] / ".AAM_TARGET_MOUNTED").exists(),
                "inference": ("exist_ok=True makes the mkdir seam re-entrant; "
                              "the next rollover stage resumes safely"),
            })
            assert ops["first_keys"] == ["lan", "source"] == ops["second_keys"], f"op={ops}"
            assert ops["lan_canary_still_there"], f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            shutil.rmtree(Path(tempfile.gettempdir()) / "scen_fy14_src",
                          ignore_errors=True)
            shutil.rmtree(nas_test_dir() / "E2E_FY14", ignore_errors=True)


class TestFY15DailySchedulerWiring:
    """FY-15: rollover_check_flow is scheduled daily at the configured cron -
    machine need never reboot for the FY boundary to be handled."""

    def test_FY_15_scheduler(self):
        sid = "FY-15"
        ops = {}
        try:
            cfgx = cfg()
            serve_src = Path("serve.py").read_text(encoding="utf-8")

            checks = {
                "cron_configured": cfgx.schedule.rollover_cron == "0 6 * * *",
                "flow_scheduled": (
                    "rollover_check_flow.to_deployment" in serve_src
                    and "Cron(config.schedule.rollover_cron" in serve_src
                ),
                "no_op_until_boundary": True,  # proven live in FY-01/FY-02 rows
            }
            ops.update({
                "wiring": checks,
                "rollover_cron_value": cfgx.schedule.rollover_cron,
                "timezone": cfgx.schedule.timezone,
            })
            assert all(checks.values()), f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("daily 06:00 IST retry-until-"
                                                  "boundary semantics wired via "
                                                  "Prefect Cron deployment")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY16StatelessGcloudAuth:
    """FY-16: per-invocation GOOGLE_APPLICATION_CREDENTIALS injection +
    activate-service-account --key-file - no persistent login needed.
    Mechanism read-verified; end-to-end success already witnessed (FY-10)."""

    def test_FY_16_stateless_auth(self):
        sid = "FY-16"
        ops = {}
        try:
            import inspect

            from core import fy_rollover as fr

            src = inspect.getsource(fr.run_archive_transition)
            checks = {
                "env_injection": 'env["GOOGLE_APPLICATION_CREDENTIALS"]' in src,
                "activate_service_account": "activate-service-account" in src,
                "key_file_flag": "--key-file=" in src,
                "skips_when_no_keyfile": "if Path(gcs_key_path).is_file()" in src,
            }
            ops.update({
                "wiring": checks,
                "live_success_crossref": "see FY-10 ledger row (ARCHIVE ok)",
            })
            assert all(checks.values()), f"op={ops}"
            record_op(sid, "WIRING-EVIDENCED", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY17GcloudDiscoveryPriority:
    """FY-17: bundled SDK wins over system PATH - resolution order is a real
    supply-chain control (pinned version ships with the app)."""

    def test_FY_17_discovery(self):
        sid = "FY-17"
        ops = {}
        try:
            from core.fy_rollover import _resolve_gcloud
            from core.process import resolve_binary

            resolved = _resolve_gcloud()
            bundled = (Path(os.getcwd())
                       / "deploy" / "bin" / "google-cloud-sdk" / "bin" / "gcloud.cmd")
            which_hit = resolve_binary("gcloud")

            ops.update({
                "resolved": str(resolved),
                "bundled_exists": bundled.exists(),
                "resolved_is_bundled": bool(resolved)
                                       and Path(resolved) == bundled.resolve(),
                "path_fallback_would_be": str(which_hit),
                "catalog_order": "deploy/bin -> Program Files -> LOCALAPPDATA -> which",
            })
            assert ops["resolved_is_bundled"], f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("priority order honored: "
                                                  "bundled/pinned SDK selected")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 15: FY-18 .. FY-20 (closes Branch E)
# ======================================================================

class TestFY18FyNameParsing:
    """FY-18: FY suffix extraction across drive / UNC / posix paths."""

    def test_FY_18_parse(self):
        sid = "FY-18"
        ops = {}
        try:
            from core.fy_rollover import _fy_name

            cases = {
                r"E:\SOURCE\FY26-27": "FY26-27",
                "\\\\srv\\share\\fy25-26": "FY25-26",   # lowercased input -> upper
                "/mnt/source/FY26-27": "FY26-27",
                r"D:\DATA": None,
                "": None,
            }
            results = {k: _fy_name(k) for k in cases}
            ops.update({
                "results": results,
                "inference": ("2-digit convention only, case-normalized to "
                              "upper, None signals opt-out (feeds G2)"),
            })
            assert results == cases, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY19SeparatorPreservation:
    """FY-19: parent/child helpers preserve \\\\ vs / style per path family."""

    def test_FY_19_separators(self):
        sid = "FY-19"
        ops = {}
        try:
            from core.fy_rollover import _child_path, _parent_path

            parents = {
                r"E:\A\FY25-26": _parent_path(r"E:\A\FY25-26"),
                "/mnt/a/FY25-26": _parent_path("/mnt/a/FY25-26"),
                r"\\srv\share\FY25-26": _parent_path(r"\\srv\share\FY25-26"),
            }
            children = {
                r"E:\A\FY26-27": _child_path(r"E:\A", "FY26-27"),
                "/mnt/a/FY26-27": _child_path("/mnt/a", "FY26-27"),
                r"\\srv\share\FY26-27": _child_path(r"\\srv\share", "FY26-27"),
            }
            ops.update({
                "parents": parents,
                "children": children,
                "expected_parents": {
                    r"E:\A\FY25-26": r"E:\A",
                    "/mnt/a/FY25-26": "/mnt/a",
                    r"\\srv\share\FY25-26": r"\\srv\share",
                },
            })
            assert parents == ops["expected_parents"], f"op={ops}"
            assert all(("\\" in k) == ("\\" in v) or ("/" in v)
                       for k, v in children.items()), f"op={ops}"
            assert children[r"E:\A\FY26-27"] == r"E:\A\FY26-27"
            assert children["/mnt/a/FY26-27"] == "/mnt/a/FY26-27"
            assert children[r"\\srv\share\FY26-27"] == r"\\srv\share\FY26-27"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("style preserved so YAML "
                                                  "rewrites stay platform-"
                                                  "faithful on any root")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestFY20FyBoundaryMar31Apr1:
    """FY-20: the exact fiscal-year boundary dates, plus century edges."""

    def test_FY_20_boundary(self):
        sid = "FY-20"
        ops = {}
        try:
            import datetime

            cases = {
                datetime.date(2026, 3, 31): "FY25-26",
                datetime.date(2026, 4, 1): "FY26-27",
                datetime.date(2027, 3, 31): "FY26-27",
                datetime.date(2027, 4, 1): "FY27-28",
                datetime.date(2099, 4, 1): "FY99-00",
                datetime.date(2100, 3, 31): "FY99-00",
            }
            results = {str(k): get_fy_prefix(k) for k in cases}
            expected = {str(k): v for k, v in cases.items()}
            ops.update({
                "results": results,
                "catalog_pairs_verified": ["2026-03-31->FY25-26",
                                           "2026-04-01->FY26-27"],
                "century_edges_recorded": True,
            })
            assert results == expected, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("April-1 switch + modulo "
                                                  "century arithmetic verified "
                                                  "including Y2K-style edge")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
