# Plan: fixes from the code audit (2026-11-25, branch rclone-engine)

Source: code-only audit of core/, flow.py, models/config.py by three reviewers (FY rollover/overlap, rclone cloud leg, LAN leg/orchestration/alerts).
Finding 1 was re-verified by hand. Findings 2 and 3 were reproduced by the cloud reviewer with `deploy\bin\rclone.exe` (v1.74.3) in scratch folders. All other findings are from tracing code: **every task starts with a "confirm the finding" step; if it does not reproduce, stop and report, do not fix.**

## 0. Rules for whoever executes this (binding, from CLAUDE.md)

- No product change without the owner's go. Tasks marked **[DECISION]** need an owner answer first.
- Build process: one small chunk = one commit. **Test agent and code agent are different agents.** The test agent writes tests from this plan only and must show them FAILING on the current code; the code agent makes them pass and may not edit, weaken, skip or xfail those tests. Haiku builds, Sonnet fixes after 2 failed Haiku attempts, Opus reviews/decides interfaces.
- Scope guard: compare `git diff --stat` with the "Scope" line of each task. Extra files or much larger diffs go to Opus review before they count.
- Every tunable is a config key (pydantic default + bounds) added in the same commit as the code that reads it. No literals at call sites.
- Reuse `run_tool`, `statuses`, `ManifestDB.from_config`, `notify`. No new dependencies.
- Never power off this PC or the NAS in tests; use fakes. Never touch the live schedule on the box.
- Before editing any function run GitNexus impact (`node .gitnexus/run.cjs impact "<symbol>" --direction upstream --repo .`) and report callers/risk. Run `detect-changes` before each commit.
- Commit message ends with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (or the model that actually wrote it) and cites the task ID (AF-n).
- Test commands (venv interpreter, never uv at runtime):
  - fast: `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600 -m "not slow"`
  - single file: `.venv\Scripts\python.exe -m pytest tests/<file>.py -q -p no:cacheprovider -o addopts=""`
  - slow calendar file only when FY code changes: `... --timeout=1200 -m slow`
- Document each finding/decision in the newest session doc (owner rule) and tick tasks here when committed.

## 1. Order and dependencies

```
Phase A (cloud copy-only path)   AF-1 -> AF-2 -> AF-3        (AF-2 is hidden behind AF-1; do in this order)
Phase B (FY safety)              AF-4, AF-5 [DECISION], AF-6, AF-7
Phase C (LAN / shutdown safety)  AF-8, AF-9, AF-10
Phase D (hardening)              AF-11 .. AF-14
Phase E (low items, cleanup)     AF-15 .. AF-17
```
Each Phase ends with: full fast suite green, Opus review of the diff including mutation checks of the key condition(s), then next Phase.
Phases A, B, C touch different modules and may run in parallel by different agents, EXCEPT both A and C touch `flow.py`; serialise commits to `flow.py` (rebase between).

---

## Phase A: cloud copy-only and renames

### AF-1 (HIGH) `is_g3_only_block` never fires
- **Finding:** `core/guards.py:326-336` reads `record["blocked"]` and `r["passed"]`. `enforce()` (guards.py ~300-323) builds the record from `asdict(GuardResult)`, which has `verdict` but not `passed` (a property, line ~59), and nothing writes `blocked`. Callers: `flow.py:1781`, `flow.py:2186` pass `gb.record` (GuardBlocked.record). Existing tests hand-build dicts with the wrong keys (`tests/test_b4_copy_only.py`, `test_rf3_*`, `test_rf4_*`).
- **Fix (code agent):** make `is_g3_only_block` test the real shape: record is a dict; a result "failed" when `r.get("verdict")` is the BLOCK value (use the same constant/enum `GuardResult.passed` uses, do not re-type the string); `blocked` key not required. Keep: failing set minus `approved_guards` must equal exactly `{"G3_MASS_DELETE"}`. Do not change the `enforce()` record shape (interface) unless Opus approves.
- **Tests (test agent, new file `tests/test_af1_g3_only_block.py`):** build the record by calling the real `enforce()` with fake `GuardResult`s / a real `GuardBlocked`, never a hand-written dict:
  1. only G3 fails -> True
  2. G3 + G1 fail -> False
  3. G3 fails but G3 in `approved_guards` and nothing else fails -> False (nothing unapproved failing)
  4. only G2 fails -> False
  5. None / non-dict / empty results -> False
  6. end-to-end: LAN and cloud pipeline with a G3-only GuardBlocked from the real `enforce()` enter the copy-only branch (assert `max_delete=0` sync is invoked and no deletes occur) via `fake_run_tool`.
  Also update the old hand-built-dict tests to use the shared real-shape helper (test agent only; allowed because the old shape was wrong, list every changed test in the commit message).
