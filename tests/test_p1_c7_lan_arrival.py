"""Plan P1 chunk C7: the LAN arrival check (core/lan_sync.py).

Matrix rows (docs/PLAN_P1_RCLONE_LAN_2026-11-25.md, "A arrival (U)" and §2.3):
  A1 clean check verified, matching parsed from "N matching files".
  A2 a '+' file changed during the backup (ctime/mtime >= sync start) verifies and is counted
     in changed_during_backup.
  A3 the same file, not changed during the backup, is not verified and its path is listed.
  A4 an old '*' file is not verified.
  A5 no completion summary is not verified.
  A6 '!' lines or exit >= 2 are not verified.
  A7 a count mismatch between the summary and the combined file is not verified.
  A8 the check argv (§2.1).
  A9 a timeout is not verified.

Plus the changed_since rule of check_lan_arrival (source mtime/ctime against
changed_after_epoch minus lan.modify_window_seconds; OSError counts as not changed).
Times are set in the future (changed_after_epoch = now + 100 s) so that the files created by
the test (whose creation time on Windows is now) are never "changed" by accident.

Real fixtures (test_support/rclone_logs): lan_check_clean.stderr.txt / _combined.txt (exit 0),
lan_check_missing.stderr.txt / _combined.txt (exit 1, one '+'), lan_check_sizediff.stderr.txt /
_combined.txt (exit 1, one '*').

The functions are looked up on the module at call time, so each test fails with
AttributeError until C7 lands.
"""

import os
import time
from pathlib import Path

import pendulum
import pytest

import core.lan_sync as lan_sync
from core.permissions import PROBE_PATTERN
from core.run_artefacts import RunArtefacts
from models.config import LanConfig
from test_support.fake_tools import fake_rclone_lan

FIXTURES = Path(__file__).resolve().parents[1] / "test_support" / "rclone_logs"
RUN_ID = "3f2a9c1e-77aa-4c6b-9d11-0a1b2c3d4e5f-lan"
STARTED = pendulum.datetime(2026, 11, 25, 9, 0, tz="Asia/Kolkata")
NOTICE = 'NOTICE: Config file "X:/rclone.conf" not found - using defaults\n'
MISSING_PATH = "sub/deep/c.txt"
SIZEDIFF_PATH = "photos_renamed/p1.jpg"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


CLEAN_ERR = _fixture("lan_check_clean.stderr.txt")
CLEAN_COMB = _fixture("lan_check_clean_combined.txt")
MISSING_ERR = _fixture("lan_check_missing.stderr.txt")
MISSING_COMB = _fixture("lan_check_missing_combined.txt")
SIZEDIFF_ERR = _fixture("lan_check_sizediff.stderr.txt")
SIZEDIFF_COMB = _fixture("lan_check_sizediff_combined.txt")


def _none_changed(path):
    return False


def _changed(*paths):
    return lambda p: p in paths


def _all_changed(path):
    return True


def _setup(tmp_path):
    src = tmp_path / "src"
    (src / "sub" / "deep").mkdir(parents=True)
    dst = str(tmp_path / "dst")
    artefacts = RunArtefacts(tmp_path / "runlogs", RUN_ID, started=STARTED)
    return src, dst, str(artefacts.dir)


def _flag(argv, flag):
    return Path(argv[argv.index(flag) + 1])


# ── decide_lan_arrival (pure) ───────────────────────────────────────

def test_a1_clean_check_is_verified_and_matching_is_parsed():
    r = lan_sync.decide_lan_arrival(0, CLEAN_ERR, CLEAN_COMB, _none_changed)
    assert r["verified"] is True
    assert r["reason"] is None
    assert r["matching"] == 9
    assert r["missing"] == 0
    assert r["differ"] == 0
    assert r["changed_during_backup"] == 0
    assert r["unverified_paths"] == []


