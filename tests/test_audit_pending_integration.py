"""Step 2 (call site) of docs/PLAN_AUDIT_PENDING: the check uses the classifier ONLY for a
completed check that found differences. Every fail-closed branch before it is unchanged."""

import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import flow
from core import audit_pending as ap
from core import integrity
from test_support.fake_tools import SubprocessStyleRunTool

NOW = time.time()
REF = NOW - 6 * 3600
OLD = REF - 3 * 86400
NEW = REF + 3600


class _Done:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _stat(birth):
    def stat(path):
        st = os.stat(path)
        return SimpleNamespace(st_mtime=st.st_mtime, st_size=st.st_size, st_birthtime=birth)
    return stat


def _ctx(src, birth=OLD, ref=REF):
    return ap.PendingContext(source_root=str(src), dest_root=None, reference=ref, now=NOW,
                             modify_window_seconds=2, max_delete_files=500, max_delete_percent=10,
                             files_checked=None, stat=_stat(birth))


def _write(root, rel, mtime):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    os.utime(p, (mtime, mtime))
    os.utime(p.parent, (OLD, OLD))


def _check(monkeypatch, rows, pending=None, rc=1, stderr=None):
    n = len([1 for m, _ in rows if m != "="])
    if stderr is None:
        stderr = f"2026/10/19 15:01:00 NOTICE: {n} differences found\n"

    def fake_run(cmd, **kw):
        with open(cmd[cmd.index("--combined") + 1], "w", encoding="utf-8") as f:
            f.write("".join(f"{m} {p}\n" for m, p in rows))
        return _Done(rc, "", stderr)

    monkeypatch.setattr(integrity, "resolve_binary", lambda n: "rclone")
    monkeypatch.setattr(integrity, "run_tool", SubprocessStyleRunTool(side_effect=fake_run))
    return integrity._run_rclone_check("SRC", "DST", "lan/full", 60, pending=pending)


def test_only_waiting_files_give_pending_backup(monkeypatch, tmp_path):
    _write(tmp_path, "new.txt", NEW)
    _write(tmp_path, "edit.txt", NEW)
    rows = [("+", "new.txt"), ("*", "edit.txt"), ("=", "same.txt")]
    res = _check(monkeypatch, rows, _ctx(tmp_path, birth=NEW))
    assert res["status"] == "PENDING_BACKUP" and res["mismatches"] == 0
    assert "1 new" in res["detail"] and "1 changed" in res["detail"]
    assert "wait for the next backup" in res["detail"]
    assert res["files_checked"] == 3


def test_a_real_difference_stays_verification_failed_and_names_only_it(monkeypatch, tmp_path):
    _write(tmp_path, "new.txt", NEW)
    _write(tmp_path, "bad.txt", OLD)
    res = _check(monkeypatch, [("+", "new.txt"), ("*", "bad.txt")], _ctx(tmp_path, birth=NEW))
    assert res["status"] == "VERIFICATION_FAILED" and res["mismatches"] == 1
    assert "bad.txt" in res["detail"] and "1 more file" in res["detail"]


def test_without_a_context_nothing_changes(monkeypatch, tmp_path):
    _write(tmp_path, "new.txt", NEW)
    res = _check(monkeypatch, [("+", "new.txt")], None)
    assert res["status"] == "VERIFICATION_FAILED" and res["mismatches"] == 1


def test_no_last_backup_time_keeps_everything_real(monkeypatch, tmp_path):
    _write(tmp_path, "new.txt", NEW)
    res = _check(monkeypatch, [("+", "new.txt")], _ctx(tmp_path, birth=NEW, ref=None))
    assert res["status"] == "VERIFICATION_FAILED"


def test_fail_closed_branches_are_decided_before_the_classifier(monkeypatch, tmp_path):
    _write(tmp_path, "new.txt", NEW)
    ctx = _ctx(tmp_path, birth=NEW)
    tls = ("2026/10/04 03:05:12 ERROR : : tls: failed to verify certificate: x509: certificate "
           "signed by unknown authority\n2026/10/04 03:05:12 NOTICE: 1 differences found\n")
    assert _check(monkeypatch, [("+", "new.txt")], ctx, stderr=tls)["status"] == "AUDIT_INCOMPLETE"
    assert _check(monkeypatch, [("+", "new.txt")], ctx, rc=2, stderr="boom")["status"] == "AUDIT_INCOMPLETE"
    assert _check(monkeypatch, [("!", "new.txt")], ctx)["status"] == "VERIFICATION_FAILED"
    contradictory = "2026/10/19 15:01:00 NOTICE: 0 differences found\n"
    assert _check(monkeypatch, [("+", "new.txt")], ctx, stderr=contradictory)["status"] == "VERIFICATION_FAILED"


def test_size_only_never_becomes_pending(monkeypatch, tmp_path):
    _write(tmp_path, "new.txt", NEW)
    monkeypatch.setattr(integrity, "resolve_binary", lambda n: "rclone")

    def fake_run(cmd, **kw):
        with open(cmd[cmd.index("--combined") + 1], "w", encoding="utf-8") as f:
            f.write("+ new.txt\n")
        return _Done(1, "", "NOTICE: 1 differences found\n")

    monkeypatch.setattr(integrity, "run_tool", SubprocessStyleRunTool(side_effect=fake_run))
    res = integrity._run_rclone_check("S", "D", "lan/full", 60, size_only=True,
                                      pending=_ctx(tmp_path, birth=NEW))
    assert res["status"] == "VERIFICATION_FAILED"


# ── the flow builds the context from the last good backup ──────────────────

def _cfg():
    cfg = MagicMock()
    cfg.paths.source_drive = "E:\\FY26-27"
    cfg.paths.lan_destination = "\\\\nas\\share\\FY26-27"
    cfg.audit.modify_window_seconds = 2
    cfg.guards.max_delete_files = 500
    cfg.guards.max_delete_percent = 10.0
    return cfg


def test_flow_context_uses_the_start_of_the_last_successful_run():
    db = MagicMock()
    db.last_successful_run.return_value = {"started_at": "2026-10-19T00:00:00+00:00"}
    ctx = flow._pending_context(_cfg(), db, "lan")
    assert ctx.reference == 1792368000.0
    assert ctx.dest_root == "\\\\nas\\share\\FY26-27" and ctx.source_root == "E:\\FY26-27"
    assert (ctx.max_delete_files, ctx.max_delete_percent) == (500, 10.0)
    db.last_successful_run.assert_called_once_with("lan")


def test_flow_context_has_no_destination_folder_for_the_cloud():
    db = MagicMock()
    db.last_successful_run.return_value = {"started_at": "2026-10-19T00:00:00+00:00"}
    assert flow._pending_context(_cfg(), db, "cloud").dest_root is None


@pytest.mark.parametrize("last", [None, {"started_at": None}, {"started_at": "garbage"}, MagicMock()])
def test_flow_context_reference_is_unknown_when_there_is_no_usable_time(last):
    db = MagicMock()
    db.last_successful_run.return_value = last
    assert flow._pending_context(_cfg(), db, "lan").reference is None


def test_flow_context_survives_a_failing_database():
    db = MagicMock()
    db.last_successful_run.side_effect = RuntimeError("db locked")
    assert flow._pending_context(_cfg(), db, "lan").reference is None