- **Acceptance:** tests 1-6 fail on old code, pass on new. Mutation check (Opus): changing `== {"G3_MASS_DELETE"}` to `>=` or dropping the approved-guards subtraction makes a test fail.
- **Scope:** `core/guards.py` (~10 lines), tests only otherwise. Flag any `flow.py` change.

### AF-2 (HIGH) copy-only passes `--track-renames` to `rclone copy`
- **Finding:** `core/cloud_sync.py:569-573` adds `--track-renames` in copy mode; cloud default `track_renames=True`. rclone logs `level=error "Ignoring --track-renames as it doesn't work with copy or move, only sync"`; `scan_rclone_log_for_errors` turns it into CLOUD_SUSPECT (exit 0) or CLOUD_FAILED (exit 9). LAN builder already guards (`core/lan_sync.py:94`) - mirror that.
- **Confirm first:** run the command builder in copy mode and assert the flag is present; optionally reproduce with `deploy\bin\rclone.exe copy` between two scratch dirs.
- **Fix:** in the cloud command builder, add `--track-renames` (and any rename-only flag, e.g. `--track-renames-strategy`) only when mode is sync.
- **Tests:** (a) builder unit test: copy mode -> no `--track-renames*` in argv; sync mode with config true -> present; sync with false -> absent. (b) `scan_rclone_log_for_errors` regression: not needed after fix, but add a test that a copy-only run via `fake_run_tool` returning exit 0 with a clean log yields success, `copy_only.error` empty, and `copied` populated. (c) one real-rclone test (new, marker-guarded to skip if `deploy\bin\rclone.exe` missing): `rclone copy` with the builder's argv between temp dirs exits 0 and logs no ERROR line.
- **Acceptance:** (a),(b) fail on old code; (c) passes with the real binary; no ERROR in log.
- **Scope:** `core/cloud_sync.py` (~5 lines) + tests.

### AF-3 (HIGH) renames are copy+delete on GCS; G3 counts them as moves
- **Finding:** GCS backend has Copy but no Move, so `--track-renames` performs server-side copy + delete. Reproduced on `:memory:`: one rename -> `deletes=1, renames=1, serverSideCopies=1`; with `--max-delete 0` it produced fatalError/errors=2. G3 dry-run (`cloud_sync.py:347-360`) counts renames as moves, so `--max-delete` (cloud_sync.py:567-573, same allowance as G3) excludes them; `deletes_of` counts the delete half as a real deletion in `already_deleted`; `max_delete_reached()` may not see the message and the night ends CLOUD_FAILED (exit 7, not retried) instead of BLOCKED.
- **[DECISION - Owner chose (a) on 2026-11-25]** Owner picks one (recommendation first):
  - **(a) Recommended, simplest that works (CHOSEN):** drop `--track-renames` for the cloud leg on GCS (default `track_renames=False` for cloud, keep config key). A renamed folder is then uploaded again and the old names deleted as normal, counted by G3 honestly. Costs upload time/egress on renames only. Versioning keeps old copies.
  - (b) Keep track-renames and add the rename count into the `--max-delete` allowance (+ renames) and make `max_delete_reached()` also match the real log line. More code, harder to reason about.
- **Confirm first:** run the `:memory:` reproduction (script from the cloud reviewer: put N files in dir, rename, `rclone sync --track-renames --max-delete 0 --use-json-log`) and keep its output in `agent_runs/`.
- **Tests (for option a):** default config -> cloud argv has no `--track-renames`; G3 dry-run on a rename scenario reports the old+new names as delete+copy (not as a move); rename of K files with allowance < K ends BLOCKED (not FAILED) and nothing is deleted after the block. Lab: add an `aam_gcs` alias backend WITHOUT Move (rclone `:memory:` or a `crypt`/`alias` combo with no Move; the Opus test-matrix author picks) so the audit lab can see this class of bug.
- **Acceptance:** rename of more files than the allowance -> BLOCKED, zero deletions performed, next night after approval completes; rename within allowance -> success with exactly those deletes counted.
- **Scope:** `core/cloud_sync.py` (~30 lines), `models/config.py` default (1 line + docs help text in `core/config_help.py`), lab fixture.

