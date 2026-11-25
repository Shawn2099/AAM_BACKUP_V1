# Test matrix: Phase 1 review amendments (2026-11-25)

Author: Opus (planner). Reference for builders and test writers of chunks R1-R11 in
`docs/PLAN_P1_RCLONE_LAN_2026-11-25.md` section "Review amendments 2026-11-25". Binding:
builders and test writers use these rows, not their own guesses. New cases found later are
added here (by Opus) before they are built.

Layers: **U** unit (pure function or wrapper with `fake_rclone_lan`, or real rclone local -> local
on `tmp_path` where the row says so), **F** flow (`_run_lan_pipeline` / tasks with
`fake_rclone_lan`), **L** audit lab (real `deploy\bin\rclone.exe`, local disk -> `\\127.0.0.1\C$`,
`unc_nas` fixture). "Kills" names the mutant the row must turn red (Opus review runs it).

## 0. Facts measured for this matrix (rclone v1.74.3, 2026-11-25, scratch dirs -> loopback share)

| # | Fact | Used by |
|---|---|---|
| X1 | Read-only destination file + changed source: ERROR `partial file rename failed: rename <dst>\ro.txt.7514dbf0.partial <dst>\ro.txt: Access is denied.`, `object` = `ro.txt.7514dbf0.partial`, `objectType` `*local.Object`; then `not deleting files as there were IO errors` (`objectType` `*local.Fs`); exit 1; `stats.errors` 1, `deletes` 0; the pending delete stays at the destination. | S1, R11, D17, L12 |
| X2 | Deleting a read-only destination file works (rclone clears the attribute): `Deleted`, exit 0. Only REPLACING one fails. | R11 |
| X3 | Destination file held open (share mode 0) and deleted at source: 10 NOTICE `Remove detected sharing violation - retry k/10`, then ERROR `Couldn't delete: remove <dst>\del.txt: The process cannot access the file because it is being used by another process.`, `object` `del.txt`, `objectType` `*local.Object`; only `not deleting directories as there were IO errors` (NO "not deleting files" line, other deletes went ahead); exit 1. `stats.deletes` = 3 with only 2 files really gone (each failed delete attempt is counted), `stats.errors` 2 for one file. | S1, D17b, Deferred F1 |
| X4 | `--backup-dir` dry-run: deletions are `"skipped":"move into backup dir"` (NOT `"delete"`); a replaced (changed) file is `"skipped":"move"` (object = that file) plus its `"skipped":"copy"`; renames stay `"skipped":"move to <new>"`. Dry-run stats: `deletes` 2, `renames` 4 (3 renames + 1 replace). Real run: `Moved into backup dir` per deletion, replaced file logs `Moved (server-side)` then `Copied (new)` (not "replaced existing"); stats `deletes` 2, `renames` 6, `serverSideMoves` 6. | M3, P7, V13 |
| X5 | `--backup-dir` + `--max-delete 1` with 3 deletes + 1 replace: exit 7, 1 file moved into the backup dir, the replaced file still replaced; `--max-delete 0` with only a replace: exit 0. So `--max-delete` counts backup-dir deletions and not replace moves. | V13 |
| X6 | `rclone check --files-from ... --exclude ...` is refused: CRITICAL `the usage of --files-from overrides all other filters, it should be used alone or with --files-from-raw`, exit 1. `--files-from` with `--ignore-case` and `--skip-links` is accepted. | S3, A11 |
| X7 | `--files-from` drops lines starting with `#` or `;` and strips leading spaces (files `#hash.txt`, `;semi.txt`, ` lead space.txt` were silently NOT checked); `--files-from-raw` checks all 5. | S3, A11 |
| X8 | Hash check (no `--size-only`) over a list: same-size corruption gives `* new.txt`, stderr `ERROR : new.txt: md5 differ`, `1 differences found`, `1 errors while checking`, `2 matching files`, `Failed to check: 1 differences found`, exit 1. A listed path absent on the source is skipped silently (no line, not counted). An EMPTY list prints `0 differences found` and NO "matching files" line, exit 0. | S3, A12, A13e |
| X9 | Sync JSON log entries for transfers: `{"level":"info","msg":"Copied (new)","size":N,"object":P}` and `"msg":"Copied (replaced existing)"`; renames are `Moved (server-side) to: <new>` (object = old) plus `Renamed from "<old>"`. | S3, H1-H4 |
| X10 | Mixing `--include` and `--exclude` (as a sharded `audit_lan` would): rclone warns `--include and --exclude as the order they are parsed in is indeterminate`, and the excludes were NOT applied (canary, probe and `System Volume Information` files listed inside the shard). Ordered `--filter` rules are required. `--ignore-case` is a FILTER flag ("Ignore case in filters"), not case-insensitive name matching. | S4, AU2 |
| X11 | Python 3.12.15 on Windows: `st_ctime == st_birthtime` (creation time), no DeprecationWarning under `-W error`. | M2 docstring |

