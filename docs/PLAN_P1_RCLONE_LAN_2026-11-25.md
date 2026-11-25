# Phase 1 plan: rclone for the LAN leg, a simpler guard set, Prefect left alone (2026-11-25)

Author: Opus (Plan agent). Branch `rclone-engine`, base f79ff84 (`baseline-v1-overlap`). Binding for this phase; do not re-plan.
Labels: [CODE] read in code. [LAB] measured with rclone v1.74.3. [VERIFY] must be confirmed with the real binary in chunk C0b before any parser depending on it is built.

Roles: Opus plans and reviews. Sonnet writes code only. Haiku writes tests first (from the matrix rows) and runs test loops. Tests and code are written by different agents.

## 1. Scope

**In this phase**
1. The LAN mirror, its dry-run and a new nightly arrival check all use rclone (`deploy\bin\rclone.exe`, `resolve_binary`). Robocopy code, log parsing and keys are deleted.
2. One LAN dry-run per FY pass feeds every data guard: G1, G2, G3 with the `--max-delete` backstop, G5, NAS free-space. These go away: the robocopy `/L` dry-run; the robocopy-vs-rclone cross-check (flow.py:1196-1205) and `extras` check (flow.py:1350-1357); both destination snapshot walks; LAN `file_entries` and the walk-diff cross-check; the LAN Prefect markdown artifact.
3. Unchanged: WoL + smb_ready, SMB probe and canary, G4, source read probe, write probe + free space, slot/lock, budget/MISSED, FY passes and overlap, rollover, NAS shutdown policy.
4. A LAN evidence gate: COMPLETE needs a clean rclone verdict AND a clean arrival check.
5. Three cheap engine-neutral fixes: G3 small-tree floor (P3); "G4 host cannot be resolved" -> FAIL not BLOCK; P2 regex reorder (network failure no longer reported as a key problem).
6. A real-rclone audit-lab file for the LAN leg, local disk -> loopback admin share (`\\127.0.0.1\C$`, labconf `unc_nas`). Three existing pipeline lab files adapted.