---

## Phase B: FY safety

### AF-4 (HIGH) final-copy gate in overlap mode accepts a stale copy
- **Finding:** `core/fy_overlap.py:487-497` `final_copy_since` counts any verified copy since 1 Apr 00:00 IST as "done"; with `flow.py:2991-2996` G6 then passes after a failed final run (e.g. cloud OK on 1 Apr, fails nightly after). Non-overlap path resets `final_backups` (D-17, `core/rollover_journal.py:78-85`) so it blocks. Fail-closed rule violated.
- **Confirm first:** write the scenario as a test against current code (cloud success 1 Apr 01:00, failure 10 Apr; switch attempted on 10 Apr 06:00); it must show the switch proceeding.
- **Fix:** the final-copy gate must require that the **most recent** scheduled run of that leg for the configured FY is a success (or an owner approval exists), not merely any success since 1 Apr. Reuse the D-17 semantic; do not invent a new state store. If the intended rule is "a final copy taken AFTER the last data change", say so to the owner instead of guessing.
- **Tests:** (1) success then later failure -> gate blocks, switch not written, BLOCKED mail "open the dashboard"; (2) success only -> passes; (3) failure then success -> passes; (4) run started 31 Mar never counts; (5) approval via `core/approvals.py` lets it through exactly once; (6) non-overlap behaviour unchanged (existing tests stay green).
- **Acceptance:** (1) fails on old code, passes on new; slow calendar test still green.
- **Scope:** `core/fy_overlap.py` (~20 lines), possibly `flow.py` gate call site (~5 lines), tests.

### AF-5 (HIGH, design) old FY is never backed up after the switch  **[DECISION - Owner chose (c) on 2026-11-25]**
- **Finding:** `fy_overlap.py:127-147`, `flow.py:3032-3044`: passes are {configured FY, next FY}. After 1 Apr the old FY (e.g. FY26-27) is not backed up by either leg, its cloud copy goes to ARCHIVE the same morning (365-day minimum), and nothing alarms if it changes.
- **Question for owner:** during Apr-Jun do you expect FY26-27 to keep being backed up (closing entries, audit, ITR)? Options:
  - **(a) Keep the old FY as a tracked pass for N days after the switch** (config key e.g. `fy.previous_fy_grace_days`, default 92, bounds 0-366). Postpone ARCHIVE until grace ends. Reuses the existing multi-pass machinery in reverse.
  - (b) Backup stays as is, but add a **detector**: nightly cheap check (rclone `size`/`lsf --max-age` or LAN dry-run) that alerts "old FY changed since freeze" using the alert-class mechanism. No extra backup.
  - **(c) Accept as designed (CHOSEN);** once the switch succeeds, the old FY is no longer tracked. Document in the dashboard/operator text if needed.
- **Do not start until the owner answers.** Opus then writes the test matrix `docs/TESTMATRIX_AF5_<date>.md` before any agent builds; chunks per behaviour (pass list, archive postponement, end-of-grace freeze + archive, report/attention attribution, guard/approval scoping per FY).
- **Acceptance (if a):** for 92 days after switch both legs back up the old FY as well as the current FY, each with its own guard baselines/approvals; on day 93 it is frozen and archived exactly once; slow calendar test extended for Mar 25 - Jul 10; crash/resume safe.

