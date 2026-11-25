"""Plan P1 chunk C3: shared rclone log/combined parsers in core/cloud_sync.py.

Matrix rows (docs/PLAN_P1_RCLONE_LAN_2026-11-25.md, "P parsers (U)"):
  P1 combined counts (combined_counts), source/dest formulas from the counts.
  P2 names with spaces, unicode, CRLF; unknown first char counts as "?".
  P3 planned copy bytes: sum of sizes, None when a size is missing, 0 when none.
  P4 failed object paths from the final attempt only, capped.
  P5 rclone_deletes_of: stats.deletes, else the cap after a backstop stop, else None.
  P6 rename fixture: planned deletes 0, planned moves 5 (existing functions).

Fixtures are the real rclone v1.74.3 outputs of plan C0b
(test_support/rclone_logs/LAN_FIXTURES.md). The new functions are looked up on
the module at call time so each test fails on its own while they are missing.
"""

import json
from pathlib import Path

import core.cloud_sync as cloud_sync

FIXTURES = Path(__file__).resolve().parents[1] / "test_support" / "rclone_logs"
COUNT_KEYS = ("=", "*", "+", "-", "!", "?")


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _counts(**values) -> dict:
    """Expected combined_counts dict: every key present, unlisted keys 0."""
    out = {key: 0 for key in COUNT_KEYS}
    for key, value in values.items():
        out[{"eq": "=", "star": "*", "plus": "+", "minus": "-", "bang": "!", "q": "?"}[key]] = value
    return out


def _write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "combined.txt"
    path.write_bytes(data)
    return path


def _json_lines(*entries: dict) -> str:
    return "\n".join(json.dumps(e) for e in entries)


def _copy_entry(name: str, size) -> dict:
    entry = {"level": "notice", "msg": "Skipped copy as --dry-run is set",
             "skipped": "copy", "object": name, "objectType": "*local.Object"}
    if size is not None:
        entry["size"] = size
    return entry


# ── P1 combined counts ──────────────────────────────────────────────

class TestP1CombinedCounts:
    def test_clean_sync_is_twelve_plus_lines(self):
        counts = cloud_sync.combined_counts(FIXTURES / "lan_clean_combined.txt")
        assert counts == _counts(plus=12)

    def test_rename_fixture_counts_seven_same_five_plus_five_minus(self):
        counts = cloud_sync.combined_counts(FIXTURES / "lan_rename_combined.txt")
        assert counts == _counts(eq=7, plus=5, minus=5)

    def test_rename_fixture_source_and_dest_formulas_hold(self):
        # plan 2.3: source_files = "=" + "+" + "*"; dest_files = "=" + "*" + "-"
        # (fact sheet (c2): rename case 12 source = 7 "=" + 5 "+"; 12 dest = 7 "=" + 5 "-").
        counts = cloud_sync.combined_counts(FIXTURES / "lan_rename_combined.txt")
        assert counts["="] + counts["+"] + counts["*"] == 12
        assert counts["="] + counts["*"] + counts["-"] == 12

    def test_mixed_fixture_minus_lines_include_moved_sources(self):
        # 5 moves + 2 deletes show as 7 "-" lines in combined (plan C0b (c2)).
        counts = cloud_sync.combined_counts(FIXTURES / "lan_mixed_combined.txt")
        assert counts == _counts(eq=1, plus=5, minus=7)

    def test_check_clean_is_nine_equal_lines(self):
        counts = cloud_sync.combined_counts(FIXTURES / "lan_check_clean_combined.txt")
        assert counts == _counts(eq=9)

    def test_check_missing_has_one_plus(self):
        counts = cloud_sync.combined_counts(FIXTURES / "lan_check_missing_combined.txt")
        assert counts == _counts(eq=8, plus=1)

    def test_check_sizediff_has_one_star(self):
        counts = cloud_sync.combined_counts(FIXTURES / "lan_check_sizediff_combined.txt")
        assert counts == _counts(eq=8, star=1)

    def test_excluded_marker_dry_run_has_only_data_file(self):
        counts = cloud_sync.combined_counts(FIXTURES / "lan_h_marker_excluded_combined.txt")
        assert counts == _counts(plus=1)

    def test_crlf_lines_counted_and_bang_counted(self, tmp_path):
        path = _write(tmp_path, b"= a.txt\r\n+ b c.txt\r\n- \xe4\xb8\xad\xe6\x96\x87.txt\r\n! broken\r\n")
        assert cloud_sync.combined_counts(path) == _counts(eq=1, plus=1, minus=1, bang=1)

    def test_unknown_first_char_counts_under_question_mark(self, tmp_path):
        path = _write(tmp_path, b"# odd line\n= a\n")
        assert cloud_sync.combined_counts(path) == _counts(q=1, eq=1)

    def test_names_with_spaces_and_unicode_are_one_line_each(self, tmp_path):
        text = "= name with spaces & (parens).txt\n= Ünïcode café.txt\n+ 中文 文件.txt\n"
        path = _write(tmp_path, text.encode("utf-8"))
        assert cloud_sync.combined_counts(path) == _counts(eq=2, plus=1)


