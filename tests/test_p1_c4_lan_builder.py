"""Plan P1 chunk C4: LAN rclone command builder (core/lan_sync.py).

Matrix rows (docs/PLAN_P1_RCLONE_LAN_2026-11-25.md, "M builder (U)" and §2.1):
  M1 exact flag set of a real sync.
  M2 the never-list is never present, for any config variation.
  M3 dry-run argv = sync argv + --dry-run/--combined, minus --max-delete,
     --max-duration and --cutoff-mode.
  M4 an empty source raises ValueError on the real build only.
  M5 --max-delete 0 is emitted.

Config keys read: lan.transfers, lan.checkers, lan.modify_window_seconds (new,
so LanConfig(...) with them fails until C4 lands), lan.retry_count,
lan.retry_wait_seconds, lan.track_renames.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import core.lan_sync as lan_sync
from core.permissions import PROBE_PATTERN
from models.config import LanConfig

SRC_NAME = "src"
DEST = r"\\127.0.0.1\C$\rig\dst"
LOG = r"C:\runtime\run_logs\lan\sync.log"
COMBINED = r"C:\runtime\run_logs\lan\dryrun.combined.txt"

EXPECTED_EXCLUDES = [
    ".AAM_TARGET_MOUNTED",
    PROBE_PATTERN,
    "System Volume Information/**",
    "$RECYCLE.BIN/**",
]

NEVER_FLAGS = (
    "--ignore-errors",
    "--size-only",
    "--checksum",
    "--ignore-checksum",
    "--local-no-check-updated",
    "--local-nounc",
    "--error-on-no-transfer",
    "--delete-excluded",
    "--immutable",
    "--ignore-existing",
    "--config",
)

BUDGET_FLAGS = ("--max-delete", "--max-duration", "--cutoff-mode")


@pytest.fixture(autouse=True)
def _pin_rclone_binary():
    """Builder resolves the binary through core.process.resolve_binary."""
    with patch("core.lan_sync.resolve_binary", create=True, return_value="rclone"):
        yield


@pytest.fixture
def source(tmp_path: Path) -> str:
    """A real source with one file (G1 passes)."""
    src = tmp_path / SRC_NAME
    src.mkdir()
    (src / "a.txt").write_text("x", encoding="utf-8")
    return str(src)


def _build(config, source, *, dry_run=False, max_delete=5, **kwargs):
    if dry_run:
        kwargs.setdefault("combined_path", COMBINED)
    return lan_sync.build_lan_rclone_command(
        config, source, DEST, LOG, max_delete=max_delete, dry_run=dry_run, **kwargs)


def _pairs(argv: list[str]) -> list[tuple[str, str | None]]:
    """Split argv after `rclone sync SRC DST` into (flag, value-or-None) pairs.

    Every flag starts with "--"; a value is the next token when it does not
    start with "--" (exclude patterns such as "$RECYCLE.BIN/**" qualify).
    """
    args = argv[4:]
    pairs: list[tuple[str, str | None]] = []
    i = 0
    while i < len(args):
        token = args[i]
        assert token.startswith("--"), f"stray token in argv: {token!r}"
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            pairs.append((token, args[i + 1]))
            i += 2
        else:
            pairs.append((token, None))
            i += 1
    return pairs


def _tokens(argv: list[str]) -> set[str]:
    return set(argv)


CONFIG_VARIANTS = [
    pytest.param(dict(), id="defaults"),
    pytest.param(dict(track_renames=False), id="no-track-renames"),
    pytest.param(dict(transfers=1, checkers=1), id="min-concurrency"),
    pytest.param(dict(transfers=32, checkers=32, modify_window_seconds=10), id="max-concurrency"),
    pytest.param(dict(retry_count=10, retry_wait_seconds=300, track_renames=False), id="max-retries"),
    pytest.param(dict(modify_window_seconds=0), id="zero-modify-window"),
]


# ── M1 exact flag set ───────────────────────────────────────────────

class TestM1ExactFlagSet:
    def test_real_sync_flag_set_matches_plan_2_1(self, source):
        argv = _build(LanConfig(), source, max_delete=7)
        expected = [
            ("--check-first", None),
            ("--create-empty-src-dirs", None),
            ("--skip-links", None),
            ("--ignore-case", None),
            ("--modify-window", "2s"),
            ("--transfers", "4"),
            ("--checkers", "4"),
            ("--retries", "3"),
            ("--retries-sleep", "10s"),
            ("--use-json-log", None),
            ("--log-level", "INFO"),
            ("--log-file", LOG),
            ("--exclude", ".AAM_TARGET_MOUNTED"),
            ("--exclude", PROBE_PATTERN),
            ("--exclude", "System Volume Information/**"),
            ("--exclude", "$RECYCLE.BIN/**"),
            ("--track-renames", None),
            ("--max-delete", "7"),
        ]
        key = lambda pairs: sorted((flag, value or "") for flag, value in pairs)
        assert key(_pairs(argv)) == key(expected)

    def test_positional_args_are_binary_sync_source_dest(self, source):
        argv = _build(LanConfig(), source)
        assert argv[:4] == ["rclone", "sync", source, DEST]

    def test_track_renames_false_removes_only_that_flag(self, source):
        on = _tokens(_build(LanConfig(), source))
        off = _tokens(_build(LanConfig(track_renames=False), source))
        assert "--track-renames" in on
        assert "--track-renames" not in off
        assert on - off == {"--track-renames"}

    def test_configured_values_reach_the_flags(self, source):
        config = LanConfig(transfers=9, checkers=11, modify_window_seconds=3,
                           retry_count=5, retry_wait_seconds=42)
        pairs = dict(p for p in _pairs(_build(config, source)) if p[0] != "--exclude")
        assert pairs["--transfers"] == "9"
        assert pairs["--checkers"] == "11"
        assert pairs["--modify-window"] == "3s"
        assert pairs["--retries"] == "5"
        assert pairs["--retries-sleep"] == "42s"

    def test_excludes_constant_is_the_four_patterns(self):
        assert list(lan_sync.LAN_EXCLUDES) == EXPECTED_EXCLUDES

    def test_real_sync_has_the_four_excludes(self, source):
        excludes = [v for f, v in _pairs(_build(LanConfig(), source)) if f == "--exclude"]
        assert excludes == EXPECTED_EXCLUDES

    def test_budget_limit_adds_max_duration_with_soft_cutoff(self, source):
        pairs = _pairs(_build(LanConfig(), source, max_duration_s=600))
        assert ("--max-duration", "600s") in pairs
        assert ("--cutoff-mode", "SOFT") in pairs

    def test_no_budget_means_no_max_duration(self, source):
        tokens = _tokens(_build(LanConfig(), source))
        assert "--max-duration" not in tokens
        assert "--cutoff-mode" not in tokens


# ── M2 never-list ───────────────────────────────────────────────────

class TestM2NeverList:
    @pytest.mark.parametrize("overrides", CONFIG_VARIANTS)
    def test_real_sync_never_passes_forbidden_flags(self, source, overrides):
        argv = _build(LanConfig(**overrides), source, max_delete=3, max_duration_s=900)
        for flag in NEVER_FLAGS:
            assert flag not in argv, f"{flag} must never be passed"

    @pytest.mark.parametrize("overrides", CONFIG_VARIANTS)
    def test_dry_run_never_passes_forbidden_flags(self, source, overrides):
        argv = _build(LanConfig(**overrides), source, dry_run=True)
        for flag in NEVER_FLAGS:
            assert flag not in argv, f"{flag} must never be passed"


# ── M3 dry-run = sync + --dry-run ───────────────────────────────────

class TestM3DryRunInvariant:
    @pytest.mark.parametrize("overrides", CONFIG_VARIANTS)
    def test_dry_run_argv_is_sync_argv_plus_dry_run_and_combined(self, source, overrides):
        config = LanConfig(**overrides)
        sync_argv = _build(config, source, max_delete=4, max_duration_s=600)
        dry_argv = _build(config, source, dry_run=True, max_delete=4, max_duration_s=600)

        # Strip the budget flags from the sync argv (same order preserved).
        stripped = [(flag, value) for flag, value in _pairs(sync_argv)
                    if flag not in BUDGET_FLAGS]

        dry_pairs = _pairs(dry_argv)
        assert ("--dry-run", None) in dry_pairs
        assert ("--combined", COMBINED) in dry_pairs
        without_dry = [p for p in dry_pairs if p not in (("--dry-run", None), ("--combined", COMBINED))]
        assert without_dry == stripped
        assert dry_argv[:4] == sync_argv[:4]

    def test_dry_run_has_no_budget_flags(self, source):
        argv = _build(LanConfig(), source, dry_run=True, max_delete=4, max_duration_s=600)
        for flag in BUDGET_FLAGS:
            assert flag not in argv


# ── M4 empty source ─────────────────────────────────────────────────

class TestM4EmptySource:
    def test_real_build_raises_on_empty_source(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ValueError):
            _build(LanConfig(), str(empty))

    def test_dry_run_build_does_not_raise_on_empty_source(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        argv = _build(LanConfig(), str(empty), dry_run=True)
        assert "--dry-run" in argv


# ── M5 --max-delete 0 ───────────────────────────────────────────────

class TestM5MaxDeleteZero:
    def test_max_delete_zero_is_emitted(self, source):
        argv = _build(LanConfig(), source, max_delete=0)
        idx = argv.index("--max-delete")
        assert argv[idx + 1] == "0"

    def test_dry_run_never_carries_max_delete(self, source):
        argv = _build(LanConfig(), source, dry_run=True, max_delete=0)
        assert "--max-delete" not in argv