## 1. M2 arrival tolerance (chunk R1)

Rule: `changed_since(p)` = `max(st_mtime, st_ctime) >= changed_after_epoch` on the source file (no `- lan.modify_window_seconds`); OSError = not changed.

| ID | Scenario | Inputs | Expected | Layer | Fixture | Kills |
|---|---|---|---|---|---|---|
| A2b | File exactly at the epoch | `after = float(int(time.time()) + 100)` (an integer, exactly representable); source `sub/deep/c.txt` mtime set with `os.utime(path, ns=(int(after)*10**9,)*2)`; fake check = `_missing_spec()` | `verified True`, `changed_during_backup 1` | U | existing `lan_check_missing.*` | `>=` -> `>` |
| A3b | File 1 s before the epoch | as A2b, mtime `after - 1` | `verified False`, `unverified_paths == ["sub/deep/c.txt"]` | U | same | keeping the window (`- modify_window_seconds`) |
| A3c | Window is no longer read | `LanConfig(modify_window_seconds=10)`, mtime `after - 1` | `verified False` | U | same | keeping the window with a non-default value |
| A3d | Creation time counts | file created now (ctime now), mtime set to `after - 3600`, `after = time.time() - 5` | `verified True` (ctime >= epoch) | U | same | `max(mtime, ctime)` -> `mtime` |

**Tests to be rewritten by the test agent under Opus (frozen to the code agent):** in `tests/test_p1_c7_lan_arrival.py`
- `test_a2_modify_window_edge_comes_from_config`: replace the parameter list `[(2,-2,True),(2,-3,False),(5,-4,True),(5,-6,False)]` with `[(2, 0, True), (2, -1, False), (10, -1, False), (0, 0, True)]` (ids `[2-0-True]`, `[2--1-False]`, `[10--1-False]`, `[0-0-True]`); `after` becomes `float(int(time.time()) + 100)` and mtime is set with `ns=` as in A2b; rename the test to `test_a2_changed_since_threshold_is_the_epoch_not_the_window`. Today's ids `[2--2-True]` and `[5--4-True]` encode the tolerance and must go.
- `test_a2_changed_since_reads_the_source_mtime_and_verifies`: `_write_source_file(src, after - 1)` -> `_write_source_file(src, after + 1)` (a write after the epoch). Expectations unchanged (`verified True`, `changed_during_backup 1`).
- `_write_source_file` may gain an `ns` variant; A3/A9 and the other rows stay as they are.

## 2. S2 errors beyond differences (chunk R2)

| ID | Scenario | Inputs | Expected | Layer | Kills |
|---|---|---|---|---|---|
| A10 | More check errors than differences | `decide_lan_arrival(1, stderr, "+ a.txt\n", lambda p: True)`; stderr = `lan_check_missing.stderr.txt` text with `1 errors while checking` replaced by `3 errors while checking` (one `+` line, `1 differences found`) | `verified False`, reason contains `3 error(s) beyond its 1 difference(s)` | U | dropping the `errors and int(errors[-1]) > differences` branch |
| A10b | Errors equal differences stays verified | the unmodified missing fixture with `changed_since` true | `verified True` (already covered by A2; re-assert here next to A10) | U | `>` -> `>=` |

