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
import time
from pathlib import Path

import pendulum
import pytest
from pydantic import ValidationError

import core.config_help as config_help
import core.lan_sync as lan_sync
from core.run_artefacts import RunArtefacts
from models.config import LanConfig
from test_support.fake_tools import fake_rclone_lan

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


# ── R8b: check_lan_transfers_hash (matrix A11, A12, A12b, A12c, A13 wrapper) ──
#
# Fixtures lan_hash_clean.* and lan_hash_md5diff.* are real rclone v1.74.3 output of
# `rclone check SRC DST --one-way --files-from-raw LIST --skip-links --ignore-case --checkers 4`
# (local disk; see LAN_FIXTURES.md). The clean list is a.txt, sub/b.txt, new.txt (3 matching);
# the md5diff run had new.txt overwritten with same-size different bytes (X8).

HASH_RUN_ID = "3f2a9c1e-77aa-4c6b-9d11-0a1b2c3d4e5f-lan"
HASH_STARTED = pendulum.datetime(2026, 11, 25, 9, 0, tz="Asia/Kolkata")
HASH_PATHS = ["a.txt", "sub/b.txt", "new.txt"]
A11_PATHS = ["#hash.txt", " lead.txt", "sub/é.txt"]
CLEAN_HASH_ERR = _fixture("lan_hash_clean.stderr.txt")
CLEAN_HASH_COMB = _fixture("lan_hash_clean_combined.txt")
MD5DIFF_ERR = _fixture("lan_hash_md5diff.stderr.txt")
MD5DIFF_COMB = _fixture("lan_hash_md5diff_combined.txt")
ARRIVAL_KEYS = {"verified", "reason", "matching", "missing", "differ",
                "changed_during_backup", "unverified_paths"}


