"""Step 2 of docs/PLAN_AUDIT_PENDING: the classifier that tells "waiting for the next backup" from
"really wrong". Real temp files and os.utime; only the creation time is injected (a test cannot
back-date it)."""

import os
from types import SimpleNamespace

import pytest

from core import audit_pending as ap

NOW = 1_800_000_000.0
REF = NOW - 6 * 3600          # the last good backup started 6 h ago
OLD = REF - 3 * 86400         # untouched since well before that backup
NEW = REF + 3600              # changed after it
WINDOW = 2


def _stat_with_birth(birth):
    """os.stat, but with the creation time chosen by the test."""
    def stat(path):
        st = os.stat(path)
        b = birth(path) if callable(birth) else birth
        return SimpleNamespace(st_mtime=st.st_mtime, st_size=st.st_size, st_birthtime=b)
    return stat


def _write(root, rel, mtime, data=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))
    return p


def _touch_dir(path, mtime):
    os.utime(path, (mtime, mtime))


def _ctx(src, dst=None, ref=REF, **kw):
    base = dict(source_root=str(src), dest_root=str(dst) if dst else None, reference=ref,
                now=NOW, modify_window_seconds=WINDOW, max_delete_files=500,
                max_delete_percent=10.0, files_checked=1000, stat=_stat_with_birth(OLD))
    base.update(kw)
    return ap.PendingContext(**base)


def classify(src, *, added=(), modified=(), removed=(), dst=None, **kw):
    return ap.classify(list(added), list(modified), list(removed), _ctx(src, dst, **kw))