No code change is expected (the branch exists); the chunk is test-only and must show the mutant red.

## 3. S1 failed object paths (chunk R3)

Rule for `cloud_sync.failed_object_paths(log_text, limit)`: entries of the FINAL attempt (existing `_final_attempt_start`) at level ERROR/CRITICAL whose `msg` starts with `Failed to copy`, `partial file rename failed` or `Couldn't delete`, whose `object` is a non-empty str and whose `objectType` does not end in `.Fs`. The name has `re.sub(r"\.[0-9a-f]{8}\.partial$", "", obj)` applied, then de-duplicated in log order, capped at `limit`.

Fixtures (test agent generates with `deploy\bin\rclone.exe`, local SRC -> `\\127.0.0.1\C$\<scratch>\DST`, C0b sanitising, `<common>` flags with `--retries 3 --retries-sleep 1s`; recipe appended to `test_support/rclone_logs/LAN_FIXTURES.md`; the `object` field is never edited):
- `lan_readonly_dst_exit1.jsonl`: initial sync of `a.txt`, `ro.txt`, `del.txt`; `attrib +R DST\ro.txt`; source `ro.txt` rewritten longer, source `del.txt` deleted; sync. Expected exit 1.
- `lan_locked_dst_delete_exit1.jsonl`: initial sync of `a.txt`, `del.txt`, `del2.txt`; both deleted at source; `DST\del.txt` held open with `CreateFileW(..., share=0)` during the sync. Expected exit 1.