def test_a1_matching_is_none_when_the_summary_has_no_matching_line():
    stderr = NOTICE + "NOTICE: Local file system at X:/rig/dst: 0 differences found\n"
    r = lan_sync.decide_lan_arrival(0, stderr, CLEAN_COMB, _none_changed)
    assert r["matching"] is None


def test_a2_missing_file_changed_during_the_backup_is_verified_and_counted():
    r = lan_sync.decide_lan_arrival(1, MISSING_ERR, MISSING_COMB, _changed(MISSING_PATH))
    assert r["verified"] is True
    assert r["missing"] == 1
    assert r["changed_during_backup"] == 1
    assert r["matching"] == 8
    assert r["unverified_paths"] == []


def test_a3_missing_file_not_changed_during_the_backup_is_not_verified_and_listed():
    r = lan_sync.decide_lan_arrival(1, MISSING_ERR, MISSING_COMB, _none_changed)
    assert r["verified"] is False
    assert r["reason"]
    assert r["missing"] == 1
    assert r["changed_during_backup"] == 0
    assert r["unverified_paths"] == [MISSING_PATH]


def test_a4_old_size_difference_is_not_verified():
    r = lan_sync.decide_lan_arrival(1, SIZEDIFF_ERR, SIZEDIFF_COMB, _none_changed)
    assert r["verified"] is False
    assert r["differ"] == 1
    assert r["unverified_paths"] == [SIZEDIFF_PATH]


def test_a4_size_difference_changed_during_the_backup_is_verified():
    r = lan_sync.decide_lan_arrival(1, SIZEDIFF_ERR, SIZEDIFF_COMB, _changed(SIZEDIFF_PATH))
    assert r["verified"] is True
    assert r["differ"] == 1
    assert r["changed_during_backup"] == 1


def test_a5_no_completion_summary_is_not_verified():
    r = lan_sync.decide_lan_arrival(1, NOTICE, MISSING_COMB, _all_changed)
    assert r["verified"] is False
    assert r["reason"]


@pytest.mark.parametrize("exit_code", [2, 3, 7, -1, None])
def test_a6_exit_codes_other_than_0_and_1_are_not_verified(exit_code):
    r = lan_sync.decide_lan_arrival(exit_code, MISSING_ERR, MISSING_COMB, _all_changed)
    assert r["verified"] is False


def test_a6_a_bang_line_is_not_verified_even_on_a_clean_summary():
    r = lan_sync.decide_lan_arrival(0, CLEAN_ERR, CLEAN_COMB + "! unreadable.txt\n", _none_changed)
    assert r["verified"] is False


def test_a6b_a_question_mark_line_is_not_verified():
    r = lan_sync.decide_lan_arrival(0, CLEAN_ERR, CLEAN_COMB + "? odd.txt\n", _none_changed)
    assert r["verified"] is False


def test_a7_summary_says_one_difference_but_combined_has_none_is_not_verified():
    r = lan_sync.decide_lan_arrival(1, MISSING_ERR, CLEAN_COMB, _all_changed)
    assert r["verified"] is False


def test_a7_combined_has_a_difference_but_summary_says_none_is_not_verified():
    combined = CLEAN_COMB + "+ sub/deep/c.txt\n"
    r = lan_sync.decide_lan_arrival(0, CLEAN_ERR, combined, _all_changed)
    assert r["verified"] is False


# ── check_lan_arrival (run_tool + changed_since on the real source tree) ─

