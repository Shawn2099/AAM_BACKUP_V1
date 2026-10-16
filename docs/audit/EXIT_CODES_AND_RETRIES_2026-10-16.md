# rclone / robocopy exit codes and sync retries: findings, research, design

Date 2026-10-16. Branch `mvp-hardening`. IDs **XR-1..XR-6** are used in the commit messages.

## 1. Facts (verified, not assumed)

Source code: rclone **v1.74.3** (`cmd/cmd.go`, `fs/accounting/stats.go`,
`lib/exitcode/exitcode.go`). Experiments: the real `deploy\bin\rclone.exe` v1.74.3
syncing local folders on this PC (script kept in the session scratchpad).

| # | Fact | Evidence |
|---|---|---|
| F1 | Exit codes: 0 ok, **1 uncategorised**, **2 usage**, 3 dir not found, 4 file not found, 5 retryable, 6 no-retry, 7 fatal, 8 `--max-transfer`, 9 no transfer (`--error-on-no-transfer`), 10 `--max-duration`. Our docstring had 1 and 2 swapped. | `lib/exitcode` |
| F2 | `--max-delete N` is a **per-process** counter: rclone deletes exactly N, refuses the rest (fatal), exit 7, and never retries inside itself. Running the same command again deletes **N more**. 3 runs with `--max-delete 2` deleted all 6 files. | `StatsInfo.DeleteFile`; E2/E3 |
| F3 | A deletes-only sync exits **9** (exit 9 = `GetTransfers()==0`; deletes are not transfers). `stats.deletes` holds the real number. | `resolveExitCode`; E1 |
| F4 | rclone's own `--retries` loop logs **at ERROR level** for every failed attempt and even for `Attempt 2/3 succeeded`. Between attempts it calls `ResetErrors()`, so the final `stats.errors` describes the **last attempt only**. A run that recovered: exit 0, `errors: 0`, 5 ERROR lines. | `cmd.Run`; E4 |
| F5 | Final stats carry `fatalError` and `retryError` booleans: rclone's own judgement of whether a retry can help. A locked file all night: exit **1**, `retryError: true`, `fatalError: false`. | E5/E6 |
| F6 | `--max-duration` stop: exit 10, `fatalError: true`, and the file already in flight is finished (SOFT). | E7 |
| F7 | `--backup-dir` turns deletes into moves (still counted by `--max-delete`, still exit 9 when only moves happen). | E8/E9 |
| F8 | robocopy has no delete cap. `/MIR` deletes extras while it walks; `/XX` disables deletion completely. | robocopy docs |
| F9 | Prefect task `retries` re-call the task with the **same arguments**. A retry can't lower `--max-delete` or re-check the guards. | Prefect 3 API |

## 2. The six issues: options weighed, decision

### XR-1 (data loss): outer retry multiplies the `--max-delete` backstop

`cloud_sync_task` ran under Prefect `retries = cloud.max_attempts - 1` (default 2). After a
`--max-delete` stop (exit 7) the whole sync re-ran with a fresh N, so up to 3 × the G3
limit could be deleted (F2, F9).

