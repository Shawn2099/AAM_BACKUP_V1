# LAN rclone fixtures (plan P1 C0b)

Recorded with rclone v1.74.3 (`deploy\bin\rclone.exe`) on 2026-11-25. Source tree on local disk, destination on the loopback admin share `\\127.0.0.1\C$\...` (rclone shows it as `//?/UNC/127.0.0.1/C$/...`). Paths in the files are sanitised: `X:/rig/` is the destination or source root, `X:/rclone.conf` is the (absent) config file. `SRC`, `DST`, `LOG`, `COMB` below stand for those paths.

Common flags of every sync and dry-run (plan 2.1): `--check-first --create-empty-src-dirs --skip-links --ignore-case --modify-window 2s --transfers 4 --checkers 4 --retries 3 --retries-sleep 10s --use-json-log --log-level INFO --log-file LOG --exclude .AAM_TARGET_MOUNTED --exclude .aam_write_probe_* --exclude "System Volume Information/**" --exclude "$RECYCLE.BIN/**"`. The dry-run drops `--max-delete` and adds `--dry-run --combined COMB`. Check: `rclone check SRC DST --one-way --size-only --ignore-case --skip-links --checkers 4 --combined COMB` plus the 4 excludes, no log file.

- `lan_clean.jsonl`: `rclone sync SRC DST <common>` (no track), first run into empty DST, exit 0.
- `lan_clean_dryrun.jsonl`: `rclone sync SRC DST <common> --dry-run --combined COMB` before the clean sync, exit 0.
- `lan_clean_combined.txt`: `--combined` output of `lan_clean_dryrun`, exit 0.
- `lan_nochange.jsonl`: `rclone sync SRC DST <common>` re-run with no source change, exit 0.
- `lan_rename.jsonl`: `rclone sync SRC DST <common> --track-renames` after folder `photos` renamed to `photos_renamed` (5 files), exit 0.
- `lan_rename_dryrun.jsonl`: `rclone sync SRC DST <common> --track-renames --dry-run --combined COMB` before `lan_rename`, exit 0.
- `lan_rename_combined.txt`: `--combined` output of `lan_rename_dryrun`, exit 0.
- `lan_deletes.jsonl`: `rclone sync SRC DST <common> --track-renames` after 3 source files deleted, exit 0.
- `lan_deletes_dryrun.jsonl`: `rclone sync SRC DST <common> --track-renames --dry-run --combined COMB` before `lan_deletes`, exit 0.
- `lan_deletes_combined.txt`: `--combined` output of `lan_deletes_dryrun`, exit 0.
- `lan_maxdelete.jsonl`: separate pair (5 files, 3 deleted at source), `rclone sync SRC DST <common> --max-delete 1`, exit 7.
- `lan_check_clean.stderr.txt`: `rclone check SRC DST --one-way --size-only --ignore-case --skip-links --checkers 4 --combined COMB <excludes>` after `lan_deletes`, exit 0.
- `lan_check_clean_combined.txt`: `--combined` output of the clean check, exit 0.
- `lan_check_missing.stderr.txt`: same check after destination file `sub/deep/c.txt` deleted, exit 1.
- `lan_check_missing_combined.txt`: `--combined` output of the missing check, exit 1.
- `lan_check_sizediff.stderr.txt`: same check after the destination file restored and source `photos_renamed/p1.jpg` made 7 bytes larger (2001 to 2008), exit 1.
- `lan_check_sizediff_combined.txt`: `--combined` output of the sizediff check, exit 1.
- Extra `lan_mixed.jsonl`: `rclone sync SRC DST <common> --track-renames --max-delete 2` after a 5-file folder rename plus 2 source deletes, exit 0.
- Extra `lan_mixed_dryrun.jsonl`: `rclone sync SRC DST <common> --track-renames --dry-run --combined COMB` before `lan_mixed`, exit 0.
- Extra `lan_mixed_combined.txt`: `--combined` output of `lan_mixed_dryrun`, exit 0.
- Extra `lan_h_marker_notexcluded_combined.txt`: dry-run with the 4 excludes removed (marker and probe file in source), exit 0.
- Extra `lan_h_marker_excluded_combined.txt`: the same dry-run with the excludes, exit 0.