# ── P2 is the parser side of "names, CRLF, unknown first char" ──────
# (The plan-not-ok consequence of "?" is row PL3 and is tested with the plan wrapper.)

class TestP2NamesAndUnknownChars:
    def test_unknown_char_never_silently_dropped(self, tmp_path):
        path = _write(tmp_path, b"~ strange\n")
        counts = cloud_sync.combined_counts(path)
        assert sum(counts.values()) == 1
        assert counts["?"] == 1


# ── P3 planned copy bytes ───────────────────────────────────────────

class TestP3PlannedCopyBytes:
    def test_clean_dry_run_sums_copy_sizes(self):
        # Fact sheet (a): 12 copy entries, sizes sum to dry-run stats.bytes 13062.
        assert cloud_sync.planned_copy_bytes(_fixture("lan_clean_dryrun.jsonl")) == 13062

    def test_no_copy_entries_is_zero(self):
        # Rename dry-run: moves only, no "copy" entries.
        assert cloud_sync.planned_copy_bytes(_fixture("lan_rename_dryrun.jsonl")) == 0

    def test_copy_without_int_size_is_none(self):
        text = _json_lines(_copy_entry("a.txt", 10), _copy_entry("b.txt", None))
        assert cloud_sync.planned_copy_bytes(text) is None

    def test_copy_with_non_int_size_is_none(self):
        text = _json_lines(_copy_entry("a.txt", "10"))
        assert cloud_sync.planned_copy_bytes(text) is None

    def test_synthetic_sum(self):
        text = _json_lines(_copy_entry("a.txt", 10), _copy_entry("b.txt", 32))
        assert cloud_sync.planned_copy_bytes(text) == 42


class TestP3PlannedCopyCount:
    def test_clean_dry_run_has_twelve_copies(self):
        assert cloud_sync.planned_copy_count(_fixture("lan_clean_dryrun.jsonl")) == 12

    def test_rename_dry_run_has_no_copies(self):
        assert cloud_sync.planned_copy_count(_fixture("lan_rename_dryrun.jsonl")) == 0


# ── P4 failed object paths ──────────────────────────────────────────

class TestP4FailedObjectPaths:
    def test_locked_file_reports_final_attempt_object_once(self):
        # "Failed to copy: locked" appears in attempt 1 and attempt 2; only the
        # final attempt's line is reported.
        paths = cloud_sync.failed_object_paths(_fixture("locked_file_exit1.jsonl"), 10)
        assert paths == ["locked"]

    def test_recovered_retry_has_no_final_attempt_failures(self):
        # Attempt 1 failed on "locked"; attempt 2/3 succeeded, so nothing failed last.
        assert cloud_sync.failed_object_paths(_fixture("recovered_retry_exit0.jsonl"), 10) == []

    def test_earlier_attempt_failures_are_excluded(self):
        text = _json_lines(
            {"level": "error", "msg": "Failed to copy: read old", "object": "old"},
            {"level": "error", "msg": "Attempt 1/2 failed with 1 errors and: read old",
             "source": "cmd/cmd.go:283"},
            {"level": "error", "msg": "Failed to copy: read new", "object": "new"},
            {"level": "error", "msg": "Attempt 2/2 failed with 1 errors and: read new",
             "source": "cmd/cmd.go:283"},
        )
        assert cloud_sync.failed_object_paths(text, 10) == ["new"]

    def test_capped_at_limit(self):
        text = _json_lines(*[
            {"level": "error", "msg": f"Failed to copy: boom {i}", "object": f"f{i}"}
            for i in range(5)
        ])
        paths = cloud_sync.failed_object_paths(text, 2)
        assert len(paths) == 2
        assert set(paths) <= {f"f{i}" for i in range(5)}


