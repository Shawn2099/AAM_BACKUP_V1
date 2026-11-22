"""Break-tests for ManifestDB (core/manifest.py) and run-artefact retention (core/run_artefacts.py).

Invariants under test:
  * purge_older_than never deletes anything outside its root, and never a folder that is not a
    day folder (YYYY-MM-DD).
  * DB and artefact-store errors (corrupt, locked, disk full, odd rows) reach the caller or are
    logged as degraded; they are never reported as a successful write or an empty success.

All files live under pytest's tmp_path. Nothing here touches the real config or runtime folders.
"""

import errno
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from core import run_artefacts, statuses
from core.manifest import ManifestDB
from core.run_artefacts import RunArtefacts, purge_older_than

DAY = 86400
NOW = 1_800_000_000.0          # fixed clock passed as now=, so the age checks are deterministic
OLD = NOW - 200 * DAY
RECENT = NOW - 1 * DAY
FUTURE = NOW + 30 * DAY
WINDOWS = sys.platform.startswith("win")


# ── helpers ─────────────────────────────────────────────────────────────────

def _stamp(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _day(root: Path, name: str, when: float = OLD, files=("rclone.log",)) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for f in files:
        p = d / f
        p.write_text("log line\n", encoding="utf-8")
        _stamp(p, when)
    _stamp(d, when)
    return d


def _outside(tmp_path: Path) -> Path:
    out = tmp_path / "outside_secret"
    out.mkdir()
    (out / "keep.txt").write_text("do not delete", encoding="utf-8")
    _stamp(out / "keep.txt", OLD)
    _stamp(out, OLD)
    return out


def _assert_outside_intact(out: Path) -> None:
    assert (out / "keep.txt").read_text(encoding="utf-8") == "do not delete"


def _make_dir_link(link: Path, target: Path) -> bool:
    """A directory symlink, or a Windows junction when symlinks need privilege. False if neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        pass
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, check=False)
    return done.returncode == 0 and link.exists()


def _run(run_id, status="CLOUD_COMPLETE", mode="cloud", started="2026-11-20T10:00:00+05:30", **extra):
    row = {"run_id": run_id, "mode": mode, "fy": "FY26-27", "started_at": started, "status": status}
    row.update(extra)
    return row


def _ids(db: ManifestDB) -> list[str]:
    return [r["run_id"] for r in db.get_recent_runs(10_000, fy="FY26-27")]


class _Failing:
    """Stands in for the sqlite connection and raises one chosen error on one kind of call.

    when = "write"  : INSERT/UPDATE/DELETE raise `exc` at execute time
    when = "commit" : the real write runs, then commit() raises `exc` (disk full at fsync)
    when = "read"   : SELECT raises `exc`
    """

    def __init__(self, conn, when: str, exc: BaseException):
        self._conn, self._when, self._exc = conn, when, exc

    def execute(self, sql, *args):
        verb = sql.lstrip().split(None, 1)[0].upper()
        if self._when == "write" and verb in ("INSERT", "UPDATE", "DELETE"):
            raise self._exc
        if self._when == "read" and verb == "SELECT":
            raise self._exc
        return self._conn.execute(sql, *args)

    def commit(self):
        if self._when == "commit":
            raise self._exc
        return self._conn.commit()

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc_info):
        return self._conn.__exit__(*exc_info)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def db(tmp_path):
    handle = ManifestDB(tmp_path / "manifest.db", busy_timeout_ms=200)
    yield handle
    handle.close()


# ── purge_older_than: what may be removed ──────────────────────────────────

def test_old_day_folders_go_recent_ones_stay(tmp_path):
    root = tmp_path / "run_logs"
    root.mkdir()
    old = _day(root, "2026-01-01")
    recent = _day(root, "2026-11-21", when=RECENT)
    assert purge_older_than(root, 90, now=NOW) == 1
    assert not old.exists() and recent.exists()


def test_only_real_day_folder_names_are_removed_even_when_old(tmp_path):
    root = tmp_path / "run_logs"
    root.mkdir()
    names = ["FY26-27", "archive", "2026-1-1", "2026-01-01x", "2026-01-01 copy", ".2026-01-01",
             "2026_01_01", "ഫോൾഡർ-ഒഴിവാക്കരുത്"]   # last one: Malayalam, unicode name, not a day
    kept = [_day(root, name) for name in names]
    assert purge_older_than(root, 90, now=NOW) == 0
    assert all(p.exists() for p in kept)


def test_future_dated_folders_are_kept(tmp_path):
    root = tmp_path / "run_logs"
    root.mkdir()
    future = _day(root, "2026-12-30", when=FUTURE)
    assert purge_older_than(root, 90, now=NOW) == 0
    assert future.exists()


def test_huge_retention_removes_nothing_and_does_not_raise(tmp_path):
    root = tmp_path / "run_logs"
    root.mkdir()
    old = _day(root, "2026-01-01")
    assert purge_older_than(root, 10**9, now=NOW) == 0
    assert old.exists()


@pytest.mark.xfail(strict=False, reason="SUSPECTED BUG: core/run_artefacts.py:263 has no floor on "
                   "`days` (config floor is 7). Retention 0 or negative makes the cutoff now or later, "
                   "so today's folder, possibly a run in progress, is deleted.")
@pytest.mark.parametrize("days", [0, -1])
def test_zero_or_negative_retention_never_removes_todays_folder(tmp_path, days):
    root = tmp_path / "run_logs"
    root.mkdir()
    today = _day(root, "2026-11-22", when=NOW - 60)
    purge_older_than(root, days, now=NOW)
    assert today.exists()


@pytest.mark.xfail(strict=False, reason="SUSPECTED BUG: core/run_artefacts.py:263 computes "
                   "int(days)*86400 and the float arithmetic outside the try, so an absurd retention "
                   "raises OverflowError although the docstring says 'Never raises'.")
def test_absurd_retention_does_not_raise(tmp_path):
    root = tmp_path / "run_logs"
    root.mkdir()
    _day(root, "2026-01-01")
    assert purge_older_than(root, 10**400, now=NOW) == 0


@pytest.mark.skipif(not WINDOWS, reason="Windows keeps an open file from being deleted")
def test_file_held_open_by_another_handle_is_skipped_without_raising(tmp_path):
    root = tmp_path / "run_logs"
    root.mkdir()
    day = _day(root, "2026-01-01")
    held = day / "rclone.log"
    with open(held, "a", encoding="utf-8") as fh:
        fh.write("still being written\n")
        fh.flush()
        removed = purge_older_than(root, 90, now=NOW)   # must not raise
        assert held.exists()
    assert removed == 0


@pytest.mark.skipif(not WINDOWS, reason="read-only attribute blocks deletion on Windows")
def test_read_only_file_in_old_day_is_skipped_without_raising_or_escaping(tmp_path):
    root = tmp_path / "run_logs"
    root.mkdir()
    out = _outside(tmp_path)
    day = _day(root, "2026-01-01", files=("ro.log", "rw.log"))
    ro = day / "ro.log"
    os.chmod(ro, stat.S_IREAD)
    _stamp(day, OLD)
    try:
        assert purge_older_than(root, 90, now=NOW) == 0   # folder cannot be fully removed
    finally:
        os.chmod(ro, stat.S_IREAD | stat.S_IWRITE)
    _assert_outside_intact(out)


@pytest.mark.skipif(not WINDOWS, reason="one stuck folder is only stuck on Windows")
def test_one_stuck_folder_does_not_stop_the_others(tmp_path):
    root = tmp_path / "run_logs"
    root.mkdir()
    stuck = _day(root, "2026-01-01", files=("ro.log",))
    os.chmod(stuck / "ro.log", stat.S_IREAD)
    _stamp(stuck, OLD)
    gone = _day(root, "2026-01-02")
    try:
        removed = purge_older_than(root, 90, now=NOW)
    finally:
        os.chmod(stuck / "ro.log", stat.S_IREAD | stat.S_IWRITE)
    assert removed == 1 and not gone.exists()


@pytest.mark.parametrize("kind", ["dir_link_named_like_a_day", "junction_inside_old_day", "file_link_at_root"])
def test_links_pointing_outside_the_log_tree_are_never_followed(tmp_path, kind):
    root = tmp_path / "run_logs"
    root.mkdir()
    out = _outside(tmp_path)
    if kind == "dir_link_named_like_a_day":
        if not _make_dir_link(root / "2026-01-01", out):
            pytest.skip("cannot create a directory link on this machine")
    elif kind == "junction_inside_old_day":
        day = _day(root, "2026-01-02", files=())
        if not _make_dir_link(day / "escape", out):
            pytest.skip("cannot create a directory link on this machine")
        _stamp(day, OLD)
    else:
        try:
            os.symlink(out / "keep.txt", root / "link.log")
        except OSError:
            pytest.skip("cannot create file symlinks on this machine")
        _stamp(out / "keep.txt", OLD)
    purge_older_than(root, 90, now=NOW)   # must not raise, must not reach outside
    _assert_outside_intact(out)


def test_very_long_path_does_not_raise(tmp_path):
    root = tmp_path / "run_logs"
    root.mkdir()
    day = root / "2026-01-01"
    cur = day
    try:
        for i in range(12):
            cur = cur / f"segment_{i:02d}_{'x' * 30}"
        cur.mkdir(parents=True)
    except OSError:
        pytest.skip("paths this long cannot be created here (MAX_PATH)")
    (cur / "deep.log").write_text("x", encoding="utf-8")
    _stamp(day, OLD)
    removed = purge_older_than(root, 90, now=NOW)   # must not raise
    assert removed in (0, 1)
    assert root.exists()


@pytest.mark.xfail(strict=False, reason="SUSPECTED BUG: core/run_artefacts.py:37 _DATE_DIR uses \\d, "
                   "which also matches non-ASCII digits, so an old folder named with Arabic-Indic digits "
                   "is treated as a day folder and deleted.")
def test_non_ascii_digit_folder_is_not_a_day_folder(tmp_path):
    root = tmp_path / "run_logs"
    root.mkdir()
    odd = _day(root, "٢٠٢٦-٠١-٠١")   # "2026-01-01" in Arabic-Indic digits
    purge_older_than(root, 90, now=NOW)
    assert odd.exists()


@pytest.mark.xfail(strict=False, reason="SUSPECTED BUG: core/run_artefacts.py:270-274 removes any old "
                   "plain file matching the glob, not only day folders (the comment says 'only ever "
                   "remove our own day folders'). Default glob '*' reaches loose files at the root.")
def test_loose_old_file_at_root_is_kept(tmp_path):
    root = tmp_path / "run_logs"
    root.mkdir()
    note = root / "operator-notes.txt"
    note.write_text("keep me", encoding="utf-8")
    _stamp(note, OLD)
    purge_older_than(root, 90, now=NOW)
    assert note.exists()


def test_missing_root_and_file_as_root_return_zero(tmp_path):
    assert purge_older_than(tmp_path / "no_such_root", 90, now=NOW) == 0
    as_file = tmp_path / "a_file"
    as_file.write_text("x", encoding="utf-8")
    assert purge_older_than(as_file, 90, now=NOW) == 0
    assert as_file.exists()


# ── ManifestDB: corrupt, locked, full, missing files ───────────────────────

def test_zero_byte_database_file_opens_as_a_fresh_database(tmp_path):
    path = tmp_path / "manifest.db"
    path.touch()
    handle = ManifestDB(path, busy_timeout_ms=200)
    try:
        handle.insert_run(_run("r-zero"))
        assert _ids(handle) == ["r-zero"]
    finally:
        handle.close()


@pytest.mark.parametrize("kind", ["truncated", "garbage"])
def test_truncated_or_garbage_database_is_refused_and_left_untouched(tmp_path, kind):
    path = tmp_path / "manifest.db"
    seed = ManifestDB(path, busy_timeout_ms=200)
    seed.insert_run(_run("r1"))
    seed.close()
    if kind == "truncated":
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 3])
    else:
        path.write_bytes(b"not a database, just text " * 200)
    before = path.read_bytes()
    handle = ManifestDB(path, busy_timeout_ms=200)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            handle.get_recent_runs(10, fy="FY26-27")   # surfaced, not an empty list
    finally:
        handle.close()
    assert path.read_bytes() == before


def test_write_blocked_by_another_connection_raises_after_busy_timeout(tmp_path):
    path = tmp_path / "manifest.db"
    handle = ManifestDB(path, busy_timeout_ms=200)
    handle.get_recent_runs(1, fy="FY26-27")          # open and create the schema first
    holder = sqlite3.connect(path, timeout=0, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")                # another process holds the write lock
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            handle.insert_run(_run("r-blocked"))
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    handle.insert_run(_run("r-after"))               # once released, the write goes through
    assert _ids(handle) == ["r-after"]
    handle.close()


@pytest.mark.parametrize("when,exc", [
    ("write", OSError(errno.ENOSPC, "No space left on device")),
    ("commit", sqlite3.OperationalError("database or disk is full")),
], ids=["enospc-on-write", "disk-full-on-commit"])
def test_disk_full_while_writing_is_raised_and_nothing_is_reported_saved(tmp_path, when, exc):
    handle = ManifestDB(tmp_path / "manifest.db", busy_timeout_ms=200)
    handle.get_recent_runs(1, fy="FY26-27")
    real = handle._conn
    handle._conn = _Failing(real, when, exc)
    try:
        with pytest.raises((OSError, sqlite3.OperationalError), match="space|full"):
            handle.insert_run(_run("r-full"))
    finally:
        handle._conn = real
        real.rollback()
    assert _ids(handle) == []                        # the failed write is not visible as saved
    handle.close()


def test_read_error_is_raised_not_reported_as_no_success(tmp_path):
    handle = ManifestDB(tmp_path / "manifest.db", busy_timeout_ms=200)
    handle.insert_run(_run("r-ok", status="CLOUD_COMPLETE"))
    real = handle._conn
    handle._conn = _Failing(real, "read", sqlite3.OperationalError("disk I/O error"))
    try:
        with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
            handle.last_successful_run("cloud", fy="FY26-27")   # must not return None
    finally:
        handle._conn = real
    handle.close()


def test_database_reopens_cleanly_after_its_wal_and_shm_files_are_gone(tmp_path):
    path = tmp_path / "manifest.db"
    handle = ManifestDB(path, busy_timeout_ms=200)
    handle.insert_run(_run("r1"))
    handle.close()                                   # clean close checkpoints the WAL
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    again = ManifestDB(path, busy_timeout_ms=200)
    try:
        assert _ids(again) == ["r1"]
    finally:
        again.close()


# ── ManifestDB: concurrent writers ─────────────────────────────────────────

@pytest.mark.parametrize("shared", [True, False], ids=["one-instance", "instance-per-thread"])
def test_concurrent_writers_lose_no_run_rows(tmp_path, shared):
    path = tmp_path / "manifest.db"
    threads_n, per_thread = 6, 25
    shared_db = ManifestDB(path, busy_timeout_ms=5000)
    shared_db.get_recent_runs(1, fy="FY26-27")
    errors: list[str] = []

    def worker(t: int) -> None:
        handle = shared_db if shared else ManifestDB(path, busy_timeout_ms=5000)
        try:
            for i in range(per_thread):
                handle.insert_run(_run(f"t{t}-{i}"))
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(repr(exc))
        finally:
            if not shared:
                handle.close()

    workers = [threading.Thread(target=worker, args=(t,)) for t in range(threads_n)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=120)
    assert errors == []
    shared_db.close()
    check = ManifestDB(path, busy_timeout_ms=5000)
    try:
        assert len(_ids(check)) == threads_n * per_thread
    finally:
        check.close()


# ── run_history rows: odd statuses and None fields ─────────────────────────

ODD_STATUSES = ["", "cloud_complete", "CLOUD_COMPLETE ", "FOO_COMPLETE", "SUCCESS_NO",
                "LAN_SUCCESS", "NONE", "CLOUD_FAILED_COMPLETE"]


def test_odd_and_lookalike_statuses_never_count_as_success(db):
    db.insert_run(_run("real", status="CLOUD_COMPLETE", started="2026-11-20T08:00:00+05:30"))
    for i, status in enumerate(ODD_STATUSES):
        db.insert_run(_run(f"odd{i}", status=status, started=f"2026-11-20T09:0{i}:00+05:30"))
    assert db.last_successful_run("cloud", fy="FY26-27")["run_id"] == "real"
    for status in ODD_STATUSES:
        assert not statuses.is_success(status), repr(status)
        assert statuses.category(status) == statuses.CATEGORY_UNKNOWN, repr(status)
    assert statuses.label("FOO_COMPLETE") == "FOO_COMPLETE"   # unknown shown in full, never cut


def test_every_known_status_agrees_with_the_success_set(tmp_path):
    for member in statuses.RunStatus:
        handle = ManifestDB(tmp_path / f"{member.value}.db", busy_timeout_ms=200)
        try:
            handle.insert_run(_run("only", status=member.value))
            got = handle.last_successful_run("cloud", fy="FY26-27")
            assert (got is not None) == statuses.is_success(member.value), member.value
        finally:
            handle.close()


def test_none_in_optional_fields_round_trips_and_null_status_is_refused(db):
    db.insert_run({
        "run_id": "none1", "mode": "lan", "fy": "FY26-27", "started_at": "2026-11-20T10:00:00+05:30",
        "status": "LAN_COMPLETE", "ended_at": None, "exit_code": None, "files_copied": None,
        "bytes_copied": None, "files_failed": None, "duration_seconds": None,
        "error_message": None, "extended_metrics": None,
    })
    row = db.last_successful_run("lan", fy="FY26-27")
    assert row["run_id"] == "none1" and row["exit_code"] is None
    assert statuses.describe(row["status"], row["files_copied"], row["files_failed"]) \
        == "Backup complete - 0 file(s) transferred"
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_run(_run("null-status", status=None))
    assert _ids(db) == ["none1"]


# ── run artefacts: disk-full on the artefact store ─────────────────────────

def test_disk_full_on_artefact_store_is_logged_and_marks_the_run_degraded(tmp_path, monkeypatch):
    art = RunArtefacts(tmp_path / "run_logs", "probe", day="2026-11-22")

    def no_space(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(run_artefacts.tempfile, "mkstemp", no_space)
    art.note("written while the disk is full")       # must not raise: a log failure never breaks a backup
    monkeypatch.undo()

    def probe_fails(_path):
        raise OSError("volume not readable")

    monkeypatch.setattr(run_artefacts.shutil, "disk_usage", probe_fails)
    assert art.check_free_space(5.0) is False
    assert art.degraded is True                       # the failed probe is recorded on disk