**Deferred**
| Item | Reason |
|---|---|
| `--backup-dir` + age purge | Completed in Phase 1b (`WS-B`, `AAM_versions`) and hardened in `RF-1` (`rclone purge` per due run folder). |
| Copy-only run on a G3-only block (audit F.2.1) | Completed in Phase 1b (`WS-B`) and hardened in `RF-3` (truthful blocked mail) & `RF-4` (copy-only free-space counts moves). |
| Cloud guard trims (`--combined` replacing `rclone size` walks, drop G2 on cloud, probe A, write probe, merge verify checks) | Cloud leg is proven on the box. Small Phase 2 chunk using parsers built here. |
| Prefect retries on preflight/verify; cloud `max_attempts` | Prefect phase. |
| Notify fixes (P1, P8/R2, P7) | Not engine work. |
| Rollover re-running final backups (P5), new-FY audit, retention keeps last success | Separate logic. |
| Shadow trial on the box (step A dry-run vs real mirror, step B NAS test folder, cable pull) | Box and real NAS off-limits in this phase. Gate before deploying. Step A (review amendments M4, R11) also: records the wall time of the dry-run, of the size-only `rclone check` and of a hash check over a typical night's copies; counts read-only files on the NAS mirror, read-only (no attribute change). |
| S6 progress/stall watchdog in `run_tool` (kill rclone when its log stops growing) | Phase 2. Measured: `--timeout` / `--contact-timeout` have no effect on the local/SMB backend, so a hung SMB read is only caught by the hard timeout (S5 now names it). |
| S8a measure `--retries 3` vs 2 on a locked night | Shadow trial (see R2). |
| S8b `--fix-case` | Measure in the shadow trial: case-only renames on the source vs Samba. |
| S8c explicit `--delete-after` | rclone's default for `sync` already deletes after transfers; making it explicit is a builder change with a never/always-list test. Phase 2. |
| S8d rotating sample hash (a slice of the whole mirror each night) | Weekly audit covers it; Phase 2 if the audit runs out of budget. |
| S8e arrival/hash check timeout classified as "not verified" (SUSPECT) vs a distinct "check ran out of time" | Kept as SUSPECT (fail closed) in Phase 1; revisit with shadow-trial timings. |
| S8f stats-snapshot marking on killed runs (last periodic stats of a killed rclone shown as "at least") | Phase 2 (dashboard/metrics). |
| F1 (found in review amendments) `stats.deletes` counts each FAILED delete attempt (measured: 3 with only 2 files gone after a "Couldn't delete" with `--retries 2`) | Safe direction for the backstop (`rclone_deletes_of` lowers the next attempt's cap), but `files_deleted` in metrics overstates. Phase 2, with the metrics truth audit. |
| F2 the same `stopped = ... or (limited and timed_out)` pattern in `cloud_sync_task` (flow.py:966) | Cloud leg is proven on the box; fix with the Phase 2 cloud chunk using the R6 rule. |
| F3 `decide_lan_arrival` parses `line[2:].strip()`, so a NAS path with a leading/trailing space does not match the source file and is reported unverified | Fails closed (false SUSPECT only, never a false pass). Phase 2: `rstrip("\r\n")` with a row. |
| TCP 3 s probe NETWORK vs AUTH | Only the regex reorder now. |
| `--multi-thread-streams`, LAN bandwidth limit | Measure in shadow trial first. |
| Rewrite robocopy-era audit-lab files (w13, w5a/b/c, w11, w1a/b/c/d2, w12, w8, w9, w10, w16, w18) | Phase 2 lab sweep. Out of the Phase 1 gate, listed in handoff, no skip hacks. |
| Full `tools/livebox` adaptation | Next live-box phase; only compatibility edits now (C10c). |
| Removing duplicate SMB probe in preflight | Kept: one TCP connect when NAS is up; only reachability probe when WoL is off. |

**`lan.engine` switch: NO.** A robocopy fallback keeps ~800 lines and doubles the test matrix. Rollback = redeploy previous tag (`baseline-v1-overlap`) plus its config.yaml backup (old StrictModel rejects new keys, risk R7).

## 2. Target LAN flow (per FY pass, inside `_run_lan_pipeline`)

**Pre phase**
1. `health_check_task(lan)`: source checks; rclone binary instead of robocopy (health.py:261-263). SeBackupPrivilege check (flow.py:680-683) removed.
2. `wol_check_task`, then NAS FY folder steps (unchanged).
3. `lan_preflight_task` rewritten:
   a. SMB 445 probe (`lan_preflight._is_smb_reachable`, unchanged). Failure: coded `AAM-NAS-01` FAIL.
   b. Canary `is_file()`. OSError -> FAIL `AAM-NAS-01`.
   c. G1 first file: `guards.first_regular_file`. No file -> return `{"source_empty": True, "canary_present": ...}` with NO dry-run (G1 gives its own message).
   d. `lan_sync.plan_lan_mirror(...)`: one rclone dry-run. Not `ok` -> coded `AAM-LAN-01` (FAIL, retried next night, never BLOCKED).
4. `lan_guards_task(config, fy, plan, approval_id, run_id, already_deleted)`; `destination_files` param gone.
   - G1. G2: `plan.source_files` vs baseline. G4: if `forbid_local` and `resolve_host` is None -> coded `AAM-NAS-01` (FAIL).
   - G3: `plan.planned_deletes` vs `plan.dest_files`, with the floor. G5: `canary_present`, `dest_files > 0`.
   - Record gains `max_delete` (same rule as cloud: G3 approved -> `planned_deletes`; else `remaining_delete_allowance(...)`).
5. `lan_access_task(config, plan, run_id)`: source read probe, write probe, `check_destination_free_space(bytes_needed=plan.planned_bytes)`. None -> `AAM-NAS-05`, text "rclone dry-run byte count".

**Sync phase**
6. `_sync_with_retries` (unchanged): `run_once` = `lan_sync_task(config, artefacts_dir, max_delete=caps["max_delete"], budget=budget)` -> `lan_sync.run_lan_mirror`. `deletes_of` = `cloud_sync.rclone_deletes_of(r, caps["max_delete"])` (stats.deletes excludes track-renames moves [VERIFY]); `lan_deletes_of` deleted. `retryable` = `r.get("retryable") is True`. `remeasure` = preflight+guards+access updating `caps`. Budget binding -> `max_duration_s = resolve_max_duration_seconds(timeout, None)` adds `--max-duration ... --cutoff-mode SOFT`; exit 10 -> `LAN_PARTIAL` + `budget_stopped`, no arrival check.

**Verify phase** (new `phase = "verify"`, new `@task(name="lan-verify") lan_verify_task`)
7. Only when sync status is `LAN_COMPLETE`: `lan_sync.check_lan_arrival(..., changed_after_epoch=<wall clock just before last sync attempt>)`, `timeout = budget.cap(lan.verify_timeout_seconds, 0)`.
   - `BudgetExceeded` -> `LAN_PARTIAL`, `budget_stopped`, "copied but not verified: no time left" (`AAM-LAN-03`).
   - Not verified -> `LAN_SUSPECT` with reason, one alert (`AAM-LAN-04`), then `PartialRun`. (`decide_terminal_status` keeps SUSPECT, statuses.py:287-288.)
   - (Review amendment S3) When the size-only check is verified, `lan_verify_task` also hashes tonight's transfers: `lan_sync.check_lan_transfers_hash` over the `Copied (...)` objects of every sync attempt log of this pass (§2.1 second command). Skipped (verdict stays size-only, reason in metrics) when `lan.arrival_hash_transferred` is false, nothing was copied, the byte total is unknown or above `lan.arrival_hash_max_gb`. Hash not verified or timed out -> `LAN_SUSPECT` (`AAM-LAN-04`); no budget left for it (`budget.cap(lan.arrival_hash_timeout_seconds, 0)` raises) -> `LAN_PARTIAL`, `budget_stopped`, "copied and size-checked, but no time left for tonight's hash check" (`AAM-LAN-03`). Rows: `docs/TESTMATRIX_P1_REVIEW_2026-11-25.md` §8.

**Post phase**
8. `extended_metrics`: `stage_seconds`; `counts_source: "rclone"`; `tool_counts` (transfers, bytes, deletes, renames, errors, checks); `files_deleted`, `moves`, `deletes_held`, `failed_paths` (capped); `arrival` {verified, matching, missing, differ, changed_during_backup}; `total_files = arrival.matching` (ui.py:1902). Row: `files_copied = stats.transfers`, `bytes_copied = stats.bytes`, `files_failed`.
9. Shutdown/alert policy unchanged; only `LAN_COMPLETE` shuts the NAS down. PARTIAL alert text: "rclone exit N: K file(s) could not be copied: <paths>"; with `deletes_held`: "No deletions were made tonight (rclone deletes nothing after a read error); they run on the next clean night."

### 2.1 Exact rclone flags

`lan_sync.build_lan_rclone_command`, real sync:
```
rclone sync SRC DST --check-first --create-empty-src-dirs --skip-links --ignore-case
  --modify-window {lan.modify_window_seconds}s --transfers {lan.transfers} --checkers {lan.checkers}
  --retries {lan.retry_count} --retries-sleep {lan.retry_wait_seconds}s
  --use-json-log --log-level INFO --log-file LOG
  --exclude .AAM_TARGET_MOUNTED --exclude {permissions.PROBE_PATTERN}
  --exclude "System Volume Information/**" --exclude "$RECYCLE.BIN/**"
  [--track-renames]                         (lan.track_renames; strategy default "hash")
  --max-delete {N}                          (always on a real sync; 0 is emitted)
  [--max-duration {S}s --cutoff-mode SOFT]  (only when budget-limited)
```
Dry-run: same argv plus `--dry-run --combined COMBINED`, minus `--max-delete`, `--max-duration`, `--cutoff-mode`. Invariant tested: dry-run = sync + `--dry-run`.
G1 backstop: `require_source_files(source)` on the real sync only.

Arrival check:
```
rclone check SRC DST --one-way --size-only --ignore-case --skip-links --checkers {lan.checkers}
  --combined CHECK_COMBINED <same 4 excludes>
```
Plain stderr, no `--log-file`/JSON, so `cloud_verify.decide_cloud_verify_result` is reused unchanged.

Hash check of tonight's transfers (review amendment S3, step `rclone-lan-hash`, runs only after a verified size-only check):
```
rclone check SRC DST --one-way --files-from-raw LIST --skip-links --ignore-case --checkers {lan.checkers}
  --combined HASH_COMBINED
```
`LIST` = run-folder file `rclone-lan-hash-files` (UTF-8, one relative path per line) built by `lan_sync.transferred_files` from the sync JSON logs (`msg` starting `Copied (`: `Copied (new)`, `Copied (replaced existing)`). No `--size-only`, so the local backend reads both copies and compares MD5. No excludes: rclone refuses `--exclude` with `--files-from` (CRITICAL, exit 1, measured) and the list already passed the sync's excludes. `--files-from-raw`, not `--files-from`: the latter silently drops names starting with `#`/`;` and strips leading spaces (measured: such files were not checked). Decided by `decide_lan_arrival` with the same `changed_since` rule. Caveat: the NAS may serve the just-written data from its RAM cache, so this proves the SMB path and the NAS's view, not the disk surface; the weekly `integrity.audit_lan` stays the proof.

**Never passed** (each has a test): `--ignore-errors --size-only(sync) --checksum --ignore-checksum --local-no-check-updated --local-nounc --error-on-no-transfer --delete-excluded --immutable --ignore-existing --config`.

Exit 9: never expected (no `--error-on-no-transfer` on LAN). A no-change night is `LAN_COMPLETE` with `transfers == 0`. No new status; legacy `LAN_NO_CHANGES_COMPLETE` stays read-only.

### 2.2 `decide_lan_rclone_result(exit_code, log_text) -> dict` (pure)
Reuses `final_stats`, `scan_rclone_log_for_errors`, `max_delete_reached`, `rclone_retryable` from cloud_sync. Rules in order:
| # | Input | Status | Flags |
|---|---|---|---|
| 1 | exit not int (bool/None) or <0 | LAN_FAILED | abnormal |
| 2 | `max_delete_reached(log)` (exit 7) | LAN_FAILED | `max_delete_reached=True`, retryable False; flow turns into BLOCKED via `backstop_blocked` (`AAM-DEL-03`) |
| 3 | exit in {2,3,7,8,9} or unknown | LAN_FAILED | retryable False; 9 reason "unexpected on LAN" |
| 4 | exit != 0 and no final stats | LAN_FAILED | abnormal, retryable False |
| 5 | exit 0 and no final stats | LAN_SUSPECT | "no final stats: rclone did not finish" |
| 6 | exit 0, `stats.errors == 0`, no final-attempt ERROR | LAN_COMPLETE | reason "no changes" when transfers, deletes, renames all 0 |
| 7 | exit 0 with ERROR signals or `stats.errors > 0` | LAN_SUSPECT | |
| 8 | exit 10 | LAN_PARTIAL | `time_limit_stopped=True` |
| 9 | exit 5, stats present, `fatalError` not true | LAN_FAILED | `retryable=True` |
| 10 | exit in {1,4,6}, stats present, `fatalError` not true | LAN_PARTIAL | `files_failed = max(stats.errors,1)`, `failed_paths` = final-attempt ERROR objects of "Failed to copy", "partial file rename failed" (read-only NAS file, R11) and "Couldn't delete" (file open on the NAS), never `*.Fs` objects, rclone's `.<8 hex>.partial` suffix stripped (capped `lan.error_tail_lines`; review amendment S1), `deletes_held` = log has "not deleting files as there were IO errors" |
| 11 | `fatalError` true | LAN_FAILED | retryable False |

A locked file = row 10: LAN_PARTIAL with no deletions (fixture `test_support/rclone_logs/locked_file_exit1.jsonl`). PARTIAL is never retried at flow level.

### 2.3 Plan and arrival parsers
Added to `core/cloud_sync.py` (additive; cloud unchanged):
- `combined_counts(path) -> {"=","*","+","-","!","?"}` (streams, counts by first char).
- `planned_copy_bytes(log_text) -> int|None` (sum `size` over `skipped == "copy"`; any copy entry without int size -> None [VERIFY] so free-space fails closed).
- `failed_object_paths(log_text, limit)`; `deletes_were_held(log_text)`; `rclone_deletes_of(result, max_delete)` (template: cloud closure flow.py:1829-1834; cloud keeps its closure for now).
- (Review amendment S1) `failed_object_paths` accepts final-attempt ERROR/CRITICAL entries whose msg starts with `Failed to copy`, `partial file rename failed` or `Couldn't delete`, whose `object` is a non-empty str and whose `objectType` does not end in `.Fs`; applies `re.sub(r"\.[0-9a-f]{8}\.partial$", "", obj)`, then de-duplicates (log order) and caps.
- (Review amendment M3) With `--backup-dir` the dry-run reports a deletion as `"skipped":"move into backup dir"` and a replaced file as `"skipped":"move"` (measured). `count_planned_deletes` counts `"delete"` and `"move into backup dir"`; `count_planned_moves` counts only `skipped` starting with `"move to "` (today's `startswith("move")` would count deletions and replaces as renames under Phase 1b and hide them from G3).

`plan_lan_mirror` output: `source_files = = + * + +`; `dest_files = = + * + -`; `planned_deletes = count_planned_deletes`; `moves = count_planned_moves` (existing); `planned_copies`; `planned_bytes`. Any `!`/`?` line, exit != 0, ERROR signals or `stats.errors > 0` -> `ok=False`.

`decide_lan_arrival(exit_code, stderr, combined_text, changed_since)` (pure): `decide_cloud_verify_result` decides completion/termination. Verified only if: completion true and exit in {0,1}; no `!` lines; `differences == (+ lines) + (* lines)` (else fail closed) [VERIFY]; every `+`/`*` path p has `changed_since(p)` true. `changed_since` = `max(st_mtime, st_ctime) >= changed_after_epoch` on the source file (review amendment M2: no `- lan.modify_window_seconds` tolerance; a file last written before the attempt started was seen by that attempt, so a difference on it is real); OSError = not changed. Docstring note: on Windows with Python 3.12 `st_ctime` is the creation time (`== st_birthtime`, measured), so a file created during the backup counts even when its mtime is old. `matching` parsed from "N matching files". A summary with more "errors while checking" than differences is not verified (row A10).

## 3. Config
New keys (each added in the chunk whose code reads it, with `config_help` entry):
| Key | Default | Bounds | Read by |
|---|---|---|---|
| `lan.transfers` | 4 | 1..32 | builder (replaces `mt_threads`) |
| `lan.checkers` | 4 | 1..32 | builder, check |
| `lan.modify_window_seconds` | 2 | 0..10 | builder only (review amendment M2: no longer the arrival tolerance; LAN no longer reads `cloud.modify_window_seconds`) |
| `lan.error_tail_lines` | 100 | 1..10000 | decision, plan, alerts (LAN no longer reads cloud's) |
| `lan.verify_timeout_seconds` | 1800 | 60..14400 | arrival check |
| `guards.min_delete_floor_files` | 20 | 0..100_000 | `max_delete_allowance` (both legs) |
| `lan.arrival_hash_transferred` | true | bool | `flow.lan_verify_task` (review S3, chunk R8a). config_help: "Hash-check tonight's copied files" / "After the size check, the files copied tonight are read back from the NAS and compared by content." |
| `lan.arrival_hash_max_gb` | 50 | 0..10000 (GB = 1024**3, `permissions.GB`) | `flow.lan_verify_task`: above it the hash is skipped and recorded (`metrics.arrival.hash.reason`), size-only verdict kept. config_help: "Largest nightly copy that is hash-checked" / "On nights that copied more than this, only sizes are checked; the weekly safety check still compares content." |
| `lan.arrival_hash_timeout_seconds` | 3600 | 60..14400 | `lan_sync.check_lan_transfers_hash` via `budget.cap(..., 0)`. config_help: "Time for the nightly hash check" / "The hash check of tonight's copied files gives up after this long." |

Decision (S3 timeout): a new key, not a share of `lan.verify_timeout_seconds`. The size-only check scales with the file count of the whole tree (a walk); the hash check scales with tonight's bytes (both copies read in full). One number cannot be right for both, and a shared budget would let a slow walk silently starve the hash (or the reverse). Both are still capped by the run budget, and `budget.lan_post_reserve_minutes` must cover both (§6 gate step).

Kept, new meaning (descriptions updated): `lan.retry_count` -> `--retries`; `lan.retry_wait_seconds` -> `--retries-sleep`; `lan.rclone_measure_timeout_seconds` = dry-run timeout; `lan.track_renames` also applies to the copier; `lan.subprocess_timeout_seconds` = sync timeout; `lan.max_attempts`, `lan.retry_delay_seconds` = flow retries (exit 5 only); `budget.lan_post_reserve_minutes` covers the arrival check, the hash check of tonight's transfers and the verified NAS shutdown (review amendment S7: models/config.py description still says "destination walk", config_help label says only "switching the NAS off"; both fixed in chunk R7).

To `DEPRECATED_KEYS`: `"lan": {mt_threads, log_tail_kb, dry_run_timeout_seconds, use_backup_mode, privilege_check_timeout_seconds}`, `"maintenance": {orphan_robocopy_log_max_age_hours}`. Also remove from config_edit.py:56,:137; config_help.py:98-117,:250; config.example.yaml; deploy/probe_service_account.py:80-88; tools/livebox/rig.py:598-599.

## 4. Guard changes
| Change | Where | Now? |
|---|---|---|
| One dry-run returns `plan` {ok, error, exit_code, source_files, dest_files, planned_deletes, moves, planned_copies, planned_bytes, log_path, combined_path} + `canary_present`, `source_empty` from preflight | `lan_sync.plan_lan_mirror`, flow `lan_preflight_task` | YES |
| `lan_guards_task` takes `plan`; no `destination_files`, no robocopy `extras` checks; returns `max_delete` | flow.py:1319-1402 | YES |
| `lan_access_task` free space uses `plan.planned_bytes` | flow.py:1405-1417, permissions.py:181 | YES |
| G3 floor: `max_delete_allowance(dest, max_files, pct, min_floor_files)` = `max(0, min(max_files, max(min_floor, floor(dest*pct/100))))`; threaded through `g3_mass_delete`, `remaining_delete_allowance`, both guard tasks | guards.py:118-150, 325-349; flow.py:797-810 | YES (both legs) |
| G4 unresolvable host -> FAIL not BLOCK (pre-check in `lan_guards_task`; pure `g4` keeps defensive BLOCK) | flow | YES |
| P2: NETWORK pattern before AUTH; drop `oauth2: cannot fetch token` from AUTH (keep invalid_grant, 401, 403, forbidden) | rclone_errors.py:29-36 | YES |
| Copy-only on a G3 block | | Phase 1b |
| Cloud dry-run `--combined` replacing `rclone size` walks | | Phase 2 |

## 5. Work breakdown
Each chunk: Haiku writes tests FIRST from the matrix rows and shows them fail on old code (ambiguous rows go to Opus); Sonnet makes them pass without touching those tests; Haiku runs loops. Each chunk ends green on its targeted tests. Full suite must be green from C11 on (C8/C9 may leave unrelated LAN flow tests red until C11). Commit per chunk citing plan chunk ID.

| # | Chunk | Files (~lines) | Behaviour | Tests first | Acceptance | Who |
|---|---|---|---|---|---|---|
| C0 | Save test matrix | in this doc (§Test matrix) | - | - | exists | parent |
| C0b | Fixtures from real rclone | `test_support/rclone_logs/lan_*.jsonl`, `lan_*_combined.txt`, `lan_check_*.stderr.txt` | Script with `deploy\bin\rclone.exe`, local disk -> `\\127.0.0.1\C$\...`: clean sync, no-change sync, rename of 5 files, 3 deletes, `--max-delete 1` exit 7, dry-run + combined, check clean, check with 1 missing. Confirm every [VERIFY] (copy `size` field; combined has no folder lines; `stats.deletes` excludes renames; does `--max-delete` count moves; differences = + and *; "N matching files" format) | - | Fact sheet appended below. A [VERIFY] that proves false goes to Opus before C3 | Haiku |
| C1 | G3 floor | guards.py, models/config.py, config_help.py, flow.py (2 sites) (~40) | §4 | F1-F4 | targeted + test_m2_guards + test_b1_delete_allowance green | Haiku / Sonnet |
| C2 | P2 reorder | core/rclone_errors.py (~10) | §4 | E1-E7 | test_d4_transport_classifier, test_m2_rclone_errors green | Haiku / Sonnet |
| C3 | Shared parsers | core/cloud_sync.py (~80 additive) | §2.3 | P1-P6 on C0b fixtures | cloud tests unchanged and green | Haiku / Sonnet |
| C4 | LAN builder | core/lan_sync.py (`LAN_EXCLUDES`, `build_lan_rclone_command`), keys transfers, checkers, modify_window (~70) | §2.1 | M1-M5 | argv property test | Haiku / Sonnet |
| C5 | Exit decision | core/lan_sync.py `decide_lan_rclone_result`, key error_tail_lines (~80) | §2.2 | D1-D16 | every table row tested | Haiku / Sonnet |
| T0 | LAN fake | test_support/fake_tools.py `fake_rclone_lan(script)` patches `core.lan_sync.run_tool`; dispatches on step name (`rclone-lan-plan`/`-sync`/`-check`; list per name supports retries); writes `log_path`, the `--combined` file, stderr (~50) | - | self-test | - | Haiku |
| C6 | Wrappers | core/lan_sync.py `run_lan_mirror`, `plan_lan_mirror` (~110) | `run_tool` with step names above; spawn/timeout/log handling as `run_cloud_sync` | R1-R4, PL1-PL5 | - | Haiku / Sonnet |
| C7 | Arrival check | core/lan_sync.py `check_lan_arrival`, `decide_lan_arrival`; key verify_timeout_seconds (~110) | §2.3 | A1-A9 | - | Haiku / Sonnet |
| C8 | Preflight, guards, access | lan_preflight.py (delete `run_lan_dry_run`, add `check_lan_destination`); flow.py `lan_preflight_task`, `lan_guards_task`, `lan_access_task` + pre-sync calls; permissions.py text (~140) | §2 steps 3-5, §4 | G-1..G-12 | `lan_snapshot_before_task` no longer called | Haiku / Sonnet |
| C9 | Sync, verify, post wiring | flow.py: `lan_sync_task` -> `run_lan_mirror` + max_delete/budget; new `lan_verify_task`; post block (metrics, PARTIAL text); remove snapshot-after, record, publish calls; health_check_task + health.py (rclone binary for lan, no privilege check) (~150) | §2 steps 6-9 | W1-W13 | LAN pass makes exactly 3 `run_tool` calls (plan, sync, check), no robocopy argv | Haiku / Sonnet |
| C10a | Delete robocopy code | lan_sync.py robocopy section, lan_preflight robocopy imports, core/lan_manifest.py, flow.py (`lan_snapshot_*`, `lan_record_task`, `lan_publish_artifact_task`, `lan_deletes_of`, `cleanup_orphaned_robocopy_logs` call at :3439, imports), `permissions.check_backup_privilege`, `fake_tools.robocopy_log` | deletion + import fixes only (exempt from line cap) | K-del | `git grep -n robocopy -- core flow.py` leaves only allow-listed comments | Sonnet |
| C10b | Config deprecations | models/config.py DEPRECATED_KEYS + field removal, config_edit.py, config_help.py, config.example.yaml, deploy/probe_service_account.py (~60) | §3 | K1-K3 | test_config_validation, test_m4_config_example, test_m4_settings* green | Haiku / Sonnet |
| C10c | Peripheral strings | watchdog.py:198 (drop robocopy.exe), serve.py:56 text, statuses.py:116/226 comments, chaos_harness/scenarios*.py strings, tools/livebox/rig.py (`_strip_zb`, lane keys) (~40) | text/compat only | - | test_review2_rig, test_chaos_harness* green | Sonnet |
| C11 | Test sweep | files below | Run suite; classify failures: (a) patch of removed symbol/key: fix mechanically; (b) assertion about robocopy behaviour: delete only where a matrix row covers new behaviour, log in `agent_runs/p1_sweep.md`; (c) anything else: escalate | - | fast loop + slow file green | Haiku run, Sonnet fix |
| C12 | Audit lab | new `audit_lab/test_w25_rclone_lan.py`; adapt `test_w4_pipeline.py` (LAN case), `test_fy_overlap_lab.py` (drop `strip_zb` patch; :402 looks for UTF-8 rclone log), `test_w6b_rollover_nas_off.py` | L1-L11 | - | 4 files green with clean `PREFECT_HOME` | Haiku |

**Status 2026-11-25:** C10a-c and C11 are DONE (code `3483065`, tests `1adbae2`; sweep `fe48776`, `c7512ee`; log `agent_runs/p1_sweep.md`). Next: C12.

**Review amendments 2026-11-25 (owner-approved items M2, M3, M4, R11, S1, S2, S3, S4, S5, S7 of the Opus robustness review; S6, S8 deferred).** Status: PLANNED, nothing built. Reference rows and measured facts: `docs/TESTMATRIX_P1_REVIEW_2026-11-25.md`. M4 and R11 are doc-only (§6). Same process as §5: Haiku writes the tests first and shows them red on the current code (R2: red on the named mutant instead, the branch already exists); Sonnet writes the code and may not edit those tests (a wrong test goes back to Opus); Haiku runs the loops; `git diff --stat` per chunk is compared with the scope below; Opus reviews each chunk with the §11 mutation list. Commit per chunk, citing the R-ID and the review item. Rows marked "to be rewritten by the test agent under Opus" are the only existing tests whose expectations change.

Acceptance command form (from the repo root, `PYTHONPATH=.`): `C:\Users\malliath\Desktop\aam_backup\.venv\Scripts\python.exe -m pytest <files> -q -p no:cacheprovider -o addopts="" --timeout=600`.

| # | Item | Files (~lines) | Behaviour | Tests first (Haiku) | Code (Sonnet) | Acceptance |
|---|---|---|---|---|---|---|
| R1 | M2 | core/lan_sync.py `check_lan_arrival` (~6: threshold, docstring incl. st_ctime note) | `changed_since` threshold = epoch | A2b, A3b, A3c, A3d + the two rewritten tests in tests/test_p1_c7_lan_arrival.py (matrix §1) | yes | test_p1_c7_lan_arrival.py, test_p1_c9_lan_pipeline.py |
| R2 | S2 | tests/test_p1_c7_lan_arrival.py only (~20) | none (test-only) | A10, A10b | none | test_p1_c7 green; Opus: deleting the branch turns A10 red |
| R3 | S1 | core/cloud_sync.py `failed_object_paths` (~15); fixtures `test_support/rclone_logs/lan_readonly_dst_exit1.jsonl`, `lan_locked_dst_delete_exit1.jsonl` + LAN_FIXTURES.md entries (test agent generates) | §2.3 S1 rule; reason wording only after owner Q4 | D17-D17f in tests/test_p1_c5_lan_decision.py / test_p1_c3_parsers.py | yes | test_p1_c3, test_p1_c5, test_p1_c6, test_cloud_sync* |
| R4 | M3 | core/cloud_sync.py `count_planned_deletes`, `count_planned_moves` (~8, docstrings) ; fixture `lan_backupdir_dryrun.jsonl` (test agent generates) | §2.3 M3 rule (cloud never uses `--backup-dir`, so no cloud change) | P7, P7b, P7c in tests/test_p1_c3_parsers.py | yes | test_p1_c3, test_p1_c8, test_cloud_sync* |
| R5 | S4 | core/integrity.py `audit_lan` (~20; imports `lan_sync.LAN_EXCLUDES`, local import if a cycle appears) | matrix §5 rule, ordered `--filter` for shards | AU1-AU4 in new tests/test_p1_r5_audit_filters.py (AU3/AU4 real rclone local -> local) | yes | new file + test_integrity_audit.py, test_lan_canary_integrity.py, test_integrity_tool_runner.py |
| R6 | S5 | flow.py `lan_sync_task` (~15) + post-block verify text (~2); core/cloud_sync.py `MAX_DURATION_MARGIN_S` constant used by `resolve_max_duration_seconds` (~3) | matrix §6 rule (`killed_after_cutoff`, `AAM-LAN-04`, LAN_PARTIAL) | W14, W14b, W14c in tests/test_p1_c9_lan_pipeline.py | yes | test_p1_c9, test_characterization_core.py, test_cloud_sync_comprehensive.py |
| R7 | S7 | models/config.py (`lan_post_reserve_minutes` description), core/config_help.py label/help (~4) | text | K4 in new tests/test_p1_r7_reserve_text.py | yes | new file + test_m4_settings*, test_config_validation.py |
| R8a | S3 keys + list | models/config.py 3 keys (~12), core/config_help.py (~6), core/lan_sync.py `transferred_files` (~25); fixture `lan_replaced.jsonl` | matrix §8 keys and `transferred_files` | K5, H1-H6 in new tests/test_p1_r8_hash.py | yes | new file + test_config_validation.py, test_m4_settings* |
| R8b | S3 check | core/lan_sync.py `check_lan_transfers_hash` + shared `changed_since` helper used by both checks (~55); test_support/fake_tools.py `LAN_RCLONE_STEPS` + `"rclone-lan-hash"` (test agent, ~2); fixtures `lan_hash_clean.*`, `lan_hash_md5diff.*` | §2.1 second command | A11, A12, A12b, A12c, A13 (wrapper part), T1 self-test | yes | test_p1_r8_hash.py, test_p1_t0_fake_rclone_lan.py, test_p1_c7 |
| R8c | S3 task | flow.py `lan_verify_task` (~45) | matrix §8 task rules (skip reasons, cap, budget, OSError) | A13 (task), A13h, A13i via `lan_verify_task.fn` with fakes | yes | test_p1_r8_hash.py |
| R8d | S3 wiring | flow.py `_run_lan_pipeline` run_once log collection, hash budget branch, metrics (~25) | matrix §8 flow rules | W15, W16, A13b-A13g + rewrites of W1/W7/W10 (matrix §8) | yes | test_p1_c9, then the mechanical sweep below |
| R9 | sweep | test files using `fake_rclone_lan` in a flow | only "no spec for step 'rclone-lan-hash'" fixed (add a clean hash spec); log `agent_runs/p1_review_sweep.md`; anything else to Opus | - | Haiku runs, Sonnet fixes | fast loop + slow file (CLAUDE.md) green |
| R10 | labs | audit_lab/test_w25_rclone_lan.py (~120) | L12, L13, L13b, L14 (matrix §9) | Haiku writes and runs (lab rows are the tests) | none expected; a real-rclone surprise goes to Opus | w25 file green with a clean `PREFECT_HOME`; L1-L11 still green |
| R11 | review | - | Opus: full-suite result, `git diff --stat` vs scope, matrix §11 mutations, `ruff check` on changed files | - | - | gate item 7 (§6) |

### Test files
- **Rewrite** (10): test_lan_sync.py (M,D,R), test_lan_result_contract.py (D), test_lan_preflight_comprehensive.py (G-1..4, PL), test_lan_canary_integrity.py (G-2, L9 unit form), test_track_renames_g3.py, test_track_renames_review_fixes.py (G-6, cross-check gone), test_m2_guard_wiring.py LAN part, test_m2_preflight_permissions.py (drop privilege; G-11), test_xr_sync_attempts.py LAN part (W3,W7), test_f1_run_artefact_names.py.
- **Adapt**: test_m3_metrics.py LAN part (W1), test_publish_artifact.py (drop LAN artifact).
- **Delete** (3): test_lan_sync_comprehensive.py, test_lan_manifest_comprehensive.py, test_fix_22_2_1_snapshot_before.py.
- **Mechanical**: test_flow_orchestration, test_flow_status_semantics, test_m3_terminal_status, test_a2_interrupted_run_status, test_a4_final_lan_no_shutdown, test_a7_pending_nas_folder, test_fy_overlap_pipelines, test_lan_shutdown_policy, test_m1_full_logs, test_m2_approval_workflow, test_m3_alerts, test_m4_shutdown, test_task_retry_config, test_cloud_evidence_gate, run_chaos_suite, test_batch3_fixes, test_bug_reproductions, test_production_scenarios, test_logic_soundness_audit, test_fallback_defaults_from_model, test_chaos_harness*.

### Test matrix (U unit, F flow with `fake_rclone_lan`, L audit lab real rclone)
**M builder (U):** M1 exact flag set §2.1. M2 never-list holds for any config. M3 dry-run argv = sync argv + `--dry-run`/`--combined`, minus `--max-delete`/`--max-duration`/`--cutoff-mode`. M4 empty source raises ValueError on real build only. M5 `--max-delete 0` emitted.

**D decision (U):** D1 exit 0 clean stats -> COMPLETE, counts map through. D2 no-change night COMPLETE reason "no changes". D3 exit 0 without stats -> SUSPECT. D4 exit 0 with ERROR lines or `stats.errors>0` -> SUSPECT. D5 earlier attempt failed then "Attempt 2/3 succeeded" -> COMPLETE. D6 locked fixture -> PARTIAL, files_failed 1, failed_paths ["locked"], deletes_held True. D7 `fatalError` -> FAILED. D8 nonzero exit without stats -> FAILED not retryable. D9 exit 5 -> FAILED retryable. D10 max-delete -> FAILED `max_delete_reached`. D11 exit 7 without max-delete -> FAILED. D12 exits 2,3,8,9 -> FAILED. D13 None, -1, True -> FAILED. D14 exit 10 -> PARTIAL `time_limit_stopped`. D15 exit 1 with "not enough space" -> PARTIAL. D16 exit 1 without "not deleting" -> `deletes_held` False.

**P parsers (U):** P1 combined counts, source_files, dest_files. P2 names with spaces, unicode, CRLF; unknown first char is `?` -> plan not ok. P3 planned bytes sum, None when a size missing, 0 when no copies. P4 failed paths from final attempt only, capped. P5 `rclone_deletes_of` returns stats.deletes, else cap after a backstop stop, else None. P6 planned delete count on rename fixture excludes moves.

**R wrapper (U):** R1 rclone missing -> FAILED `AAM-SYS-04`. R2 timeout -> FAILED `timed_out` (+stats if log has them). R3 result dict has every key. R4 full log kept under step `rclone-lan-sync`.

**PL plan (U):** PL1 clean plan dict. PL2 nonzero exit, ERROR lines or `stats.errors` -> `ok=False`. PL3 missing combined file or `!` lines -> `ok=False`. PL4 timeout/spawn error -> `ok=False`. PL5 argv equals M3 dry-run argv.

**A arrival (U):** A1 clean check verified, `matching` parsed. A2 `+` file with ctime >= sync start verified, `changed_during_backup` 1. A3 same file but older not verified, path listed. A4 old `*` file not verified. A5 no summary not verified. A6 `!` lines or exit >= 2 not verified. A7 count mismatch not verified. A8 argv. A9 timeout not verified.

**G preflight/guards (F/U):** G-1 SMB probe fails: FAIL `NAS-01`, no rclone call. G-2 canary missing: G5 blocks `NAS-09` (dest_files 0) or `NAS-10` (dest_files>0). G-3 empty source: G1 blocks, no dry-run. G-4 plan not ok: FAIL `LAN-01`, not BLOCKED. G-5 within allowance passes with `max_delete` = what is left; over blocks `DEL-01`. G-6 316 moves and 0 deletes pass. G-7 no destination walk called. G-8 G2 uses `plan.source_files`. G-9 unresolvable host with `forbid_local` is FAIL; loopback still blocks `NAS-07`. G-10 approved G3 -> `max_delete = planned_deletes`. G-11 free space uses `planned_bytes`; None -> `NAS-05`. G-12 no robocopy argv anywhere.

**F floor (U):** F1 allowance(0..9 files)=floor; (1000)=100; (100k)=500; floor 0 gives old values; floor above `max_files` capped. F2 G3 and backstop use same function (property test gains the floor). F3 key bounds. F4 cloud guard task passes the floor.

**E classifier (U):** E1 oauth wrapper + dial tcp i/o timeout -> NETWORK. E2 connection refused -> NETWORK. E3 invalid_grant -> AUTH. E4 googleapi 403 -> AUTH. E5 lookup no such host -> DNS. E6 x509 -> TLS. E7 file named "403 Smith.pdf" is not AUTH.

**W pipeline (F):** W1 happy path COMPLETE; exactly 3 calls in order plan, sync, check; row and metrics fields of §2 step 8. W2 locked file -> PARTIAL, one alert naming file + "No deletions...", no NAS shutdown, `LAN-02`. W3 backstop stop -> BLOCKED, approval request, `DEL-03`, no retry. W4 G3 block in the plan -> BLOCKED, sync never called. W5 real missing file at arrival -> SUSPECT, alert, `LAN-04`, no shutdown. W6 change during backup only -> COMPLETE + metric. W7 exit 5 then 0: remeasure, second attempt `max_delete` lowered by attempt 1's deletes, COMPLETE, 2 attempts recorded. W8 budget-limited: `--max-duration` passed; exit 10 -> PARTIAL `budget_stopped`, no check. W9 no budget for the check -> PARTIAL `LAN-03`. W10 FY overlap: two passes, each own 3 calls on its FY destination; shutdown rules unchanged. W11 health checks rclone binary, no `whoami /priv`. W12 run row written on every exception path. W13 no LAN `file_entries` rows.

**K config (U):** K1 each deprecated key loads with warning and is dropped. K2 new-key defaults/bounds. K3 every LanConfig field has help; config.example.yaml loads. K-del removed symbols gone.

**L audit lab (real rclone, local disk -> `\\127.0.0.1\C$`):** L1 200 files, unicode names, empty folder -> COMPLETE; empty folder exists at destination; arrival verified; MD5 equal. L2 unchanged rerun COMPLETE, transfers and deletes 0. L3 locked file (ctypes `CreateFileW`, share mode 0; not `msvcrt.locking`, weak per audit_lab/test_w5c_ops.py:285-288) + 3 deleted source files: PARTIAL, `failed_paths` names it, `deletes_held`, the 3 files still at destination; after closing handle rerun COMPLETE and the 3 are gone. L4 rename folder of 5 files: plan moves 5, deletes 0; sync renames 5, deletes 0. L5 3 deletes: plan 3, sync 3. L6 path > 260 chars copied and verified. L7 plan, delete 3 more source files, `run_lan_mirror(max_delete=1)`: exit 7, `max_delete_reached`, at most 1 deleted. L8 dry-run facts: planned bytes = sum of new file sizes; no folder lines in combined; marker not in combined; destination-only file shows `-`. L9 marker and probe file never copied or deleted. L10 deleting one destination file behind rclone's back -> arrival unverified, missing 1. L11 empty folder removed from source is removed from destination.

## 6. Risks and Phase 1 gate
- R1 SMB/NAS behaviour not provable here (`\\?\UNC` long paths on Samba, NAS mtime precision, case-only renames, `System Volume Information/**` on a real `D:\` root). Loopback proves only the Windows UNC path. Mitigation: shadow step A (dry-run against real mirror, no writes) before any box deploy.
- R2 A file open every night = no deletions every night; mirror grows. Visible as nightly PARTIAL naming the file. `lan.retry_count=3` makes rclone repeat the sync 3x on a locked night; measure, lower to 2 if too slow.
- R3 `--track-renames` hashes over SMB; `--check-first` memory at 1-2.5M files. Measure in step A.
- R4 Killed run leaves at most `transfers` `*.partial` files; next night deletes them (count toward G3, small).
- R5 Losing a guard while deleting code: W1 and G-5..G-10 assert guard names in the record. Opus review runs mutation checks: exit 1 -> COMPLETE, floor `max`->`min`, arrival comparator `>=`->`>`, dry-run/sync argv equality, removing the final-attempt ERROR scan. Each must turn a test red.
- R6 Exit 1 also covers destination errors: NAS full mid-run = PARTIAL not FAILED. Accepted.
- R7 Rollback: old code rejects new keys; restore config.yaml with the old tag. Do not add new keys to the box config until the switch.
- R8 Robocopy-era lab files stay red until Phase 2; listed in handoff.
- R9 Lost NTFS metadata (ACLs, attributes, creation times, `/ZB`). Accepted by owner's rclone-only decision; record in handoff.
- R10 rclone prints "Config file not found - using defaults" at NOTICE; a D-row fixture includes it so it never counts as an error.
- R11 Read-only files on the NAS mirror (review amendment). Robocopy copied the read-only attribute to the NAS; rclone does not copy attributes but cannot REPLACE a read-only destination file: `partial file rename failed: rename <file>.<8 hex>.partial <file>: Access is denied`, then "not deleting files as there were IO errors", exit 1 (measured on the loopback share). Every night that file's source changes: `LAN_PARTIAL`, all deletions held, the sync repeated `lan.retry_count` (3) times. Deleting a read-only destination file works (measured), so only changed files are hit. Mitigation now: S1 names the file in the PARTIAL alert (row D17, lab L12). Shadow step A counts read-only files on the mirror, read-only. A one-time `attrib -R /S` of the mirror (or a per-file clear) is an OWNER decision because it touches backup data; it is NOT built in Phase 1. On Samba the attribute depends on `map readonly` / `store dos attributes`, so the count on the real NAS is the number that matters.

**Gate (Phase 1 done when):**
1. Full unit suite green (fast loop + slow file per CLAUDE.md).
2. `git grep -n -i robocopy -- core flow.py models` returns only allow-listed comments (DEPRECATED_KEYS notes, UTF-16 support_bundle reader for old logs).
3. Targeted audit lab green: `test_w25_rclone_lan.py`, `test_w4_pipeline.py` (LAN case), `test_fy_overlap_lab.py`, `test_w6b_rollover_nas_off.py`.
4. `ruff check` clean on changed files.
5. Opus diff review incl. R5 mutation checks passes; `git diff --stat` per chunk within scope.
6. `SESSION_HANDOFF.md` and the fact sheet updated; deferred list copied into the handoff.
7. Review amendments R1-R10 done (status line below), their test matrix rows green, R5 mutation list of `docs/TESTMATRIX_P1_REVIEW_2026-11-25.md` §11 red on each mutant.

**Before the box switch (review amendment M4, timeouts; doc only, measured in shadow step A):**
1. Record the wall time of the LAN dry-run (`rclone-lan-plan`), the size-only arrival check (`rclone-lan-check`) and the hash check of a typical night (`rclone-lan-hash`) against the real mirror.
2. Set `lan.rclone_measure_timeout_seconds` >= 3x the measured dry-run, `lan.verify_timeout_seconds` >= 3x the measured size-only check, `lan.arrival_hash_timeout_seconds` >= 3x the measured hash check (each within its bounds; a value above the bound is an owner question, not a bound change).
3. Check `budget.lan_post_reserve_minutes * 60` >= measured check + measured hash + `wol.shutdown_verify_timeout_seconds`, with margin; otherwise raise it (owner sees the number) so a budget-limited night still verifies and shuts the NAS down.

## 7. Owner questions (none block Phase 1; all needed before the box switch, Phase 1b)
1. Copy-only on a G3-only block (new/changed files copied, deletions wait for approval)? Default yes, Phase 1b, both legs.
2. `--backup-dir` on the NAS: how many days, how much space? Default 60 days, Phase 1b before the switch.
3. Small-tree floor of 20 files (min of 500, max of 20 or 10%). Default 20 (built in C1).
4. (Review amendments) PARTIAL wording: with S1 a PARTIAL can name a file that was not REMOVED on the NAS (open there) or not REPLACED (read-only there). Today's reason says "K file(s) could not be copied" and `AAM-LAN-02` advises "close programs (Tally, Excel)", which is wrong for those two. Proposed default: reason "K file(s) could not be copied or updated on the NAS: <paths>" in R3; fault text unchanged until the notify work. Needs a yes before R3 changes the reason text (R3 builds only the paths otherwise).
5. (R11) One-time `attrib -R` of the NAS mirror after shadow step A counts the read-only files: yes/no (touches backup data; not built).

## Fact sheet (C0b output goes here)

C0b run 2026-11-25, rclone v1.74.3, fixtures in `test_support/rclone_logs/lan_*` (commands and exit codes in `test_support/rclone_logs/LAN_FIXTURES.md`). Source local, destination `\\127.0.0.1\C$` (loopback works; no NAS touched).

- **(a) YES.** Dry-run JSON copy entries carry an int `size`: `{"level":"notice","msg":"Skipped copy as --dry-run is set (size 1.172Ki)","skipped":"copy","size":1200,"object":"Ünïcode café.txt",...}` (`lan_clean_dryrun.jsonl`). All 12 copy entries have int size. Folders appear as `"skipped":"make directory"` with no size. Sum of the copy sizes = 13062 = dry-run `stats.bytes`, so planned bytes = sum of sizes = stats.bytes.
- **(b) Files only.** `lan_clean_combined.txt` has 12 `+` lines, all files. Folders (`emptydir`, `photos`, `sub`, `sub/deep`, `sub/empty2`) are in the JSON as `make directory` but not in combined. Empty folders are still created at the destination (`emptydir`, `sub/empty2` present after sync).
- **(c) Stats `deletes` excludes moves; renames are separate.** `lan_rename.jsonl` final stats: `"renames":5,"deletes":0,"deletedDirs":1`, text "Renamed: 5", "Server Side Moves: 5". `lan_mixed.jsonl` (5 moves + 2 deletes): `"deletes":2,"renames":5`. `deletes` also excludes dirs (`deletedDirs`).
- **(c2) CONTRADICTS PLAN: combined `-` lines include the moved sources.** `lan_rename_combined.txt` has 5 `+ photos_renamed/pN.jpg` AND 5 `- photos/pN.jpg`, so `-` count = 5 with 0 deletes. `lan_mixed_combined.txt` has 5 `+`, 7 `-` (5 moves + 2 deletes) while the dry-run stats say `deletes 2, renames 5`. So `planned_deletes = count_planned_deletes` must NOT be a `-` line count: take `stats.deletes` from the dry-run JSON (or `-` minus moves). Likewise `planned_copies` from `stats.transfers` (`lan_mixed_dryrun`: transfers 0, bytes 7515 = sizes of the 5 renamed files only), not from `+` count. `source_files = = + * + +` and `dest_files = = + * + -` still hold: rename case 12 = 7 `=` + 5 `+` = 12 source; 7 `=` + 5 `-` = 12 dest.
- **(d) `--max-delete` does NOT count moves.** `lan_mixed.jsonl` with `--max-delete 2`, 5 moves + 2 deletes: exit 0, `deletes 2`, `renames 5`. Max-delete consistent with stats.deletes.
- **(e) Check: `differences found` = count of `+` plus `*` lines; "N matching files" format matches plan.** Clean: 0 `+`/`*` lines, stderr `NOTICE: Local file system at X:/rig/dst: 0 differences found`, `...: 9 matching files`; combined has 9 `=` lines. Missing (`lan_check_missing`): 1 `+` line `sub/deep/c.txt`, `...: 1 differences found`, plus `ERROR : sub/deep/c.txt: file not in ...`, `1 files missing`, `1 errors while checking`, `Failed to check: 1 differences found`, `...: 8 matching files`. Sizediff: 1 `*` line `photos_renamed/p1.jpg`, `ERROR : photos_renamed/p1.jpg: sizes differ`, `1 differences found`, `8 matching files`. The `N matching files` and `differences found` lines are NOTICE on **stderr** (rclone's `--combined` goes to its file). A parser should match `: (\d+) matching files$` and not treat "errors while checking" as a pass. No `!` lines in any combined.
- **(f) Exit codes seen:** sync/dry-run 0 (clean, nochange, rename, deletes, mixed, marker dry-runs); check clean 0; check missing 1; check sizediff 1; `--max-delete 1` with 3 pending deletes **7** (`"Got fatal error on delete: --max-delete threshold reached"` x2+, `Deleted` 1 file, `"fatalError":true`, `"errors":3`, `"Cancelling sync due to fatal error: failed to delete 2 files"`). No exit 2, 5, 9 or 10 in C0b. Clean/nochange/rename/deletes logs have zero `level:"error"` lines.
- **(g) YES, every run:** `{"level":"notice","msg":"Config file \"X:/rclone.conf\" not found - using defaults"}` in JSON logs; `NOTICE: Config file "..." not found - using defaults` on check stderr. Notice level, so R10 holds: a parser must ignore it.
- **(h) Marker and probe file:** dry-run with the 4 excludes removed: combined has `+ .AAM_TARGET_MOUNTED`, `+ .aam_write_probe_ab12cd`, `+ data.txt`. With the excludes: only `+ data.txt` (`lan_h_marker_*_combined.txt`). Excludes work for dry-run (check with a marker in source was NOT tested in C0b).
- Other: the `--track-renames` dry-run logs `Making map for --track-renames`; `deletedDirs` counts the old folder removal on rename (`photos` removed at destination).

## C0b consequences for the build (parent note, 2026-11-25)
- `planned_deletes` and `moves` come from the dry-run JSON log (`count_planned_deletes` / `count_planned_moves`), NEVER from `-` lines of `--combined` (a track-renames move shows as `+ new` AND `- old`). `planned_copies` = count of JSON `skipped == "copy"` entries (not `+` lines); `planned_bytes` = sum of their `size` (int, confirmed). `source_files` / `dest_files` formulas from combined still hold.
- Combined lists files only (no folders). `--max-delete` does not count moves. `stats.deletes` excludes moves.
- Arrival check: "N matching files" and "N differences found" are NOTICE lines on STDERR; a missing file also prints "N errors while checking" + "Failed to check" and exits 1, so the parser must not treat that as a pass beyond the `+`/`*` explained-by-changed-since rule.
- NOTICE "Config file ... not found - using defaults" appears on every run (R10 confirmed).

## Owner decisions (2026-11-25)
1. Copy-only on a G3-only block: YES (Phase 1b, both legs).
2. `--backup-dir` on the NAS (deleted AND replaced files, same relative path, dated folder, same share, outside the mirror root): YES, retention 91 days (Phase 1b). Space growth from replaced daily-changing files to be measured in the shadow trial; space check must include the folder.
3. Small-tree floor 20: OK (chunk C1).

## Phase 1b: copy-only on block, `--backup-dir`, approval scenarios (owner-approved 2026-11-25; build after Phase 1 gate, tests FIRST)

Design rules
- Copy-only on a G3-only block: `rclone copy` (same flags, same excludes), status stays `*_BLOCKED`, row says "copied N, deletions held". G1/G4/G5 blocks still stop everything.
- Never auto-approve on a timeout (owner rule: never auto-proceed). A pending approval escalates by reminders (ladder day 1, 2, 4, 7, then weekly; uses the notify fix deferred from P1) and the dashboard shows "N deletions waiting, D days".
- `--backup-dir <share>\AAM_versions\<YYYY-MM-DD_HHmm>_<FY>\` : per RUN and per FY pass (never just the date), same share, outside the mirror root, so a retry, a second pass or a manual run can never overwrite an earlier save. Applies to deleted AND replaced files (also with `copy`). Retention: 91 days counted from the run folder date (= the day of deletion, not the day the file left the source), purged by `rclone delete --min-age 91d` + `rmdirs`; config keys `lan.versions_enabled`, `lan.versions_folder`, `lan.versions_retention_days` (default 91, bounds 7..3650). Space check counts the folder; its size is shown in the weekly report.
- An approval covers only the count it was granted for (existing `evaluate_approval`); a larger plan blocks again.

Test rows (written first, by the test agent)
- V1: G3-only block -> `copy` is run, new + changed files arrive, no deletion, status BLOCKED, row "deletions held N".
- V2: G1/G4/G5 block -> nothing runs (no copy-only).
- V3: block stays open 30 simulated nights: every night copies new files, never deletes, never auto-approves, reminders follow the ladder (no daily spam), dashboard count/age correct.
- V4: operator approves after 40 nights: deletions run once, deleted files are in `AAM_versions\<that run>\<same relative path>`, mirror then matches source.
- V5: approved for N, tonight's plan N+1: blocks again.
- V6: file deleted on night 1, same path recreated and deleted again night 2: both versions exist in two different run folders, nothing overwritten.
- V7: two runs on the same day (retry / second FY pass / manual): distinct run folders, no overwrite.
- V8: a replaced (changed) file: the old version is in the run folder at the same relative path; the mirror has the new one.
- V9: retention: run folder older than 91 days purged, younger kept; folder dated at deletion time, not at source-removal time; the purge never touches the mirror root.
- V10: backup-dir inside the mirror root or on another share is refused at config validation / preflight.
- V11: free-space check counts the versions folder growth; low space -> FAIL before the sync.
- V12: real rclone local->UNC (audit lab): deletes + replaces land in the run folder with identical relative paths and content.
- V13 (review amendment M3; counters fixed now in chunk R4): with `--backup-dir` the plan counts `move into backup dir` as deletes and only `move to ...` as moves (P7); `--max-delete` counts backup-dir deletions (measured: exit 7 after 1 of 3) and not replace moves (measured: `--max-delete 0` + 1 replace = exit 0); metrics `moves` must not be `stats.renames` under backup-dir (it also counts backup moves: measured 6 for 3 renames + 1 replace + 2 deletes); a replaced file logs `Copied (new)`, so the S3 hash list still holds it; the space check counts the sizes of `"skipped":"move"` (replaced) and `move into backup dir` entries as versions-folder growth. Rows: `docs/TESTMATRIX_P1_REVIEW_2026-11-25.md` §10.