# ── P4b deletes held marker ─────────────────────────────────────────

class TestDeletesWereHeld:
    def test_locked_fixture_holds_deletes(self):
        assert cloud_sync.deletes_were_held(_fixture("locked_file_exit1.jsonl")) is True

    def test_clean_fixture_does_not_hold_deletes(self):
        assert cloud_sync.deletes_were_held(_fixture("lan_clean.jsonl")) is False

    def test_exact_rclone_message_detected(self):
        text = _json_lines({"level": "error", "msg": "not deleting files as there were IO errors"})
        assert cloud_sync.deletes_were_held(text) is True


# ── P5 rclone_deletes_of ────────────────────────────────────────────

class TestP5RcloneDeletesOf:
    def test_stats_deletes_wins(self):
        result = {"stats": {"deletes": 2}, "max_delete_reached": False}
        assert cloud_sync.rclone_deletes_of(result, 5) == 2

    def test_stats_zero_deletes_is_zero_not_cap(self):
        result = {"stats": {"deletes": 0}, "max_delete_reached": True}
        assert cloud_sync.rclone_deletes_of(result, 1) == 0

    def test_backstop_stop_without_stats_counts_the_cap(self):
        result = {"stats": None, "max_delete_reached": True}
        assert cloud_sync.rclone_deletes_of(result, 3) == 3

    def test_backstop_stop_with_stats_missing_deletes_counts_the_cap(self):
        result = {"stats": {}, "max_delete_reached": True}
        assert cloud_sync.rclone_deletes_of(result, 4) == 4

    def test_no_stats_and_no_backstop_is_none(self):
        assert cloud_sync.rclone_deletes_of({"stats": None, "max_delete_reached": False}, 3) is None

    def test_missing_backstop_key_and_stats_is_none(self):
        assert cloud_sync.rclone_deletes_of({"stats": None}, 3) is None


# ── P6 rename fixture: existing planned-delete and move counters ────

class TestP6RenameFixturePlan:
    def test_rename_dry_run_has_no_planned_deletes(self):
        assert cloud_sync.count_planned_deletes(_fixture("lan_rename_dryrun.jsonl")) == 0

    def test_rename_dry_run_has_five_planned_moves(self):
        assert cloud_sync.count_planned_moves(_fixture("lan_rename_dryrun.jsonl")) == 5


# ── P7 --backup-dir dry-run counters (plan P1 review R4, M3; matrix §4) ──
# lan_backupdir_dryrun.jsonl: real rclone v1.74.3 dry-run with --track-renames
# --backup-dir. Deletions are "move into backup dir", renames "move to <new>",
# a replaced file a bare "move" (X4). Expected from matrix §4: 2 deletes, 3 moves.

class TestP7BackupDirDryRunCounts:
    def test_backup_dir_deletions_count_as_deletes_and_renames_as_moves(self):
        text = _fixture("lan_backupdir_dryrun.jsonl")
        assert cloud_sync.count_planned_deletes(text) == 2
        assert cloud_sync.count_planned_moves(text) == 3

    def test_backup_dir_dry_run_has_one_planned_copy(self):
        assert cloud_sync.planned_copy_count(_fixture("lan_backupdir_dryrun.jsonl")) == 1


class TestP7bNoRegressionWithoutBackupDir:
    def test_mixed_dry_run_has_two_deletes_and_five_moves(self):
        text = _fixture("lan_mixed_dryrun.jsonl")
        assert cloud_sync.count_planned_deletes(text) == 2
        assert cloud_sync.count_planned_moves(text) == 5

    def test_rename_dry_run_has_no_deletes_and_five_moves(self):
        text = _fixture("lan_rename_dryrun.jsonl")
        assert cloud_sync.count_planned_deletes(text) == 0
        assert cloud_sync.count_planned_moves(text) == 5


class TestP7cBareMoveIsNeitherDeleteNorMove:
    def test_replaced_file_entry_is_not_counted(self):
        text = _json_lines({"level": "notice", "msg": "Skipped move as --dry-run is set",
                            "skipped": "move", "object": "change.txt", "size": 3})
        assert cloud_sync.count_planned_deletes(text) == 0
        assert cloud_sync.count_planned_moves(text) == 0