def test_a8_check_argv_is_the_plan_2_1_check_argv(tmp_path):
    src, dst, art = _setup(tmp_path)
    config = LanConfig()
    spec = {"exit_code": 0, "stderr": CLEAN_ERR, "combined": CLEAN_COMB}
    with fake_rclone_lan({"rclone-lan-check": spec}) as fake:
        result = lan_sync.check_lan_arrival(config, str(src), dst, art,
                                            changed_after_epoch=time.time() + 100, timeout=1800)
    assert fake.steps() == ["rclone-lan-check"]
    argv = fake[0]["argv"]
    combined = _flag(argv, "--combined")
    assert argv[1:] == [
        "check", str(src), dst,
        "--one-way", "--size-only", "--ignore-case", "--skip-links",
        "--checkers", str(config.checkers),
        "--combined", str(combined),
        "--exclude", ".AAM_TARGET_MOUNTED",
        "--exclude", PROBE_PATTERN,
        "--exclude", "System Volume Information/**",
        "--exclude", "$RECYCLE.BIN/**",
    ]
    assert "--log-file" not in argv and "--use-json-log" not in argv
    assert fake[0]["timeout_s"] == 1800
    assert result["verified"] is True
    assert result["matching"] == 9


def test_a8_checkers_come_from_config(tmp_path):
    src, dst, art = _setup(tmp_path)
    config = LanConfig(checkers=7)
    spec = {"exit_code": 0, "stderr": CLEAN_ERR, "combined": CLEAN_COMB}
    with fake_rclone_lan({"rclone-lan-check": spec}) as fake:
        lan_sync.check_lan_arrival(config, str(src), dst, art,
                                   changed_after_epoch=time.time() + 100, timeout=60)
    argv = fake[0]["argv"]
    assert argv[argv.index("--checkers") + 1] == "7"


def test_a9_timeout_is_not_verified(tmp_path):
    src, dst, art = _setup(tmp_path)
    spec = {"exit_code": None, "timed_out": True}
    with fake_rclone_lan({"rclone-lan-check": spec}):
        result = lan_sync.check_lan_arrival(LanConfig(), str(src), dst, art,
                                            changed_after_epoch=time.time() + 100, timeout=60)
    assert result["verified"] is False
    assert result["reason"]


def test_a9_spawn_error_is_not_verified(tmp_path):
    src, dst, art = _setup(tmp_path)
    spec = {"exit_code": None, "spawn_error": "FileNotFoundError: missing"}
    with fake_rclone_lan({"rclone-lan-check": spec}):
        result = lan_sync.check_lan_arrival(LanConfig(), str(src), dst, art,
                                            changed_after_epoch=time.time() + 100, timeout=60)
    assert result["verified"] is False


def _missing_spec():
    return {"exit_code": 1, "stderr": MISSING_ERR, "combined": MISSING_COMB}


