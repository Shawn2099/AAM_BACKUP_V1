"""Plan P1 review chunk R8a: S3 keys and the transferred-files parser.

Matrix rows (docs/TESTMATRIX_P1_REVIEW_2026-11-25.md, section 8):
  K5  new keys: defaults, bounds, config_help entries.
  H1  Copied entries of a clean sync: all listed in log order, bytes = stats.bytes.
  H2  replaced file (`Copied (replaced existing)`) is listed with its new size.
  H3  dry-runs, renames and deletes are not transfers: ([], 0).
  H4  two attempt logs: de-duplicated, first-seen order, last seen size wins.
  H5  a Copied entry without an int size makes bytes None.
  H6  error entries (locked file) are not listed.

Also covered from the rule text of section 8 (no matrix row, not guessed):
  entries must have level "info" and a non-empty str object.

Fixtures are real rclone v1.74.3 outputs (test_support/rclone_logs). lan_replaced.jsonl
is generated for this chunk (see LAN_FIXTURES.md).

The parser is looked up on the module at call time, so the parser tests fail (with
AttributeError) while lan_sync.transferred_files is missing, and the key tests fail
while the LanConfig fields and config_help entries are missing.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import core.config_help as config_help
import core.lan_sync as lan_sync
from models.config import LanConfig

FIXTURES = Path(__file__).resolve().parents[1] / "test_support" / "rclone_logs"

CLEAN_NAMES = [
    "Ünïcode café.txt",
    "name with spaces & (parens).txt",
    "README note.txt",
    "a.txt",
    "中文 文件.txt",
    "sub/b.bin",
    "photos/p1.jpg",
    "photos/p2.jpg",
    "photos/p3.jpg",
    "photos/p4.jpg",
    "sub/deep/c.txt",
    "photos/p5.jpg",
]


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _entry(msg: str, obj, size=None, level: str = "info") -> dict:
    entry = {"level": level, "msg": msg, "objectType": "*local.Object"}
    if obj is not None:
        entry["object"] = obj
    if size is not None:
        entry["size"] = size
    return entry


def _log(*entries: dict) -> str:
    return "\n".join(json.dumps(e) for e in entries)


# ── K5 new keys ─────────────────────────────────────────────────────

class TestK5NewKeys:
    def test_defaults(self):
        assert "arrival_hash_transferred" in LanConfig.model_fields
        assert "arrival_hash_max_gb" in LanConfig.model_fields
        assert "arrival_hash_timeout_seconds" in LanConfig.model_fields
        cfg = LanConfig()
        assert cfg.arrival_hash_transferred is True
        assert cfg.arrival_hash_max_gb == 50
        assert cfg.arrival_hash_timeout_seconds == 3600

    @pytest.mark.parametrize("value", [0, 10000])
    def test_max_gb_bounds_accepted(self, value):
        assert "arrival_hash_max_gb" in LanConfig.model_fields
        assert LanConfig(arrival_hash_max_gb=value).arrival_hash_max_gb == value

    @pytest.mark.parametrize("value", [-1, 10001])
    def test_max_gb_out_of_bounds_rejected(self, value):
        assert "arrival_hash_max_gb" in LanConfig.model_fields
        with pytest.raises(ValidationError):
            LanConfig(arrival_hash_max_gb=value)

    @pytest.mark.parametrize("value", [60, 14400])
    def test_timeout_bounds_accepted(self, value):
        assert "arrival_hash_timeout_seconds" in LanConfig.model_fields
        assert LanConfig(arrival_hash_timeout_seconds=value).arrival_hash_timeout_seconds == value

    @pytest.mark.parametrize("value", [59, 14401])
    def test_timeout_out_of_bounds_rejected(self, value):
        assert "arrival_hash_timeout_seconds" in LanConfig.model_fields
        with pytest.raises(ValidationError):
            LanConfig(arrival_hash_timeout_seconds=value)

    @pytest.mark.parametrize("key", [
        "lan.arrival_hash_transferred",
        "lan.arrival_hash_max_gb",
        "lan.arrival_hash_timeout_seconds",
    ])
    def test_config_help_entry(self, key):
        assert key in config_help.HELP
        label, text = config_help.HELP[key]
        assert label.strip()
        assert isinstance(text, str)


# ── H parser: transferred_files ─────────────────────────────────────

class TestH1CleanSync:
    def test_all_new_copies_in_log_order_with_bytes(self):
        paths, total = lan_sync.transferred_files([_fixture("lan_clean.jsonl")])
        assert paths == CLEAN_NAMES
        assert total == 13062


class TestH2ReplacedFile:
    def test_replaced_existing_is_listed_with_new_size(self):
        paths, total = lan_sync.transferred_files([_fixture("lan_replaced.jsonl")])
        assert paths == ["a.txt"]
        assert total == 150


class TestH3NotTransfers:
    @pytest.mark.parametrize("name", [
        "lan_rename.jsonl",
        "lan_deletes.jsonl",
        "lan_clean_dryrun.jsonl",
        "lan_rename_dryrun.jsonl",
        "lan_deletes_dryrun.jsonl",
        "lan_mixed_dryrun.jsonl",
        "lan_backupdir_dryrun.jsonl",
    ])
    def test_no_transfers_in_rename_delete_and_dryrun_logs(self, name):
        assert lan_sync.transferred_files([_fixture(name)]) == ([], 0)


class TestH4TwoAttempts:
    def test_dedup_first_seen_order_and_last_size(self):
        attempt1 = _log(
            _entry("Copied (new)", "x.txt", size=5),
            _entry("Copied (new)", "p1.txt", size=3),
        )
        attempt2 = _log(
            _entry("Copied (new)", "x.txt", size=7),
            _entry("Copied (new)", "p2.txt", size=4),
        )
        paths, total = lan_sync.transferred_files([attempt1, attempt2])
        assert paths == ["x.txt", "p1.txt", "p2.txt"]
        assert total == 7 + 3 + 4


class TestH5MissingSize:
    def test_copy_without_size_gives_none_bytes(self):
        log = _log(
            _entry("Copied (new)", "x.txt", size=5),
            _entry("Copied (new)", "nosize.txt", size=None),
        )
        _paths, total = lan_sync.transferred_files([log])
        assert total is None


class TestH6ErrorEntries:
    def test_failed_locked_file_is_not_listed(self):
        paths, _total = lan_sync.transferred_files([_fixture("locked_file_exit1.jsonl")])
        assert "locked" not in paths


class TestRuleClauses:
    """Clauses of the section 8 rule with no matrix row of their own."""

    def test_error_level_copy_is_not_listed(self):
        log = _log(_entry("Copied (new)", "bad.txt", size=5, level="error"))
        assert lan_sync.transferred_files([log]) == ([], 0)

    def test_copy_without_object_is_not_listed(self):
        log = _log(_entry("Copied (new)", None, size=5))
        assert lan_sync.transferred_files([log]) == ([], 0)