### AF-6 (MED) rollover `nas_folder` step can stamp a folder that G5 is holding for approval
- **Finding:** `core/fy_rollover.py:380-394` `create_nas_folder` always mkdir + touches `MOUNT_MARKER`, while `core/fy_overlap.py:196-215` `ensure_nas_folder` refuses an existing non-empty unmarked folder (`NAS_NEEDS_APPROVAL`). After a human-pending G5, April silently stamps the marker, so G5 passes.
- **Fix:** make the rollover step call the same helper (`ensure_nas_folder`) and treat `NAS_NEEDS_APPROVAL` as a non-blocking step failure that leaves the journal step pending and mails "approve on dashboard" (existing non-blocking NAS-failure path already lets the switch proceed; the next LAN run finishes the step). Avoid duplicate logic: one helper only; move any shared private helpers (`_child_path`, `_parent_path`, `_fy_name`) to a small shared place rather than importing privates across modules (only if it keeps the diff small).
- **Tests:** existing-empty folder -> marker created; existing folder with files and no marker -> not stamped, step stays pending, alert raised, G5 still blocks on the next LAN run; folder with marker -> idempotent; missing folder -> created+marker.
- **Acceptance:** the non-empty/unmarked case fails on old code (marker present) and passes on new; `tests/test_a5_rollover_steps.py` still green.
- **Scope:** `core/fy_rollover.py` (~15 lines), tests.

### AF-7 (MED) archive step timeout too short
- **Finding:** `models/config.py:966-970` archive timeout default 600 s (max 7200); `gcloud storage objects update --recursive --storage-class=ARCHIVE` rewrites every object, a full FY will time out, then retries daily and alerts AAM-ROL-05 forever.
- **Confirm first:** check what the archive step records on timeout and whether a re-run re-touches already-archived objects (read gcloud docs / run on a scratch bucket prefix on the trial box with owner OK; real GCS only with test prefixes, per owner rule).
- **Fix options (code agent picks the smaller one that satisfies the acceptance, Opus approves):** raise default + upper bound, and/or make the step skip objects already ARCHIVE (e.g. list with `--filter`/`gcloud storage ls -L` or run via rclone `lsjson` + only update those not archived), and/or run it in chunks per top-level folder within the journal.
- **Tests:** timeout produces a retryable journal state, not a permanent alert loop; second run on a fully archived prefix is a no-op (fake tool asserts no `update` call); partial prefix updates only the remainder.
- **Acceptance:** documented expected duration for the firm's data size and timeout set above it with margin; repeated failure still alerts but the alert states progress.
- **Scope:** `core/fy_rollover.py` + `models/config.py` (~30 lines), tests.

---

## Phase C: LAN / shutdown safety

### AF-8 (MED-HIGH) shutdown ignores whether we woke the NAS
- **Finding:** `flow.py:2374`, `flow.py:3370`, `core/wol.py:152`: `ensure_server_online` returns True whether the NAS was already up or was woken; single-pass LAN path has no "woken" flag; the weekly audit's `nas["woken"]` only means the wake step finished. A manual/dashboard run while staff use the NAS ends with `shutdown /s /f`.
- **Fix:** `ensure_server_online` (or a wrapper) returns a result that distinguishes `already_up` from `woken`. Shutdown tasks (LAN pipeline, audit, overlap multi-pass) run only if `woken` (config key `lan.shutdown_only_if_woken`, default true; owner may set false if the NAS is dedicated). Log "skipping shutdown: NAS was already online" and record it in the run row/manifest.
- **Impact warning:** `ensure_server_online` and the shutdown path are in the overlap `wake_nas`/`allow_shutdown` plumbing; run impact on both and keep the signature change backward compatible (add a field, do not change the bool return used by callers until all are updated).
- **Tests:** NAS already up -> no shutdown call (fake `shutdown` module asserts not called) in: scheduled LAN run, manual LAN run, audit, multi-pass overlap night; NAS down -> woken -> shutdown called once after the last pass; woken but a pass failed -> unchanged existing behaviour; config false -> shutdown regardless (old behaviour).
- **Acceptance:** all "already up" cases fail on old code, pass on new. Fakes only, never real shutdown.
- **Scope:** `core/wol.py` (~15), `flow.py` (~25), `models/config.py` (~3), tests.