| Option | Pros | Cons |
|---|---|---|
| A. Drop the outer retry entirely (rclone's `--retries` is enough) | Simplest; deletion bound is exact | Loses tolerance for outages longer than rclone's ~1.5 min of internal retries; behaviour change for every failure |
| B. Prefect `retry_condition_fn` vetoing exit 7 | Small change | The cap is still per attempt; a retry after exit 5 still gets a fresh N; can't re-check guards (F9) |
| C. `--backup-dir` in the bucket (deletes become moves) | Deletes become recoverable | Doubles storage until lifecycle; a server-side copy per delete; the cap problem remains; GCS versioning already gives this (accepted risk) |
| **D. Our own attempt loop: a cumulative cap, guards re-measured before each retry, a backstop stop = BLOCKED** | Deletions bounded by the G3 limit **across the whole run**; the human decides (rule 4); the human sees real numbers | More code (one shared helper) |

**Decision: D.** The `--max-delete` value for attempt *k* = allowance − deletes already done
in this run (from rclone's own `stats.deletes`). If the backstop trips, the run is never
retried: guards are re-measured cumulatively (already deleted + still planned) and the run
ends **CLOUD_BLOCKED** with an approval card carrying those numbers. If an attempt's
deletions are unknown (no stats line), there is no retry: fail closed.

### XR-2 (data loss, LAN): robocopy `/MIR` retried without re-checking

`lan_sync_task` retried `/MIR` after 10 min with the guard verdict from before attempt 1.

| Option | Pros | Cons |
|---|---|---|
| A. No LAN outer retry | Simple | A NAS hiccup costs the night |
| **B. Re-measure (robocopy `/L`, destination count, G1-G5, access) before every retry; cumulative G3 (deleted so far + planned)** | Same safety as attempt 1; reuses the existing guards; same helper as cloud | One extra `/L` walk per retry (minutes) |
| C. Split into copy (`/E /XX`) + guarded delete phase | Copy retries can never delete | Two full walks every night (HDD cost); the delete phase still has a check-then-act gap; larger change |
| D. Move the LAN leg to rclone (`--max-delete`, `--backup-dir`) | Hard cap like cloud | Loses `/ZB` backup mode, ACL/attribute copy and the verified robocopy guard parsing; too big for MVP. **Post-MVP candidate.** |

**Decision: B** (shared with XR-1). Robocopy's deletion count per attempt = its `*EXTRA File`
lines (already parsed); unknown → no retry.

### XR-3 (truthfulness): exit 9 recorded as "no changes" when files were deleted

| Option | Pros | Cons |
|---|---|---|
| A. New status `CLOUD_DELETES_ONLY` | Precise | New status touches statuses/dashboard/reports/tests for a rare case |
| **B. `CLOUD_NO_CHANGES_COMPLETE` only when rclone's stats prove 0 transfers, 0 deletes, 0 deleted dirs, 0 renames; otherwise `CLOUD_COMPLETE`** | Truthful with existing statuses; counts are in the row | "Complete" doesn't say "deletes only" (the deletes count does) |

**Decision: B.** A missing stats line means it can't be proven, so `CLOUD_COMPLETE`.

### XR-4 (false alarms): ERROR lines from attempts rclone already retried

| Option | Pros | Cons |
|---|---|---|
| A. Trust the exit code only | Simple | Loses the C-DK-001 protection (exit 0 with errors) |
| B. Trust `stats.errors` only | rclone's own count | A missing stats line or an error logged without counting slips through |
| **C. Judge only the final attempt: ERROR lines after the last `Attempt k/N failed` marker (except the `Attempt k/N succeeded` notice) + final `stats.errors` + the plaintext fatal markers** | Keeps C-DK-001 exactly for the attempt whose result counts; recovered runs are COMPLETE; retried attempts are still recorded | Relies on rclone's marker text (pinned version; a test pins it) |

**Decision: C.** The number of rclone-internal failed attempts is recorded on the run row.

### XR-5 (truthfulness): exit 10 outside the budget path became CLOUD_VERIFY_FAILED

`--max-duration` is always set (subprocess timeout − 300 s). Exit 10 was only treated as
"stopped at the time limit" when the run budget was the binding limit; otherwise the
pipeline verified a knowingly incomplete bucket and sent "verify failed".

**Decision:** exit 10 is always a time-limit stop: CLOUD_PARTIAL, verify skipped, one
PARTIAL alert naming the limit (run budget or rclone `--max-duration`). No alternative is
better: verify can only fail on a bucket the tool says is incomplete.

### XR-6 (wasted time): retrying failures that can't change

**Decision:** retry only what can change, per rclone's own taxonomy (F1, F5):

| Outcome | Retried? |
|---|---|
| exit 0 / 9 (success), 10 (time limit) | no (not a failure) |
| exit 1, 4, 5 with `stats.fatalError` false | **yes** |
| exit 2 usage, 3 dir not found, 6 no-retry, 7 fatal, 8 | no |
| `--max-delete` stop | no: BLOCKED (XR-1) |
| hard timeout (rclone overran its own `--max-duration`), SUSPECT, spawn error | no |
| LAN_FAILED with a known deletion count, not a timeout | yes (after re-measure) |

The retry sleep must fit into the run budget, otherwise the run keeps its failure.
Also fixed: a failed sync's real exit code and status (FAILED / SUSPECT) now reach the
run row. Before, `sync_result` stayed at `exit_code -1` when the task raised.

## 3. Approvals and retries

An approved run (e.g. G3 mass delete) that fails mid-way may retry **inside the same run**:
the approval already used by this `run_id` is re-evaluated against the cumulative numbers
("not worse than approved", the same tolerance). It never covers another run.

## 4. Config

No new keys: `cloud.max_attempts` / `cloud.retry_delay_seconds` and `lan.max_attempts` /
`lan.retry_delay_seconds` keep their meaning (now enforced by the attempt loop instead of
Prefect task retries).