def _write_source_file(src: Path, mtime: float) -> None:
    path = src / "sub" / "deep" / "c.txt"
    path.write_text("content", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _set_source_mtime_ns(src: Path, whole_seconds: int) -> None:
    path = src / "sub" / "deep" / "c.txt"
    path.write_text("content", encoding="utf-8")
    os.utime(path, ns=(whole_seconds * 10**9, whole_seconds * 10**9))


def _check(src, dst, art, *, after, window=None, spec=None):
    config = LanConfig() if window is None else LanConfig(modify_window_seconds=window)
    with fake_rclone_lan({"rclone-lan-check": spec or _missing_spec()}):
        return lan_sync.check_lan_arrival(config, str(src), dst, art,
                                          changed_after_epoch=after, timeout=60)


# ── R1 (M2) rows: the threshold is the epoch, no tolerance window ───

def test_a2b_file_exactly_at_the_epoch_counts_as_changed(tmp_path):
    src, dst, art = _setup(tmp_path)
    after = float(int(time.time()) + 100)
    _set_source_mtime_ns(src, int(after))
    result = _check(src, dst, art, after=after)
    assert result["verified"] is True
    assert result["changed_during_backup"] == 1


def test_a3b_file_one_second_before_the_epoch_is_not_verified(tmp_path):
    src, dst, art = _setup(tmp_path)
    after = float(int(time.time()) + 100)
    _set_source_mtime_ns(src, int(after) - 1)
    result = _check(src, dst, art, after=after)
    assert result["verified"] is False
    assert result["unverified_paths"] == [MISSING_PATH]


def test_a3c_a_wide_modify_window_does_not_widen_the_threshold(tmp_path):
    src, dst, art = _setup(tmp_path)
    after = float(int(time.time()) + 100)
    _set_source_mtime_ns(src, int(after) - 1)
    result = _check(src, dst, art, after=after, window=10)
    assert result["verified"] is False


def test_a3d_creation_time_counts_when_mtime_is_old(tmp_path):
    src, dst, art = _setup(tmp_path)
    after = time.time() - 5
    _write_source_file(src, after - 3600)  # created now (ctime now), mtime an hour old
    result = _check(src, dst, art, after=after)
    assert result["verified"] is True
    assert result["changed_during_backup"] == 1


# ── R2 (S2) rows: errors beyond differences (branch already in decide_lan_arrival) ──

def test_a10_more_check_errors_than_differences_is_not_verified():
    stderr = MISSING_ERR.replace("1 errors while checking", "3 errors while checking")
    assert "3 errors while checking" in stderr
    r = lan_sync.decide_lan_arrival(1, stderr, "+ a.txt\n", lambda p: True)
    assert r["verified"] is False
    assert "3 error(s) beyond its 1 difference(s)" in r["reason"]


def test_a10b_errors_equal_to_differences_stays_verified():
    r = lan_sync.decide_lan_arrival(1, MISSING_ERR, MISSING_COMB, _all_changed)
    assert r["verified"] is True
    assert r["reason"] is None


def test_a2_changed_since_reads_the_source_mtime_and_verifies(tmp_path):
    src, dst, art = _setup(tmp_path)
    after = time.time() + 100
    _write_source_file(src, after + 1)  # written after the epoch: changed during the backup
    with fake_rclone_lan({"rclone-lan-check": _missing_spec()}):
        result = lan_sync.check_lan_arrival(LanConfig(), str(src), dst, art,
                                            changed_after_epoch=after, timeout=60)
    assert result["verified"] is True
    assert result["changed_during_backup"] == 1


@pytest.mark.parametrize("window, offset, changed", [
    (2, 0, True),    # exactly at the epoch counts as changed (>=)
    (2, -1, False),  # one second before the epoch does not, even with a window of 2
    (10, -1, False), # a wide lan.modify_window_seconds must not move the epoch
    (0, 0, True),
])
def test_a2_changed_since_threshold_is_the_epoch_not_the_window(tmp_path, window, offset, changed):
    src, dst, art = _setup(tmp_path)
    after = float(int(time.time()) + 100)  # integer epoch: exactly representable
    _set_source_mtime_ns(src, int(after) + offset)
    config = LanConfig(modify_window_seconds=window)
    with fake_rclone_lan({"rclone-lan-check": _missing_spec()}):
        result = lan_sync.check_lan_arrival(config, str(src), dst, art,
                                            changed_after_epoch=after, timeout=60)
    assert result["verified"] is changed
    assert result["changed_during_backup"] == (1 if changed else 0)


def test_a3_source_file_older_than_the_sync_start_is_not_verified_and_listed(tmp_path):
    src, dst, art = _setup(tmp_path)
    after = time.time() + 100
    _write_source_file(src, time.time() - 3600)
    with fake_rclone_lan({"rclone-lan-check": _missing_spec()}):
        result = lan_sync.check_lan_arrival(LanConfig(), str(src), dst, art,
                                            changed_after_epoch=after, timeout=60)
    assert result["verified"] is False
    assert result["unverified_paths"] == [MISSING_PATH]


def test_changed_since_treats_a_missing_source_file_as_not_changed(tmp_path):
    src, dst, art = _setup(tmp_path)  # sub/deep/c.txt is never created: OSError on stat
    with fake_rclone_lan({"rclone-lan-check": _missing_spec()}):
        result = lan_sync.check_lan_arrival(LanConfig(), str(src), dst, art,
                                            changed_after_epoch=time.time() + 100, timeout=60)
    assert result["verified"] is False
    assert result["unverified_paths"] == [MISSING_PATH]