### AF-9 (MED) runtime validation of shutdown target
- **Finding:** `core/shutdown.py:76`, `flow.py:1516`: `wol_target.check_wol_target` runs only at install/Settings save. If `wol.server_ip` differs from the `lan_destination` host or is loopback/this PC, `shutdown /m` hits the wrong machine (or powers off the backup server).
- **Fix:** `lan_shutdown_task` (or `core/shutdown.py`) refuses and records `*_BLOCKED`/alert when the IP is in `guards.local_addresses()` or does not match the UNC host of `lan_destination` (reuse `wol_target` helpers; no new parsing code). Never auto-proceed.
- **Tests:** loopback, this machine's IP, mismatched host, matching host (allowed), hostname-vs-IP match via the existing resolver; asserting `subprocess`/`run_tool` is never reached for refused cases.
- **Acceptance:** refused cases fail on old code (shutdown attempted via fake), pass on new.
- **Scope:** `core/shutdown.py` or `flow.py` (~20 lines), tests.

### AF-10 (MED) `_auto_backup_dir` swallows errors and drops versioning
- **Finding:** `flow.py:1119-1133` catches all exceptions and returns None; with `versions_enabled=true` the sync then runs `--delete-after` with no `--backup-dir`, silently destroying overwritten/deleted files; dry-run and real run may disagree.
- **Fix:** if versions are enabled and the dir cannot be resolved, raise (fail the run, status per `core/statuses`, alert). If versions are disabled, None stays valid. Narrow the `except` to the errors it really expects.
- **Tests:** versions on + resolver raising -> run FAILED, no sync invoked; versions off -> unchanged; versions on and OK -> `--backup-dir` present in both dry-run and real argv.
- **Acceptance:** first case fails on old code (sync invoked without backup-dir), passes on new.
- **Scope:** `flow.py` (~15 lines), tests.

---

## Phase D: hardening (each small, independent)

- **AF-11 (MED) cloud excludes.** `core/cloud_sync.py:530-556`: add `System Volume Information/**` and `$RECYCLE.BIN/**` excludes for sync, dry-run and verify exactly like the LAN leg (reuse the same constant list if one exists; if it lives in `lan_sync.py`, move it to one shared place). Config key for extra excludes only if the LAN leg already has one. Tests: argv of sync/dry-run/check all contain the excludes; a source drive root fixture with a protected folder does not fail. Scope ~15 lines.
- **AF-12 (MED) verify race.** `flow.py:1872-1880`: files that appear (`+`) or change size (`*`) AFTER the sync started should not fail verify. Options: pass `--max-age`/exclude by mtime newer than sync start, or re-check just the differing files once. Prefer re-checking only the reported differing files (one pass) before declaring VERIFY_FAILED; config key `cloud.verify_recheck_enabled` (default true). Tests: file added during window -> verified after recheck; file genuinely missing -> still VERIFY_FAILED; 100% fail-closed otherwise. **Do not weaken the fail-closed rule**; Opus reviews.
- **AF-13 (LOW-MED) record-run retry.** `flow.py:2492` (`_record_run` failure only logs CRITICAL, row stays RUNNING -> ABANDONED, G2 baseline lost): retry N times with short delay (config keys: attempts, delay), then still log CRITICAL and keep the existing sweep. Test with a DB stub that fails once.
- **AF-14 (LOW-MED) verify "reporting-only" query.** `flow.py:1879`: failure of the reporting-only `rclone size` (300 s timeout) must not turn a passed check into VERIFY_FAILED; record "size unavailable" in the report instead. Test with `fake_run_tool` size timeout.

## Phase E: low items and cleanup

- **AF-15 (LOW) classifier/set mismatches and misc literals.**
  - `cloud_sync.py:41` vs `:463-464`: exits 4 and 5 are in `_RETRYABLE_EXITS` but classified PARTIAL and never retried. Pick one behaviour with Opus; make the set and classifier agree (delete from set if never retried). Test pins the table of exit code -> status -> retry.
  - `cloud_sync.py:655-659` stall kill: run the same `rclone_retryable` checks before returning `retryable=True`; derive deletes from the last stats line plus an allowance margin rather than trusting a 60 s old value (or document and test the margin).
  - `core/health.py:90-96`: make the 30 s version-check timeout a config key; do not cache a transient failure for the process lifetime (cache only successes).
  - `flow.py:815-818`: bind `dry = None` before the branch and add a test (G3 approved while G1 blocks -> proper BLOCKED, no UnboundLocalError).
