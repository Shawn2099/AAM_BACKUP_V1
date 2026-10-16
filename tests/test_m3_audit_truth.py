"""M3 3.3 — truthful verification and audit (plan WP7).

Findings: the weekly audit recorded DNS/TLS/unreachable failures as "content
mismatch, missing-from-dest=20, elapsed_s=0" (production Sep 27, Oct 4,
Oct 11) - a false data-loss signal; the daily size-only check was labelled
"Verification Passed" / "Cryptographic Checks" (audit H-1); the cloud
capability label was rewritten after it had been logged.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core import integrity
from core.manifest import ManifestDB
from models.config import AppConfig, AuditConfig
from test_support.fake_tools import SubprocessStyleRunTool


class _Done:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _check(monkeypatch, *, rc, stderr="", rows=(), patterns=()):
    def fake_run(cmd, **kw):
        idx = cmd.index("--combined")
        with open(cmd[idx + 1], "w", encoding="utf-8") as f:
            f.write("".join(f"{m} {p}\n" for m, p in rows))
        return _Done(rc, "", stderr)

    monkeypatch.setattr(integrity, "resolve_binary", lambda n: "rclone")
    monkeypatch.setattr(integrity, "run_tool", SubprocessStyleRunTool(side_effect=fake_run))
    return integrity._run_rclone_check("SRC", "DST", "lan/full", 60,
                                       network_error_patterns=tuple(patterns))


# Real production signatures (rclone v1.74.x logs 2026-09/10).
TLS = ("2026/10/04 03:05:12 ERROR : : error reading destination root directory: Get "
       "\"https://storage.googleapis.com/...\": tls: failed to verify certificate: x509: "
       "certificate signed by unknown authority\n"
       "2026/10/04 03:05:12 NOTICE: 20 differences found\n")
DNS = ("2026/10/11 03:05:01 ERROR : : error listing: dial tcp: lookup "
       "storage.googleapis.com: no such host\n2026/10/11 03:05:01 NOTICE: 20 differences found\n")
UNC = "2026/09/27 03:05:00 ERROR : The network path was not found.\n"


@pytest.mark.parametrize("stderr,rc,category", [
    (TLS, 1, "TLS_INTERCEPTED"),     # exit 1 + "differences" - but nothing was compared
    (DNS, 1, "DNS"),
    (UNC, 3, "NETWORK"),
])
def test_transport_failure_is_audit_incomplete_not_divergence(monkeypatch, stderr, rc, category):
    res = _check(monkeypatch, rc=rc, stderr=stderr,
                 rows=[("+", f"f{i}.txt") for i in range(20)])
    assert res["status"] == "AUDIT_INCOMPLETE"
    assert category in res["detail"] and "missing-from-dest" not in res["detail"]
    assert res["mismatches"] == -1


@pytest.mark.parametrize("rc,stderr,present", [
    (2, "something broke", True),            # process error, no transport signature
    (0, "", True),                           # killed: exit 0 but no completion summary
])
def test_process_failure_is_audit_incomplete(monkeypatch, rc, stderr, present):
    res = _check(monkeypatch, rc=rc, stderr=stderr)
    assert res["status"] == "AUDIT_INCOMPLETE"


def test_timeout_and_spawn_failure_are_audit_incomplete(monkeypatch):
    monkeypatch.setattr(integrity, "resolve_binary", lambda n: "rclone")
    for exc in (subprocess.TimeoutExpired(cmd="rclone", timeout=60), OSError("no rclone")):
        monkeypatch.setattr(integrity, "run_tool", SubprocessStyleRunTool(side_effect=exc))
        res = integrity._run_rclone_check("S", "D", "lan/full", 60)
        assert res["status"] == "AUDIT_INCOMPLETE"


def test_completed_check_with_differences_is_still_verification_failed(monkeypatch):
    res = _check(monkeypatch, rc=1, stderr="2026/10/04 03:05:12 NOTICE: 1 differences found\n",
                 rows=[("*", "ledger.xlsx"), ("=", "a.txt")])
    assert res["status"] == "VERIFICATION_FAILED" and "content-changed=1" in res["detail"]


def test_clean_completed_check_is_verified(monkeypatch):
    res = _check(monkeypatch, rc=0, stderr="2026/10/04 03:05:12 NOTICE: 0 differences found\n",
                 rows=[("=", "a.txt")])
    assert res["status"] == "VERIFIED"


def test_configured_extra_pattern_classifies_as_incomplete(monkeypatch):
    stderr = "2026/10/04 03:05:12 ERROR : proxy said: SOPHOS BLOCKPAGE\n1 differences found\n"
    res = _check(monkeypatch, rc=1, stderr=stderr, rows=[("+", "a.txt")],
                 patterns=[r"sophos blockpage"])
    assert res["status"] == "AUDIT_INCOMPLETE" and "sophos" in res["detail"].lower()


def test_cloud_capability_label_is_decided_before_logging(monkeypatch):
    logged = []
    monkeypatch.setattr(integrity.logger, "info", lambda msg: logged.append(msg))
    _check_cloud = integrity.audit_cloud
    monkeypatch.setattr(integrity, "resolve_binary", lambda n: "rclone")

    def fake_run(cmd, **kw):
        idx = cmd.index("--combined")
        open(cmd[idx + 1], "w", encoding="utf-8").close()
        return _Done(0, "", "0 differences found\n")

    monkeypatch.setattr(integrity, "run_tool", SubprocessStyleRunTool(side_effect=fake_run))
    res = _check_cloud("S", "bkt", "FY26-27", "/c", timeout=60)
    assert res["capability"] == integrity.CAPABILITY_HASH_GCS
    verdict_lines = [m for m in logged if "VERIFIED" in m]
    assert verdict_lines and integrity.CAPABILITY_HASH_LOCAL not in verdict_lines[0]


def test_audit_settings_are_config():
    a = AuditConfig()
    assert (a.checkers, a.retries, a.detail_max_paths, a.network_error_patterns) == (1, 3, 100, [])
    assert "audit" in AppConfig.model_fields


def test_db_accepts_audit_incomplete(tmp_path):
    db = ManifestDB(str(tmp_path / "m.db"))
    try:
        db.record_audit({"audit_id": "a", "mode": "lan", "started_at": "2026-10-11T03:00:00",
                         "status": "AUDIT_INCOMPLETE", "detail": "DNS", "created_at": "x"})
        assert db.latest_audit("lan")["status"] == "AUDIT_INCOMPLETE"
    finally:
        db.close()


# ── the daily check is a SIZE check, and is called that ──────────────────

def test_daily_check_is_labelled_size_check_everywhere():
    import inspect

    import flow
    src = inspect.getsource(flow)
    assert "Cryptographic Checks" not in src
    js = open("static/js/dashboard.js", encoding="utf-8").read()
    assert "Verification Passed" not in js and "Size check passed" in js


def test_ui_and_report_say_the_audit_could_not_run():
    js = open("static/js/dashboard.js", encoding="utf-8").read()
    assert "AUDIT_INCOMPLETE" in js
    from core import report
    import inspect
    assert "AUDIT_INCOMPLETE" in inspect.getsource(report)