| ID | Scenario | Inputs | Expected | Layer | Kills |
|---|---|---|---|---|---|
| D17 | Read-only destination file | `decide_lan_rclone_result(1, <lan_readonly_dst_exit1>)` | `LAN_PARTIAL`, `failed_paths == ["ro.txt"]` (suffix stripped), `deletes_held True`, reason names `ro.txt` | U | removing the `partial file rename failed` prefix; removing the suffix strip |
| D17b | Locked destination delete | `decide_lan_rclone_result(1, <lan_locked_dst_delete_exit1>)` | `LAN_PARTIAL`, `failed_paths == ["del.txt"]`, `deletes_held False` | U | removing the `Couldn't delete` prefix |
| D17c | Fs-level errors are never paths | synthetic final-attempt ERROR `{"msg":"partial file rename failed: x","object":"Local file system at X:/rig/dst","objectType":"*local.Fs"}` | `failed_object_paths == []` | U | dropping the `.Fs` test |
| D17d | Only rclone's partial suffix is stripped | objects `a.txt.7514dbf0.partial` -> `a.txt`; `b.partial` -> `b.partial`; `c.txt.7514DBF0.partial` -> unchanged; `d.txt.12345678.partial.bak` -> unchanged | as listed | U | a looser regex (`\.partial$`, no anchor, case-insensitive) |
| D17e | Same file in two messages | final attempt has `Failed to copy` for `x.txt` and `partial file rename failed` for `x.txt.0badc0de.partial` | `["x.txt"]` once | U | dedupe before strip |
| D17f | Earlier attempt only | `partial file rename failed` in attempt 1/3, attempts 2-3 clean and "Attempt 3/3 succeeded" (synthetic from D5's fixture pattern) | `[]` | U | ignoring `_final_attempt_start` |
| D6 | Unchanged | locked source fixture | `["locked"]` | U | - |

## 4. M3 planned counts with `--backup-dir` (chunk R4)

Rule: `count_planned_deletes` counts `skipped == "delete"` OR `skipped == "move into backup dir"`; `count_planned_moves` counts `skipped` that starts with `"move to "` only (the bare `"move"` of a replaced file under `--backup-dir` is neither).

Fixture `lan_backupdir_dryrun.jsonl` (test agent generates as X4): initial sync of `keep0..2.txt`, `gone0..1.txt`, `change.txt`, `photos/p0..2.jpg`; then delete `gone0..1`, rewrite `change.txt` longer, rename folder `photos` -> `photos_renamed`; dry-run `<common> --track-renames --backup-dir \\127.0.0.1\C$\<scratch>\bak\2026-11-25_0100_FY --dry-run --combined COMB`. Expected exit 0.

| ID | Scenario | Inputs | Expected | Layer | Kills |
|---|---|---|---|---|---|
| P7 | Backup-dir dry-run counts | `lan_backupdir_dryrun.jsonl` | `count_planned_deletes == 2`, `count_planned_moves == 3`, `planned_copy_count == 1` | U (real-rclone fixture) | old `startswith("move")` (gives 6); missing `move into backup dir` in deletes (gives 0) |
| P7b | No regression without backup-dir | `lan_mixed_dryrun.jsonl`, `lan_rename_dryrun.jsonl` | mixed: deletes 2, moves 5; rename: deletes 0, moves 5 | U | `"move to "` -> `"move to"` typo is harmless; `== "move"` counted as move |
| P7c | Bare `"move"` is neither | synthetic entry `{"skipped":"move","object":"change.txt","size":3}` | deletes 0, moves 0 | U | counting `"move"` as a delete |

## 5. S4 integrity audit uses the mirror's filters (chunk R5)

Finding: NOT handled today. `integrity._run_rclone_check` (called by `audit_lan`) passes no excludes, no `--skip-links`, no `--ignore-case`; only the canary is filtered after the fact. On a `D:\` root source `$RECYCLE.BIN/**` and `System Volume Information/**` show as missing (`+`), and a leftover `.aam_write_probe_*` as missing or extra, so the weekly audit can never be VERIFIED there.

Rule: `audit_lan` adds, without shards, `--exclude <p>` for every `lan_sync.LAN_EXCLUDES` entry plus `--skip-links --ignore-case`. With `scope_prefixes`: ordered rules `--filter "- <p>"` for each exclude, then `--filter "+ <prefix>/**"` per prefix, then `--filter "- **"`, plus `--skip-links --ignore-case`; never `--include` and `--exclude` together (X10). The post-hoc canary filter stays (defence in depth). `audit_cloud` is unchanged.

| ID | Scenario | Inputs | Expected | Layer | Kills |
|---|---|---|---|---|---|
| AU1 | Full audit argv | `audit_lan(src, dst)` with patched `run_tool` capturing argv | 4 `--exclude` values equal `LAN_EXCLUDES` in order; `--skip-links`, `--ignore-case` present; no `--one-way`, no `--size-only`, no `--include` | U | dropping any exclude or flag |
| AU2 | Sharded audit argv | `scope_prefixes=["FY26"]` | `--filter "- p"` x4 (same order) BEFORE `--filter "+ FY26/**"`, last `--filter "- **"`; no `--include`/`--exclude` | U | old `--include` form; wrong order |
| AU3 | Real rclone, excluded items only on source | `tmp_path` src has `data.txt`, `$RECYCLE.BIN/r.txt`, `System Volume Information/s.txt`, `.aam_write_probe_ab12cd`, dst has only `data.txt` (local -> local, real binary like `tests/test_integrity_audit.py`) | `VERIFIED` (old code: `VERIFICATION_FAILED`, 3 missing) | U | any missing exclude |
| AU4 | Real rclone, sharded with excluded items inside the shard | src `FY26/x.pdf`, `FY26/System Volume Information/s.txt`, `FY26/.aam_write_probe_1`; dst `FY26/x.pdf`; `scope_prefixes=["FY26"]` | `VERIFIED`; and with dst `FY26/x.pdf` changed -> `VERIFICATION_FAILED` | U | `--include` + `--exclude` mix (X10) |
| AU5 | Existing sharding test | `test_scope_sharding_limits_checked_set` | unchanged and green | U | - |

## 6. S5 hang after the SOFT cutoff (chunk R6)

Rule in `flow.lan_sync_task`: when the budget limits the run AND `max_duration` is not None AND `run_lan_mirror` returns `timed_out True`, rclone did not stop within the 300 s margin after its `--max-duration`. Return `{**result, "status": "LAN_PARTIAL", "killed_after_cutoff": True, "budget_stopped": False, "fault_code": "AAM-LAN-04", "error": <text>}`, text: `rclone did not stop within 300 s after its time limit (--max-duration {max_duration}s, --cutoff-mode SOFT) and was killed after {timeout}s - a large file may still have been copying, or NAS I/O may be hung. {done}`. The margin value comes from the same place `resolve_max_duration_seconds` uses (no new literal: expose it as a module constant `cloud_sync.MAX_DURATION_MARGIN_S` in the same chunk). Exit 10 with a limit, or a timeout with a limit but `max_duration` None (timeout <= margin), keeps today's `budget_stopped` path. The post block's metrics `verify` text gains `"skipped - rclone was killed after its time limit"` for this flag.

| ID | Scenario | Inputs | Expected | Layer | Kills |
|---|---|---|---|---|---|
| W14 | Hang after cutoff | budget binding, `fake_rclone_lan` sync spec `timed_out True` (no exit) | `LAN_PARTIAL`, `killed_after_cutoff True`, `budget_stopped` falsy, fault `AAM-LAN-04` on the run row/alert, alert text contains "did not stop" and not "Stopped at the run budget", steps `[PLAN, SYNC]` (no check), no NAS shutdown, 1 sync attempt | F | old `stopped = ... or (limited and timed_out)` |
| W14b | Budget too small for a cutoff | budget binding with timeout <= 300 (so `max_duration` None), sync `timed_out True` | `budget_stopped True`, old text "Stopped at the run budget", no `killed_after_cutoff` | F | flagging every limited timeout as a hang |
| W14c | Exit 10 | W8 unchanged | `budget_stopped True` | F | - |

## 7. S7 reserve description (chunk R7)

| ID | Scenario | Expected | Layer | Kills |
|---|---|---|---|---|
| K4 | `budget.lan_post_reserve_minutes` description | `BudgetConfig.model_fields["lan_post_reserve_minutes"].description` contains "arrival check" and "shutdown", does not contain "destination walk"; `config_help` label/help for the key mention the arrival check(s) and the NAS shutdown | U | old text |

## 8. S3 nightly hash check of tonight's transfers (chunks R8a-R8d)

Rules
- Keys (LanConfig): `arrival_hash_transferred: bool = True`; `arrival_hash_max_gb: int = 50` (`ge=0, le=10000`, GB = `permissions.GB` = 1024**3); `arrival_hash_timeout_seconds: int = 3600` (`ge=60, le=14400`). Each with a `config_help` entry.
- `lan_sync.transferred_files(log_texts: list[str]) -> tuple[list[str], int | None]`: over all given sync JSON logs (every attempt of this FY pass, every rclone-internal attempt), entries with `level` info, `msg` starting with `Copied (` (covers `Copied (new)` and `Copied (replaced existing)`, X9; under `--backup-dir` a replaced file is `Copied (new)`, X4), `object` a non-empty str. Paths de-duplicated in first-seen order. Bytes = sum of the LAST seen int `size` per path; any such entry without an int `size` -> bytes None.
- `lan_sync.check_lan_transfers_hash(config, source, dest, artefacts_dir, *, paths, changed_after_epoch, timeout) -> dict`: writes `paths` to `artefacts.new_path("rclone-lan-hash-files", "txt")` (UTF-8, no BOM, `"\n"` join, trailing newline), runs step `rclone-lan-hash`: `rclone check SRC DST --one-way --files-from-raw LIST --skip-links --ignore-case --checkers {lan.checkers} --combined HASH_COMBINED`. Never `--size-only`, `--files-from`, `--exclude`, `--download`, `--checksum`. The list comes from tonight's sync, which already applied `LAN_EXCLUDES` (X6 forbids excludes here). Decision = `decide_lan_arrival` with the same `changed_since` rule as R1 (shared helper, no copy). Spawn error / timeout -> `verified False` with reason, same shape as `check_lan_arrival`.
- `flow.lan_verify_task(config, artefacts_dir, changed_after_epoch, budget=None, *, sync_log_paths)`: runs the size-only arrival check as today; then the hash step only when (a) arrival verified, (b) `arrival_hash_transferred` true, (c) `transferred_files` gives at least one path, (d) bytes is not None and `bytes <= arrival_hash_max_gb * GB`. Otherwise `hash = {"ran": False, "reason": ...}` and the verdict stays the size-only one. Reasons: `"off (lan.arrival_hash_transferred)"`, `"nothing copied tonight"`, `"arrival not verified"`, `"size of tonight's copies unknown"`, `"{gb:.1f} GB copied tonight is above lan.arrival_hash_max_gb {cap}"`. Reading a sync log raises OSError -> verdict not verified, reason `"hash check could not read tonight's sync log: ..."` (fail closed). Timeout = `budget.cap(lan.arrival_hash_timeout_seconds, 0)`; `BudgetExceeded` here -> return the arrival dict with `verified False`, `budget_stopped True`, hash `{"ran": False, "reason": "no time left"}`.
- Return: the arrival dict plus `hash = {ran, verified, files, bytes, matching, missing, differ, changed_during_backup, reason, unverified_paths(capped lan.error_tail_lines)}`; when the hash ran, top-level `verified` = hash verified and `reason` = `"hash check: " + hash reason`.
- Flow (`_run_lan_pipeline`): `run_once` records each attempt's `log_path` (also from `SyncFailed.result`) into `sync_log_paths`; verify phase passes it. New branch BEFORE the SUSPECT branch: `arrival.get("budget_stopped")` -> `LAN_PARTIAL`, `budget_stopped True`, text `_budget_stop_text(budget, "copied and size-checked, but no time left for tonight's hash check")` (fault `AAM-LAN-03`). Hash mismatch / hash timeout -> existing `verified is not True` branch -> `LAN_SUSPECT`, alert `AAM-LAN-04`, no NAS shutdown. `metrics["arrival"]["hash"]` = the hash dict without `unverified_paths` beyond the cap.
- Caveat (documented, not tested): the NAS just wrote these files, so their read-back may come from the NAS RAM cache; this proves the SMB path and the NAS's view, not the disk surface. The weekly `integrity.audit_lan` (full hash) stays the proof.

| ID | Scenario | Inputs | Expected | Layer | Fixture | Kills |
|---|---|---|---|---|---|---|
| K5 | New keys | `LanConfig()` and bounds | defaults True / 50 / 3600; 0 and 10000 accepted, -1 and 10001 rejected for max_gb; 59 and 14401 rejected for timeout; each key has `config_help` | U | - | wrong default/bound |
| H1 | Copied entries | `transferred_files([lan_clean.jsonl text])` | all `Copied (new)` objects in log order; bytes = sum of their sizes (= stats.bytes 13062) | U | `lan_clean.jsonl` | - |
| H2 | Replaced file | new fixture `lan_replaced.jsonl` (sync after one source file rewritten) | that path listed; size = new size | U | `lan_replaced.jsonl` (generate) | matching only `Copied (new)` |
| H3 | Not transfers | `lan_rename.jsonl`, `lan_deletes.jsonl`, any `*_dryrun.jsonl` | `([], 0)` | U | existing | matching `Moved`/`Skipped copy`/`Deleted` |
| H4 | Two attempt logs | same path in both logs with sizes 5 then 7, plus one path per log | 3 unique paths, first-seen order, bytes counts 7 for the shared one | U | synthetic | double counting |
| H5 | Missing size | one `Copied (new)` without `size` | bytes None | U | synthetic | treating missing size as 0 |
| H6 | Error entries | `locked_file_exit1.jsonl` | `locked` not listed | U | existing | matching ERROR level |
| A11 | Hash argv and list file | `check_lan_transfers_hash` with `fake_rclone_lan` step `rclone-lan-hash`, paths `["#hash.txt", " lead.txt", "sub/é.txt"]` | argv exactly as the rule (order of the flag pairs as written); list file bytes == `"#hash.txt\n lead.txt\nsub/é.txt\n"` UTF-8; never-list holds | U | - | `--files-from` (X7); adding `--size-only` or `--exclude` |
| A12 | Hash mismatch | hash spec: stderr/combined of `lan_hash_md5diff.*`; `changed_since` false | `verified False`, `unverified_paths == ["new.txt"]` | U | `lan_hash_md5diff.stderr.txt`, `lan_hash_md5diff_combined.txt` (generate as X8) | - |
| A12b | Mismatch explained by a change after the epoch | as A12, source `new.txt` mtime >= epoch | `verified True`, `changed_during_backup 1` | U | same | not reusing `changed_since` |
| A12c | Clean hash | `lan_hash_clean.*` | `verified True`, `matching` = list length | U | `lan_hash_clean.stderr.txt`, `lan_hash_clean_combined.txt` (generate) | - |
| A13 | Hash timeout | hash spec `timed_out True` | task: `verified False`, reason contains "timed out"; flow: `LAN_SUSPECT`, `AAM-LAN-04`, no shutdown | U + F | - | treating timeout as verified |
| A13b | No budget for the hash | budget with time for the size check but `cap(...)` raising for the hash (patch `RunBudget.cap` to raise on the second call) | task returns `budget_stopped True`; flow `LAN_PARTIAL`, `budget_stopped True`, fault `AAM-LAN-03`, text contains "no time left for tonight's hash check", no shutdown | F | - | budget stop -> SUSPECT or COMPLETE |
| A13c | Over the cap | `arrival_hash_max_gb=0` and a log with one 1-byte copy | hash not run, `ran False`, reason contains `arrival_hash_max_gb`; status `LAN_COMPLETE`; steps `[PLAN, SYNC, CHECK]` | F | `lan_clean.jsonl` | `<=` -> `<` at the boundary (see A13h) |
| A13d | Switch off | `arrival_hash_transferred=False` | no hash step, reason "off ...", COMPLETE | F | - | ignoring the switch |
| A13e | Nothing copied | sync log `lan_nochange.jsonl` | no hash step, reason "nothing copied tonight", COMPLETE, 3 steps | F | existing | running an empty list (X8: no matching line) |
| A13f | Size-only already failed | check spec = missing fixture, not changed | no hash step, SUSPECT (unchanged W5) | F | existing | hashing after a failed arrival |
| A13g | Unknown bytes | sync log with a `Copied (new)` without size | no hash step, reason "size ... unknown", COMPLETE | F | synthetic | - |
| A13h | Cap boundary | bytes exactly `cap * GB` (cap 1, a fake log entry with size 1073741824) | hash RUNS | U (task with fakes) | synthetic | `<=` -> `<` |
| A13i | Unreadable sync log | `sync_log_paths` with a missing file | `verified False`, reason "could not read tonight's sync log"; flow SUSPECT | U | - | swallowing the OSError as "nothing copied" |
| W15 | Happy path with copies | W1 setup + hash spec clean | COMPLETE; steps `[PLAN, SYNC, CHECK, HASH]`; `metrics.arrival.hash` = `{ran True, verified True, files N, bytes B, ...}` | F | `lan_hash_clean.*` | - |
| W16 | Retry union | W7 setup (exit 5 then 0), the two sync logs copy different files | hash list file holds the union of both attempts' copies | F | synthetic logs | using only the last attempt's log |

**Tests to be rewritten by the test agent under Opus (because the hash step is a new 4th call when files were copied):** `tests/test_p1_c9_lan_pipeline.py` line 277 (W1) -> `[PLAN, SYNC, CHECK, HASH]`; line 393 (W7) -> `[PLAN, SYNC, PLAN, SYNC, CHECK, HASH]`; line 449 (W10) -> `[PLAN, SYNC, CHECK, HASH] * 2`; each script gains a `"rclone-lan-hash": <clean spec>`. `test_support/fake_tools.py`: `LAN_RCLONE_STEPS` gains `"rclone-lan-hash"` (T1 self-test row: unknown step still raises, the new one is accepted). Other users of `fake_rclone_lan` in a flow (`test_batch3_fixes.py`, `test_fy_overlap_command_lines.py`, `test_production_scenarios.py`) get a hash spec only where the failure is "no spec for step 'rclone-lan-hash'" (mechanical, logged in `agent_runs/p1_review_sweep.md`); any other failure escalates to Opus.

## 9. Audit lab rows (chunk R10, file `audit_lab/test_w25_rclone_lan.py`)

| ID | Scenario | Expected | Depends on |
|---|---|---|---|
| L12 | Read-only destination file + changed source + 1 source delete (`attrib +R` on the NAS copy, real rclone, `retry_count 1`) | `run_lan_mirror`: `LAN_PARTIAL`, exit 1, `failed_paths == ["ro.txt"]`, `deletes_held True`, the deleted file still at the destination. Pipeline alert text (call the PARTIAL text builder or check `result["reason"]`) names `ro.txt`. After the test clears the attribute (`os.chmod(..., S_IWRITE)`, scratch only) a rerun is `LAN_COMPLETE` and the delete happens. | R3 |
| L13 | Same-size corruption of a file copied tonight | sync copies 3 new files; overwrite one NAS copy with same-size different bytes; `check_lan_arrival` (size-only) verified; `check_lan_transfers_hash` over `transferred_files([sync log])` NOT verified, path listed | R8b |
| L13b | Clean night hash | same without corruption: hash verified, `matching == 3` | R8b |
| L14 | Weekly audit on the share with excluded items on the source | source has `$RECYCLE.BIN/r.txt`, `System Volume Information/s.txt` and a probe file; mirror with `run_lan_mirror`; `audit_lan(src, unc_dest)` -> `VERIFIED`; sharded on a prefix -> `VERIFIED` | R5 |

L1-L11 stay; the M2 change must leave them green (they verify with no `+`/`*` lines).

## 10. Phase 1b row added (no build now)

| ID | Scenario | Expected | Layer |
|---|---|---|---|
| V13 | `--backup-dir` counters and backstop (X4, X5) | plan from a backup-dir dry-run: `planned_deletes` = deletions (P7 rule), `moves` = `move to` only; `--max-delete` counts backup-dir deletions (real: exit 7 after 1 of 3) and not replace moves (real: `--max-delete 0` + 1 replace = exit 0); metrics `moves` must not come from `stats.renames` under backup-dir (it also counts backup moves: 6 for 3 renames + 1 replace + 2 deletes); a replaced file is `Copied (new)` so the S3 list still holds it; the space check counts the bytes of `"skipped":"move"` (replaced) and `move into backup dir` entries as versions-folder growth | U (real fixtures) + L |

## 11. Mutation checks for the Opus review of these chunks

1. R1: `>=` -> `>` (A2b red); re-adding `- config.modify_window_seconds` (A3b, A3c red); `max(...)` -> `st_mtime` (A3d red).
2. R2: delete the `errors ... > differences` branch (A10 red); `>` -> `>=` (A10b red).
3. R3: drop each new prefix (D17 / D17b red); drop the suffix strip (D17 red); `\.partial$` (D17d red); drop the `.Fs` test (D17c red).
4. R4: revert to `startswith("move")` (P7 red); drop `move into backup dir` (P7 red).
5. R5: remove one exclude (AU1, AU3 red); revert shards to `--include` (AU2, AU4 red).
6. R6: revert to the old `stopped` expression (W14 red).
7. R8: `--files-from-raw` -> `--files-from` (A11 red); `<=` -> `<` for the cap (A13h red); hash `BudgetExceeded` -> SUSPECT (A13b red); last attempt's log only (W16 red); empty list runs the check (A13e red).
