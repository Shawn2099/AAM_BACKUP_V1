"""P1-COUNT (A-prime) - positional robocopy summary parsing + bit-3 floor.

Evidence: LAN-06/LAN-15 ledger rows - /NJS suppressed the job summary so real
copy failures reported files_failed=0. Fix: drop /NJS, parse the 'Files:'
stats ROW positionally (columns fixed by robocopy regardless of locale),
floor by exit-bitmask bit 3.
"""
from core.lan_sync import (
    build_robocopy_command,
    failed_file_count,
)


def _summary_row(total, copied, skipped, mismatch, failed, extras, sep=""):
    return (
        "\n\n               Total    Copied   Skipped  Mismatch    FAILED    Extras\n"
        f"    Files :     {total}{sep}      {copied}         {skipped}"
        f"         {mismatch}       {failed}{sep}        {extras}\n"
    )


LOG_WITH_SUMMARY = (
    "   New File              1024  good.txt\n"
    "2026/08/24 10:00:00 ERROR 32 (0x00000020) Copying File ledger.xlsx\n"
    + _summary_row(3, 1, 0, 0, 2, 0)
)

LOG_WITHOUT_SUMMARY = (
    "   New File              1024  good.txt\n"
    "2026/08/24 10:00:00 ERROR 32 (0x00000020) Copying File ledger.xlsx\n"
)


def test_summary_row_positional_failed_column():
    # FAILED is index 4 of the numeric columns - NOT word-matched
    assert failed_file_count(LOG_WITH_SUMMARY, 8) == 2


def test_locale_thousands_separators_tolerated():
    log = _summary_row(1234, 1200, 0, 0, 34, 0, sep=",")
    assert failed_file_count(log, 8) == 34


def test_missing_summary_fails_closed_to_bit3_floor():
    # old /NJS-style log: no summary block at all -> bit3 still forces >= 1
    assert failed_file_count(LOG_WITHOUT_SUMMARY, 9) == 1


def test_no_bit3_and_no_summary_is_zero():
    assert failed_file_count(LOG_WITHOUT_SUMMARY, 3) == 0


def test_parsed_zero_with_bit3_floors_to_one():
    # contradictory signals must fail LOUD-side, never report 0 failures
    log = _summary_row(5, 5, 0, 0, 0, 0)
    assert failed_file_count(log, 12) == 1


def test_fatal_16_without_summary_is_zero_failures():
    # bit4: nothing copied, no per-file failures recorded
    assert failed_file_count(LOG_WITHOUT_SUMMARY, 16) == 0


def test_command_no_longer_suppresses_summary():
    from models.config import LanConfig
    cmd = build_robocopy_command(
        "C:\\src", "\\\\nas\\share",
        LanConfig(enabled=True, retry_count=3, retry_wait_seconds=10,
                  subprocess_timeout_seconds=3600, shutdown_after_backup=False,
                  max_attempts=1, retry_delay_seconds=60, mt_threads=4),
    )
    assert "/NJS" not in [f.upper() for f in cmd]