- **AF-16 (LOW) FY edges.**
  - `models/config.py:904-918` refuse `overlap_activate_on` values that would activate AFTER the FY start in the same cycle (04-02..) - i.e. require the activation month/day to be in Dec..Mar window; test boundary values 12-31, 01-01, 03-31, 04-01 (already refused), 04-02.
  - `fy_overlap.py:38` and `:450`: use `core/time_utils` IST and parse with `tz=IST` (single source of time).
  - `flow.py:3143-3147` `end_overlap` ordering: stamp `ended_at` inside the slot / before releasing the slot and before the archive step finishes, or make attribution use the journal's switch timestamp. Opus to choose; test a run that waits for the slot across the switch is attributed to the right FY.
  - `flow.py:3559-3583`: run the FY race guard before the FIRST pass too, so a run that loaded config before the switch does not back up the old FY on stale config. Test: config loaded pre-switch, slot acquired post-switch -> run aborts/reloads, no pass on stale FY.
  - `core/source_picker.py:77-130`: while an overlap is active, changing the source parent re-checks the next FY folder under the new parent (or refuses the change). Test both.
- **AF-17 (cleanup, optional, owner go required)**
  - Delete dead code only after a text search confirms no production callers (GitNexus said callers may be unresolvable): `verify_cloud_integrity`, `get_cloud_diff`, `get_cloud_manifest`, `count_objects`, `counted_deletes`, `create_new_fy_folders`, `detect_rollover`, unused `lan_destination` parameter on `configured_fy`. Delete their tests in the same commit and note it. Pure deletion, no behaviour change; full suite must stay green.
  - Add `rclone_local.conf` to `.gitignore` (lab alias only, no secrets) or delete it.
  - Notify: build the alert email and support bundle only AFTER the switched-off / cooldown / duplicate checks (`core/notify.py:669-686`). Test: suppressed repeat does not call the bundle builder.
  - Deliberately NOT planned (over-engineering note only, owner's call): splitting `flow.py`, merging the four verification layers, collapsing alert classes, extracting a `FyContext`. Do not start these without a separate plan.

---

## 2. Test matrix owner (Opus, before any build)

Per project rule, an Opus subagent writes `docs/TESTMATRIX_AUDITFIX_<date>.md` for each Phase before Haiku starts: one row per scenario = inputs, expected result, which layer tests it (unit / fake_run_tool / real rclone / audit lab / slow calendar). The rows named in each task above are the minimum; Opus adds boundary rows. New cases found during build are appended there.

## 3. Definition of done (per task)

1. Confirm-first step done and its output saved in `agent_runs/`.
2. Test commit exists, shown failing on old code (paste the failing output in the commit body or agent report).
3. Code commit makes them pass; no existing assertion removed or weakened, nothing newly skipped (`git diff` reviewed for this).
4. `git diff --stat` within the stated scope.
5. Fast suite green; slow calendar file green for any task touching FY code (AF-4..7, AF-16).
6. New config keys have default + bounds in `models/config.py` and an entry in `core/config_help.py`, and are in `config.example.yaml`.
7. Finding/decision appended to the newest session doc and SESSION_HANDOFF.md updated at end of session; plan checkbox ticked.
8. Phase end: Opus review including mutation checks of the key conditions (list below).

Mutation checks Opus must run per Phase: AF-1 `==` -> `>=` / drop approved subtraction; AF-2 flag added unconditionally; AF-3 renames not counted; AF-4 `any success since` instead of `latest run success`; AF-6 marker always stamped; AF-8 `woken` ignored; AF-9 host check removed; AF-10 `except Exception` restored. Each mutation must turn at least one test red.

## 4. Out of scope / accepted risks (do not work on)
Ransomware vs LAN mirror, GCS lifecycle keeping ~2 versions, locked files, server-down, real NAS power-off tests (deferred until owner is at the NAS).

## 5. Task checklist
- [x] AF-1  [x] AF-2  [x] AF-3 (decision a/b -> a)
- [x] AF-4  [x] AF-5 (decision c: accept as designed)  [x] AF-6  [x] AF-7
- [x] AF-8  [x] AF-9  [x] AF-10
- [x] AF-11 [x] AF-12 [x] AF-13 [x] AF-14
- [x] AF-15 [x] AF-16 [x] AF-17 (notify order + .gitignore done; detect_rollover/configured_fy still used in prod, 6 legacy helpers retained for existing test coverage)