@pytest.fixture
def src(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    return root


# ── missing from the copy (+) ──────────────────────────────────────────────

def test_new_file_since_the_last_backup_is_pending(src):
    _write(src, "a/new.txt", NEW)
    r = classify(src, added=["a/new.txt"], stat=_stat_with_birth(NEW))
    assert r.pending_count == 1 and r.real_count == 0


def test_file_older_than_the_last_backup_but_missing_is_real(src):
    _write(src, "a/old.txt", OLD)
    _touch_dir(src / "a", OLD)
    r = classify(src, added=["a/old.txt"])
    assert r.real_count == 1 and r.pending_count == 0


def test_old_file_copied_in_recently_is_pending_by_creation_time(src):
    _write(src, "a/moved_in.txt", OLD)          # old mtime kept, but created here after the backup
    _touch_dir(src / "a", OLD)
    r = classify(src, added=["a/moved_in.txt"], stat=_stat_with_birth(NEW))
    assert r.pending_count == 1


def test_old_file_in_a_folder_touched_since_is_pending(src):
    _write(src, "a/renamed.txt", OLD)
    _touch_dir(src / "a", NEW)                  # a rename/move changed the folder
    r = classify(src, added=["a/renamed.txt"])
    assert r.pending_count == 1


# ── content differs (*) ────────────────────────────────────────────────────

def test_edited_since_the_last_backup_is_pending(src):
    _write(src, "e.txt", NEW)
    assert classify(src, modified=["e.txt"]).pending_count == 1


def test_unchanged_since_the_last_backup_but_differs_is_real(src):
    _write(src, "e.txt", OLD)
    assert classify(src, modified=["e.txt"]).real_count == 1


def test_lan_copy_with_the_same_time_but_other_bytes_is_real_corruption(src, tmp_path):
    dst = tmp_path / "dst"
    _write(src, "e.txt", NEW, b"good")
    _write(dst, "e.txt", NEW, b"BAD!")          # same modified time, different bytes
    r = classify(src, modified=["e.txt"], dst=dst)
    assert r.real_count == 1 and r.pending_count == 0


def test_lan_copy_with_an_older_time_is_pending(src, tmp_path):
    dst = tmp_path / "dst"
    _write(src, "e.txt", NEW, b"v2")
    _write(dst, "e.txt", OLD, b"v1")
    assert classify(src, modified=["e.txt"], dst=dst).pending_count == 1


# ── only in the copy (-) ───────────────────────────────────────────────────

def test_deleted_file_is_pending_when_its_folder_changed_since(src):
    (src / "a").mkdir()
    _touch_dir(src / "a", NEW)
    assert classify(src, removed=["a/gone.txt"]).pending_count == 1


def test_extra_file_in_an_untouched_folder_is_real(src):
    (src / "a").mkdir()
    _touch_dir(src / "a", OLD)
    assert classify(src, removed=["a/stray.txt"]).real_count == 1


def test_deleted_folder_uses_the_nearest_existing_parent(src):
    (src / "a").mkdir()
    _touch_dir(src / "a", NEW)
    assert classify(src, removed=["a/b/c/gone.txt"]).pending_count == 1


# ── unknown is red ─────────────────────────────────────────────────────────

def test_no_last_backup_time_means_everything_is_real(src):
    _write(src, "n.txt", NEW)
    r = classify(src, added=["n.txt"], ref=None, stat=_stat_with_birth(NEW))
    assert r.real_count == 1 and r.pending_count == 0


def test_a_time_in_the_future_is_real(src):
    _write(src, "f.txt", NOW + 10 * 86400)
    assert classify(src, modified=["f.txt"]).real_count == 1


def test_a_file_that_cannot_be_read_is_real(src):
    assert classify(src, added=["does/not/exist.txt"]).real_count == 1


def test_stat_error_is_real(src):
    _write(src, "n.txt", NEW)

    def boom(path):
        raise PermissionError("denied")
    assert classify(src, modified=["n.txt"], stat=boom).real_count == 1


@pytest.mark.parametrize("bad", ["../escape.txt", "a/../../escape.txt", "C:/Windows/x.txt", "/abs.txt"])
def test_paths_that_leave_the_source_are_real(src, bad):
    assert classify(src, added=[bad]).real_count == 1


# ── mixed and tripwire ─────────────────────────────────────────────────────

def test_one_real_difference_makes_the_result_real_but_keeps_the_pending_count(src):
    _write(src, "new.txt", NEW)
    _write(src, "bad.txt", OLD)
    r = classify(src, added=["new.txt"], modified=["bad.txt"], stat=_stat_with_birth(NEW))
    assert r.real_count == 1 and r.pending_count == 1
    assert r.real_paths == ["bad.txt"]


def test_too_many_waiting_deletes_make_everything_real(src):
    (src / "a").mkdir()
    _touch_dir(src / "a", NEW)
    gone = [f"a/f{i}.txt" for i in range(11)]
    r = classify(src, removed=gone, max_delete_files=10, max_delete_percent=100)
    assert r.tripwire and r.pending_count == 0 and r.real_count == 11


def test_too_large_a_share_of_waiting_deletes_make_everything_real(src):
    (src / "a").mkdir()
    _touch_dir(src / "a", NEW)
    gone = [f"a/f{i}.txt" for i in range(150)]
    r = classify(src, removed=gone, files_checked=1000, max_delete_files=500, max_delete_percent=10)
    assert r.tripwire and r.real_count == 150


def test_deletes_within_the_limits_stay_pending(src):
    (src / "a").mkdir()
    _touch_dir(src / "a", NEW)
    r = classify(src, removed=["a/f1.txt", "a/f2.txt"])
    assert not r.tripwire and r.pending_count == 2


def test_nothing_to_classify_is_empty(src):
    r = classify(src)
    assert r.pending_count == 0 and r.real_count == 0


def test_summary_text_counts_each_kind(src):
    _write(src, "n.txt", NEW)
    _write(src, "e.txt", NEW)
    (src / "d").mkdir()
    _touch_dir(src / "d", NEW)
    r = classify(src, added=["n.txt"], modified=["e.txt"], removed=["d/x.txt"],
                 stat=_stat_with_birth(NEW))
    assert "1 new" in r.summary and "1 changed" in r.summary and "1 removed" in r.summary


def test_reference_from_iso_text():
    assert ap.reference_epoch("2026-10-19T05:30:00+05:30") == 1792368000.0
    assert ap.reference_epoch("2026-10-19T00:00:00+00:00") == 1792368000.0


def test_reference_epoch_is_none_for_missing_or_bad_text():
    assert ap.reference_epoch(None) is None
    assert ap.reference_epoch("not a time") is None
    assert ap.reference_epoch(12345) is None