def _hash_setup(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    dst = str(tmp_path / "dst")
    artefacts = RunArtefacts(tmp_path / "runlogs", HASH_RUN_ID, started=HASH_STARTED)
    return src, dst, str(artefacts.dir)


def _arg(argv, flag):
    return Path(argv[argv.index(flag) + 1])


def _hash_check(config, src, dst, art, spec, *, paths, epoch, timeout=900):
    with fake_rclone_lan({"rclone-lan-hash": spec}) as fake:
        result = lan_sync.check_lan_transfers_hash(config, str(src), dst, art, paths=paths,
                                                   changed_after_epoch=epoch, timeout=timeout)
    return result, fake


# A11 argv and list file

def test_a11_hash_argv_is_the_plan_2_1_hash_argv(tmp_path):
    src, dst, art = _hash_setup(tmp_path)
    config = LanConfig()
    spec = {"exit_code": 0, "stderr": CLEAN_HASH_ERR, "combined": CLEAN_HASH_COMB}
    _result, fake = _hash_check(config, src, dst, art, spec, paths=A11_PATHS,
                                epoch=time.time() + 100, timeout=900)
    assert fake.steps() == ["rclone-lan-hash"]
    argv = fake[0]["argv"]
    listing = _arg(argv, "--files-from-raw")
    combined = _arg(argv, "--combined")
    assert argv[1:] == [
        "check", str(src), dst,
        "--one-way", "--files-from-raw", str(listing),
        "--skip-links", "--ignore-case",
        "--checkers", str(config.checkers),
        "--combined", str(combined),
    ]
    assert fake[0]["timeout_s"] == 900


def test_a11_list_file_is_utf8_bytes_in_the_run_folder(tmp_path):
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": 0, "stderr": CLEAN_HASH_ERR, "combined": CLEAN_HASH_COMB}
    _result, fake = _hash_check(LanConfig(), src, dst, art, spec, paths=A11_PATHS,
                                epoch=time.time() + 100)
    listing = _arg(fake[0]["argv"], "--files-from-raw")
    assert listing.parent == Path(art)
    assert "rclone-lan-hash-files" in listing.name
    assert listing.read_bytes() == "#hash.txt\n lead.txt\nsub/é.txt\n".encode()


@pytest.mark.parametrize("flag", [
    "--size-only", "--exclude", "--files-from", "--download", "--checksum",
    "--log-file", "--use-json-log",
])
def test_a11_hash_argv_never_has_the_forbidden_flags(tmp_path, flag):
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": 0, "stderr": CLEAN_HASH_ERR, "combined": CLEAN_HASH_COMB}
    _result, fake = _hash_check(LanConfig(), src, dst, art, spec, paths=A11_PATHS,
                                epoch=time.time() + 100)
    assert flag not in fake[0]["argv"]


def test_a11_checkers_come_from_config(tmp_path):
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": 0, "stderr": CLEAN_HASH_ERR, "combined": CLEAN_HASH_COMB}
    _result, fake = _hash_check(LanConfig(checkers=7), src, dst, art, spec, paths=A11_PATHS,
                                epoch=time.time() + 100)
    argv = fake[0]["argv"]
    assert argv[argv.index("--checkers") + 1] == "7"


# A12 mismatch, A12b explained by a change, A12c clean

def test_a12_same_size_mismatch_is_not_verified_and_listed(tmp_path):
    src, dst, art = _hash_setup(tmp_path)
    (src / "new.txt").write_bytes(b"N" * 300)
    spec = {"exit_code": 1, "stderr": MD5DIFF_ERR, "combined": MD5DIFF_COMB}
    result, _fake = _hash_check(LanConfig(), src, dst, art, spec, paths=HASH_PATHS,
                                epoch=time.time() + 100)
    assert result["verified"] is False
    assert result["unverified_paths"] == ["new.txt"]


def test_a12b_mismatch_explained_by_a_change_after_the_epoch_is_verified(tmp_path):
    src, dst, art = _hash_setup(tmp_path)
    (src / "new.txt").write_bytes(b"N" * 300)  # created now: changed after an epoch 100 s ago
    spec = {"exit_code": 1, "stderr": MD5DIFF_ERR, "combined": MD5DIFF_COMB}
    result, _fake = _hash_check(LanConfig(), src, dst, art, spec, paths=HASH_PATHS,
                                epoch=time.time() - 100)
    assert result["verified"] is True
    assert result["changed_during_backup"] == 1
    assert result["unverified_paths"] == []


def test_a12c_clean_hash_is_verified_and_matching_is_the_list_length(tmp_path):
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": 0, "stderr": CLEAN_HASH_ERR, "combined": CLEAN_HASH_COMB}
    result, _fake = _hash_check(LanConfig(), src, dst, art, spec, paths=HASH_PATHS,
                                epoch=time.time() + 100)
    assert result["verified"] is True
    assert result["matching"] == len(HASH_PATHS)


# A13 wrapper part: timeout, spawn error, result shape

def test_a13_hash_timeout_is_not_verified_with_a_timed_out_reason(tmp_path):
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": None, "timed_out": True}
    result, _fake = _hash_check(LanConfig(), src, dst, art, spec, paths=HASH_PATHS,
                                epoch=time.time() + 100, timeout=900)
    assert result["verified"] is False
    assert "timed out" in result["reason"]


def test_a13_hash_spawn_error_is_not_verified(tmp_path):
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": None, "spawn_error": "FileNotFoundError: missing"}
    result, _fake = _hash_check(LanConfig(), src, dst, art, spec, paths=HASH_PATHS,
                                epoch=time.time() + 100, timeout=900)
    assert result["verified"] is False
    assert result["reason"]


def test_a13_timeout_result_has_the_arrival_dict_shape(tmp_path):
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": None, "timed_out": True}
    result, _fake = _hash_check(LanConfig(), src, dst, art, spec, paths=HASH_PATHS,
                                epoch=time.time() + 100, timeout=900)
    assert set(result) >= ARRIVAL_KEYS


# A14 (R8c, Opus ruling) the list file cannot be written: the hash check must not raise

def test_a14_list_file_write_error_returns_not_verified_and_never_raises(tmp_path):
    from unittest.mock import patch
    src, dst, art = _hash_setup(tmp_path)
    real_write = Path.write_bytes

    def failing_write(self, data):
        if "rclone-lan-hash-files" in self.name:
            raise OSError(28, "No space left on device")
        return real_write(self, data)

    spec = {"exit_code": 0, "stderr": CLEAN_HASH_ERR, "combined": CLEAN_HASH_COMB}
    with fake_rclone_lan({"rclone-lan-hash": spec}) as fake, \
            patch.object(Path, "write_bytes", failing_write):
        result = lan_sync.check_lan_transfers_hash(
            LanConfig(), str(src), dst, art, paths=HASH_PATHS,
            changed_after_epoch=time.time() + 100, timeout=900)
    assert fake.steps() == [], "no rclone call without a list file"
    assert result["verified"] is False
    assert result["reason"].startswith("hash check could not start:")
    assert set(result) >= ARRIVAL_KEYS


# ── R11 (Opus rulings 2026-11-25): verified only when every listed path is accounted for ──
#
# Rule: verified only if matching is not None and matching + missing + differ == len(listed).
# A listed path that no longer exists on the source counts as changed during the backup.
# Fixture lan_hash_silent.* is real rclone v1.74.3 output for two listed paths the source does
# not have: rclone prints "0 differences found" and NO "N matching files" line (fact X8).
# Lines like "matching 1 of 2 listed" are synthetic (no real rclone output has that wording).

HASH_SILENT_ERR = _fixture("lan_hash_silent.stderr.txt")
HASH_SILENT_COMB = _fixture("lan_hash_silent_combined.txt")
TWO_GONE = ["gone_one.txt", "gone_two.txt"]
HASH12_PATHS = CLEAN_NAMES   # the twelve files of lan_clean (lan_hash12_* were checked on them)


def test_a12d_listed_files_rclone_silently_skipped_are_not_verified(tmp_path):
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": 0, "stderr": HASH_SILENT_ERR, "combined": HASH_SILENT_COMB}
    result, _fake = _hash_check(LanConfig(), src, dst, art, spec, paths=TWO_GONE,
                                epoch=time.time() + 100)
    assert result["verified"] is False
    assert "covered 0 of 2" in result["reason"]


def test_a12e_partial_count_line_does_not_verify(tmp_path):
    # synthetic: the count names 1 of the 2 listed files, the rest are unaccounted for
    stderr = ("2026/11/25 12:00:00 NOTICE: Local file system at X:/rig/dst: 0 differences found\n"
              "2026/11/25 12:00:00 NOTICE: Local file system at X:/rig/dst: matching 1 of 2 listed\n")
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": 0, "stderr": stderr, "combined": ""}
    result, _fake = _hash_check(LanConfig(), src, dst, art, spec, paths=["a.txt", "sub/b.txt"],
                                epoch=time.time() + 100)
    assert result["verified"] is False
    assert result["reason"]


def test_a12f_count_short_of_the_listed_paths_does_not_verify(tmp_path):
    # real clean output for 12 files, but 13 paths were listed: one is unaccounted for
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": 0, "stderr": _fixture("lan_hash12_clean.stderr.txt"),
            "combined": _fixture("lan_hash12_clean_combined.txt")}
    result, _fake = _hash_check(LanConfig(), src, dst, art, spec,
                                paths=HASH12_PATHS + ["extra.txt"], epoch=time.time() + 100)
    assert result["verified"] is False


def test_a12g_listed_path_gone_from_the_source_counts_as_changed_during_backup(tmp_path):
    # the Opus ruling says the gone path counts as changed; whether verified follows from that
    # is not in the matrix, so only the count is asserted here (flagged for Opus)
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": 0, "stderr": HASH_SILENT_ERR, "combined": HASH_SILENT_COMB}
    result, _fake = _hash_check(LanConfig(), src, dst, art, spec, paths=["gone_one.txt"],
                                epoch=time.time() + 100)
    assert result["changed_during_backup"] == 1
    assert "gone_one.txt" not in result["unverified_paths"]


def test_a12c_clean_hash_of_twelve_listed_files_is_verified(tmp_path):
    src, dst, art = _hash_setup(tmp_path)
    spec = {"exit_code": 0, "stderr": _fixture("lan_hash12_clean.stderr.txt"),
            "combined": _fixture("lan_hash12_clean_combined.txt")}
    result, _fake = _hash_check(LanConfig(), src, dst, art, spec, paths=HASH12_PATHS,
                                epoch=time.time() + 100)
    assert result["verified"] is True
    assert result["matching"] == len(HASH12_PATHS) == 12


# ── H7: the copy messages of rclone that are transfers ───────────────────
#
# Real fixtures lan_multithread_new/replaced.jsonl (rclone v1.74.3, --multi-thread-cutoff 1,
# local source -> loopback admin share): small.txt is "Copied (new)" and big.bin is
# "Multi-thread Copied (new)" in the same run. "Copied (Rcat, new)" and "Copied (server-side
# copy)" cannot come from a local -> UNC copy, so their entries are synthetic (flagged).

@pytest.mark.parametrize("msg", [
    "Multi-thread Copied (new)",
    "Multi-thread Copied (replaced existing)",
    "Copied (Rcat, new)",
    "Copied (server-side copy)",
])
def test_h7_copy_message_variants_are_listed(msg):
    log = _log(_entry(msg, "x.bin", size=5))
    assert lan_sync.transferred_files([log]) == (["x.bin"], 5)


def test_h7_real_multithread_new_run_lists_both_files():
    paths, total = lan_sync.transferred_files([_fixture("lan_multithread_new.jsonl")])
    assert paths == ["small.txt", "big.bin"]
    assert total == 40 + 2097152


def test_h7_real_multithread_replaced_run_lists_both_files():
    paths, total = lan_sync.transferred_files([_fixture("lan_multithread_replaced.jsonl")])
    assert paths == ["small.txt", "big.bin"]
    assert total == 55 + 2097169


def test_h7_multithread_error_entry_is_not_listed():
    log = _log(_entry("Multi-thread Copied (new)", "bad.bin", size=5, level="error"))
    assert lan_sync.transferred_files([log]) == ([], 0)
