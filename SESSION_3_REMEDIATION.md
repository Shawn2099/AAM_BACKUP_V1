# Session 3 — Mandatory Remediation Report

**Date:** 2026-08-21 (IST) · **Branch:** `reliability-2026-08-20` (base `d27198c`)
**Mandate:** close the 9 mandatory remediation conditions from the Session 2 independent audit
(`SESSION_2_INDEPENDENT_AUDIT.md` / `SESSION_2_FINAL_RECONCILIATION.md`), actually execute the 53
skipped real-hardware tests on this production server, and finish with **0 failures and 0
unjustified skips** — without weakening or deleting any test.

## VERDICT: ALL 9 MANDATORY CONDITIONS CLOSED

| # | Condition | Status | Evidence |
|---|---|---|---|
| M3 | Silent concurrency-slot timeout | ✅ fixed | rows + single alert + annotation; 2 new tests |
| M4 | SMTP partial refusal = false success | ✅ fixed | `return False` + named recipients; 3 real-STARTTLS tests + 2 mock |
| M5 | Unguarded `Path.exists()` in preflights | ✅ fixed | OSError → structured failure; 2 guard tests + 2 regressions; **live prod crash found & confirmed (2026-08-20 21:00)** |
| M1 | LAN_PARTIAL ignored robocopy bit 3 | ✅ fixed | `exit_code & 8` split; 5 test cases |
| M6 | Bare `rclone` in sync command | ✅ fixed | `resolve_binary` cmd[0]; 3 tests |
| S2-30 | `--modify-window 2s` silent skips | ✅ fixed | flag removed ×3 files; **both directions proven on real GCS** (test_cloud_02 + test_cloud_11) |
| M8 | Guide key typos + silent drop | ✅ fixed | guide corrected + loader warning; 3 tests |
| S2-14/F17 | Live bucket lifecycle 90d vs 92d | ✅ applied | `--lifecycle-file` deploy policy; verified exact match (all 5 rules) |
| S2-20 | test_fy_07 destructive cleanup | ✅ fixed | scratch roots + safety guard + wol/shutdown off; **test now PASSES on real hardware** |
| — | 53 skipped tests | ✅ executed | 54/54 real-hardware tests passed twice (run 2 = final, with the §7.3 fix) |
| — | 0 failures / 0 unjustified skips | ✅ achieved | full suite 1462 passed / 54 skips (every skip = the F4 real-hardware gate, now executed); e2e 4/4 |

Bundled should-fixes also closed: **M2** (double verify alert → single alert point) and
**S2-35** (e2e cleanup fixture). One latent live-data test bug (**pipe_01** fy-prefix patch,
§7.3) was caught by the post-run verification, recovered (byte-exact re-sync, `rclone check`
0 differences), and fixed with a pre-run safety assert.

---

## 1. Method

For every condition: (1) write a regression test that **reproduces the defect on the unfixed code**,
(2) apply the product fix, (3) verify the test passes, (4) run the full suite for regressions,
(5) run the real-hardware acceptance suite on this production machine (this machine *is* the
production server, so the `AAM_RUN_REAL_HARDWARE=1` gate is legitimately satisfied here).

> **Process deviation (disclosed):** `AGENTS.md` mandates GitNexus MCP impact analysis before
> symbol edits. The GitNexus MCP tools are **not available in this session** (no MCP server
> connected). Impact analysis was done manually instead: every modified symbol's full caller set
> was read (grep across `flow.py`, `core/*`, `tests/*`) and the complete 1462-test suite was run
> after the changes as the empirical blast-radius check.

## 2. Defect reproductions (pre-fix run)

396 tests collected across the 11 touched test files → **21 failed, 375 passed (44.2 s)**.
All 21 failures are the intended defect reproductions; no incidental failures:

| Defect | Failing reproduction tests (pre-fix) |
|---|---|
| M2 (double alert) | `test_verify_failure_records_verify_failed_no_pipeline_alert`, `test_verify_failure_single_alert` |
| M1 (anomaly-only PARTIAL) | `test_anomaly_only_no_alert_and_shuts_down[4]…[7]` (4 params) |
| M3 (silent slot timeout) | `test_slot_timeout_records_rows_and_alerts`, `test_slot_timeout_single_mode_records_one_row` |
| M4 (SMTP partial refusal) | `test_partial_refusal_is_not_success` |
| M5 (unguarded `Path.exists()`) | `test_access_denied_canary_raises_healtherror`, `test_source_stat_error_returns_structured_failure` |
| M6 (bare `rclone`) | `test_basic_structure`, `test_falls_back_to_bare_rclone_when_unresolvable`, `test_starts_with_rclone_sync`, `test_source_and_dest_in_correct_positions` |
| S2-30 (`--modify-window 2s`) | `test_modify_window_removed` ×4 (sync, sync-edge, verify, reporter) |
| M8 (silent key drop) | `test_unknown_root_key_warns`, `test_unknown_root_key_multiple_warns_all` |

**Post-fix:** 396/396 pass.

## 3. Fixes applied (per condition)

### M3 — Concurrency-slot timeout was fully silent *(mandatory, flow.py)*
Prefect `concurrency("aam-backup", …)` raises builtin `TimeoutError` on `with`-entry if the slot
can't be acquired within 3600 s. Pre-fix: no email, **no `run_history` row**, nothing on the
dashboard — a stuck holder could silently cancel entire nights of backups.

**Fix:** `_slot_acquired` flag set at slot entry; `except TimeoutError` branch in `backup()` calls
new `_handle_concurrency_slot_timeout(config, mode, flow_start_iso, exc)` which (1) records a
`CLOUD_FAILED`/`LAN_FAILED` row for every enabled pipeline of the mode, (2) sends **one** failure
alert stating the backup *did not run* and why, (3) on alert failure does the standard A1
bookkeeping (CRITICAL log + `[ALERT_NOT_DELIVERED]` since-iso annotation), then the original
`TimeoutError` is re-raised so the Prefect console still shows the failed run.

**Verified:** `TestConcurrencySlotTimeout` (2 tests) — patched `flow.concurrency` to raise
`TimeoutError` on entry; asserts the recorded rows, the single alert, and the re-raise.

### M1 — `LAN_PARTIAL` ignored robocopy bit 3 *(mandatory, flow.py)*
`core/lan_sync.classify_exit_code` maps exit 4-7 (bit 2 only — *mismatched/extra attributes; no
file failed; `error=None`*) to `LAN_PARTIAL`, exactly like exit 8-15 (bit 3 — real copy errors).
Pre-fix the branch alerted and skipped the NAS shutdown for **all** of 4-15: a healthy night
produced a false failure email and a NAS left powered on all night.

**Fix:** the `LAN_PARTIAL` branch now splits on `exit_code & 8`:
- exit 4-7 → `logger.warning` (anomaly-only), **no alert**, `lan_shutdown_task` as after a full mirror;
- exit 8-15 → failure alert + NAS left on (unchanged behavior, plus `files_failed` in the body).

**Verified:** `TestLanPartialAnomalyOnly` — exits 4,5,6,7 → no alert + shutdown called once;
exit 10 → alert + no shutdown.

### M2 — Verify failure alerted twice *(should-fix, bundled: same alert path)*
Pre-fix `_run_cloud_pipeline` sent its own `send_failure_alert` on verify failure **and**
`backup()`'s flow-level summary sent another for the same `RuntimeError` — 2 emails per verify
failure (observed live in session 1 T6).

**Fix:** removed the pipeline-level alert; the `RuntimeError(verify_err)` still carries the full
message into the flow summary, which is the **single** alert point (with since-iso annotation
bookkeeping). No other pipeline sends a pipeline-level alert for verify failure.

**Verified:** `test_verify_failure_records_verify_failed_no_pipeline_alert` (pipeline: 0 alerts)
and `test_verify_failure_single_alert` (real `_run_cloud_pipeline` with mocked tasks: exactly 1
alert, with "verification FAILED", `missing-from-cloud=1`, `unexpected-in-cloud=2`).

### M4 — `sendmail` partial refusal treated as success *(mandatory, core/report.py)*
`smtplib.sendmail()` returns `{recipient: (code, msg)}` for **partial** refusal and only *raises*
when **all** recipients are refused. Pre-fix the return value was ignored → `return True` +
"Email sent" while the dead recipient(s) got nothing.

**Fix:** capture the return dict; if non-empty, log `Email only PARTIALLY delivered` naming each
refused address + SMTP code and `return False` (permanent error — no retry). `False` feeds the
callers' existing `[ALERT_NOT_DELIVERED]` bookkeeping.

**Verified:** `TestSmtpPartialRefusal` — real STARTTLS server (self-signed cert, AUTH PLAIN)
driving the **unmocked** code path: partial refusal → `False` + refused address in logs; total
refusal → `False` ("permanent"); clean send → `True`. Mock-based regression guards added in
`test_report_comprehensive.py`.

### M5 — Unguarded `Path.exists()` in preflights *(mandatory, core/lan_preflight.py, core/cloud_preflight.py)*
CPython 3.12.3 `Path.exists()` swallows only `{ENFILE, EMFILE, ENOENT, ENOTDIR, ETIMEDOUT}`;
anything else re-raises. This NAS has answered **`WinError 5` (ACCESS_DENIED)** instead of
NOT_FOUND (missing share subfolder → the share denies access to the nonexistent child's stat),
crashing both preflights outside their self-recovery paths.

> **Live production confirmation (found during this session):** the 2026-08-20 21:00 LAN run
> failed with exactly this raw crash — `run_history` row
> `LAN_SKIPPED, exit=-1, err="[WinError 5] Access is denied:
> '\\10.10.186.231\lan_backup\FY26-27\.AAM_TARGET_MOUNTED'"`. The entire 21:00 LAN backup was
> lost to the unguarded stat. (Root cause — missing NAS FY folder — fixed via G11, §5.)

**Fix:** wrap the canary/source `exists()` in `try/except OSError` → treat as missing → the
existing structured failure/`HealthError` path with recovery guidance.

**Verified:** `TestCanaryAccessDeniedGuard` / `TestSourceExistsErrorGuard` (patched
`Path.exists()` → `PermissionError` subclass: structured `HealthError`/`{"ok": False}` instead of
a raw traceback) plus missing-path regression guards.

### M6 — Bare `"rclone"` in sync command *(mandatory, core/cloud_sync.py)*
`build_rclone_sync_command` used a bare `"rclone"` (OS PATH) while preflight/verify resolved
`deploy\bin` first — in production that meant **1.74.2 (system32) for the sync** and **1.74.3
(deploy\bin) for the verify**: the sync and its integrity check could run different binaries.

**Fix:** `rclone_exe = resolve_binary("rclone") or "rclone"` as `cmd[0]` (same resolution as
preflight/verify; bare name only as last resort).

**Verified:** `test_basic_structure` (cmd[0] == resolved `deploy\bin\rclone.exe`,
`resolve_binary` called once with `"rclone"`), `test_falls_back_to_bare_rclone_when_unresolvable`,
plus the two comprehensive-file tests updated to pin the resolved-binary contract.

### S2-30 — `--modify-window 2s` permanently skipped same-size resaves *(mandatory)*
With the window, rclone treated a same-size resave whose mtime landed within 2 s of the GCS
object's mtime as unchanged — and it stayed skipped on **every subsequent run** (reproduced on
real GCS in session 2 experiment E1: sync exit 9, object byte-verified STALE). The stated
rationale ("NTFS mtime granularity is 2 seconds") was wrong — NTFS FILETIME is **100 ns** (2 s is
FAT). GCS stores the source mtime verbatim (`x-gcs-mtime`), so without the window rclone compares
size + exact mtime: unchanged files still match exactly (no re-upload storm) and changed files
are re-uploaded.

**Fix:** removed `--modify-window 2s` from `build_rclone_sync_command` (the functional one) and
from `core/cloud_verify.py` / `core/cloud_reporter.py` (inert under `--size-only`, but the wrong
rationale comment had to go so the flag can't come back by copy-paste).

**Verified on real hardware this session:** `test_cloud_02_idempotency_zero_bytes` (second sync
transfers 0 bytes — no re-upload storm) **and** new `test_cloud_11_same_size_resave_within_old_window_is_reuploaded`
(the E1 scenario: 4 KB file uploaded, resaved with different same-size content 0.3 s later —
second sync `CLOUD_COMPLETE`, `rclone cat` byte-compares the object to the new payload) —
**both directions proven on the live bucket.**

### M8 — Deploy-guide key typos + silent Pydantic drop *(mandatory)*
The guide shipped `notification.smtp_host`, `notification.email_from`, `notification.email_to` —
none of which exist on the model (`notifications.*`, `sender`, `recipients`). Pydantic
`extra="ignore"` dropped them silently: a deployment following the guide ran with **no email
configured and no error**.

**Fix (two parts):**
1. `DEPLOYMENT_GUIDE.md`: key names corrected to `notifications.smtp_host/smtp_port/
   smtp_username/smtp_password/sender/recipients` + a warning box explaining that unknown top-level
   keys are ignored and how to spot the loader warning. Also corrected stale claims found while
   there: schedules "01:00 LAN / 18:00 cloud" → 21:00/22:00 IST (the deployed values),
   "rclone 1.74.2" → 1.74.3 with the `deploy\bin`-first resolution note.
2. `models/config.py::AppConfig.from_yaml` now logs a loud warning naming any **unknown
   top-level** key, the known keys, and the likely typo examples (`notification` →
   `notifications`, `email_from`/`email_to` → `sender`/`recipients`). Deliberately **not**
   forbidden (and not applied inside sections): existing configs carry legit legacy section keys
   (`notifications.send_on_success`, `paths.temp_directory`, …) that must keep loading.

**Verified:** `TestUnknownRootKeyWarning` (3 tests) — unknown `notification:` key warns with
`unknown`+`IGNORED`+`notifications` in the log; multiple unknowns all named; clean config → no
warning.

### S2-14 / F17 — Live bucket lifecycle (92-day policy) — see §6.

## 4. Bundled should-fixes (documented as extras, in scope of the same code paths)

- **S2-35 — e2e cleanup ran only in `__main__`:** under pytest (the only way the module actually
  runs) `E2E_TEST_FY` data accumulated on E:, the NAS, and the bucket (observed as live
  pollution in the session-2 audit). Fixed with a module-autouse fixture
  `e2e_live_cleanup` that purges `E2E_TEST_SOURCE`/`E2E_TEST_DEST` dirs and the **root**
  `aam_gcs:{bucket}/E2E_TEST_FY` prefix pre *and* post, regardless of entry point.
- **S2-20 — test_fy_07 finally-block `rmtree` targeted live production paths:** the old version
  built its scenario under the live config's parents (`E:\` and the NAS share root), so its
  cleanup resolved to **`E:\FY26-27` (the 572-file live dataset) and the NAS FY folder — a test
  that destroyed production data on success** (which is why the session-2 audit excluded it as
  destructive). Rewritten: scratch roots `<E2E_TEST_SOURCE|E2E_TEST_DEST>\ROLLOVER\FY…`, an
  `assert_safe_rollover_targets` guard that aborts if any old/new candidate matches a live
  production path, `wol.enabled=False` + `shutdown_after_backup=False` in the temp config (the
  copied prod config would otherwise wake *and* power off the NAS mid-test), and the GCS archive
  step targets the nonexistent `FY23-24` prefix (verified safe: `rollover()` archives the **old**
  FY prefix only). Pure-function guard unit tests added in `tests/test_fy_rollover.py`
  (`TestS220RolloverTargetGuard`, ungated).

**Deliberately NOT fixed (documented):** S2-32 (pre-phase `*_SKIPPED` statuses) — Low severity,
out of the mandatory scope; existing tests (`rt_06::test_pipe_03/04`) codify the current
SKIPPED semantics and changing them would alter report/weekly-summary behavior without a
mandatory-condition mandate.

## 5. Production environment repairs (prerequisites for the acceptance run)

1. **G11 — missing NAS FY folder:** `\\10.10.186.231\lan_backup\FY26-27` did not exist (the 2026-08-20
   21:00 LAN run died on it — §3 M5). Recreated the folder **and** the `.AAM_TARGET_MOUNTED` canary
   per the documented G11 self-recovery procedure. Verified by listing the share.
2. **Pre-state snapshots** taken before the run (`_s3_pre_state/`):
   - `E:\FY26-27`: 572 files, full SHA-256 manifest;
   - NAS `FY26-27`: 1 file (the canary just recreated);
   - bucket: root = 3 top-level entries; `FY26-27/` = **572 objects / 54.343 MiB** (incl. 4
     pre-existing nested `E2E_TEST_FY/` objects from 2026-08-18 — left untouched); `E2E_TEST_FY/`
     (root, suite-owned) = 0 objects; `FY23-24/` absent; `NONEXISTENT_BUCKET/` = 3, `docs/` = 3
     (O1 junk — do not delete).
3. **Real-hardware config isolation:** dev `config.yaml` backed up to `config.yaml.s2dev.bak`;
   a temporary acceptance config used for the run with: isolated `runtime_dir`
   (`C:\Users\Administrator\Desktop\testing\_s3_rt_runtime` — DB/logs/locks/Prefect home never touch
   `C:\BackupAgent`), source `E:\test_backup` (5 MB scratch, **not** the live `E:\FY26-27`), bare
   NAS share (suite works only in the `E2E_TEST_DEST`/`E2E_TEST_FY` namespaces),
   `shutdown_after_backup: false` (NAS never powered off), notifications fully disabled,
   `storage_class: STANDARD`, prod GCS key path. Validated via `load_config()` before the run.
4. **Safety gates verified pre-run:** no robocopy/rclone/gcloud processes; it was 04:08 IST
   (the 21:00/22:00 scheduled runs are long past); NAS SMB 445 reachable; `ensure_server_online`
   is a no-op when the NAS is already online (no magic packet, no stability wait);
   `lan_shutdown_task` early-returns when `shutdown_after_backup` is false; the FY-rollover
   archive step can only touch the nonexistent `FY23-24` bucket prefix.

## 6. S2-14 / F17 — GCS lifecycle policy — DONE

Read the live bucket via `gcloud storage buckets describe gs://aam-backup-demo-innovizta`
(demo service account — the key the app's rclone config already uses; no IAM block, so the prod
key was not needed): the live lifecycle was **identical to `deploy/gcs_lifecycle.json` on 4 of 5
rules**; the single drift was the `Delete` rule's `daysSinceNoncurrentTime`: **90 live vs 92 in
the deploy policy**.

Applied `deploy/gcs_lifecycle.json` atomically:
`gcloud storage buckets update gs://aam-backup-demo-innovizta --lifecycle-file deploy\gcs_lifecycle.json`
(exit 0). **Verified** by re-describe: all 5 rules now match the deploy file exactly
(order-insensitive deep-equal, `_s3_verify_lifecycle.py`):

| # | condition | action |
|---|---|---|
| 0 | `daysSinceNoncurrentTime: 1` + `matchesStorageClass: [STANDARD]` | SetStorageClass COLDLINE |
| 1 | `numNewerVersions: 2` | Delete |
| 2 | `daysSinceNoncurrentTime: 92` ← **was 90** | Delete |
| 3 | `age: 365` | SetStorageClass ARCHIVE |
| 4 | `age: 7` | AbortIncompleteMultipartUpload |

Bucket metadata confirmed untouched by the update: versioning still enabled,
`default_storage_class` STANDARD, soft-delete policy unchanged.

## 7. Results

### 7.1 Full unit + integration suite (fixed code, gate OFF)
```
1462 passed, 54 skipped in 191 s — exit code 0
```
All 54 skips are the `AAM_RUN_REAL_HARDWARE != 1` + win32 gate on the 8 real-hardware files
(53 from the session-2 baseline + 1 new real-hardware test, `test_cloud_11`).
**No other skips exist** — verified with `-rs`: every skip reason is the F4/F5 real-hardware gate.

### 7.2 Real-hardware acceptance suite (gate ON, this production server)
```
54 passed, 0 failed, 0 skipped in 436 s — exit code 0      (run 1, _s3_rt_run.log)
```
All 53 previously-skipped tests + the new `test_cloud_11` executed for the **first time ever**
on this machine: real robocopy /MIR to the NAS (incl. 50 MB hash-verified transfer and OS-locked
files), real GCS syncs/verifies (incl. tamper detection, `test_cloud_02` idempotency **and**
`test_cloud_11` same-size-resave re-upload — both S2-30 directions on the live bucket), the
previously-destructive `test_fy_07_full_rollover` (safe under the S2-20 rewrite), the full
Prefect `backup()` flow with concurrency slot + lock lifecycle (validates the M3 restructure
end-to-end), watchdog lock semantics, health checks, and the 4-test e2e suite.

### 7.3 Post-run state verification — and an incident it caught

The post-run diff (`_s3_verify_post.py`) verified:
- `E:\FY26-27`: **IDENTICAL — 572 files, full SHA-256 manifest** (the local source of truth was
  never touched by the suite);
- NAS `FY26-27`: identical (canary only); suite-owned NAS namespaces + `E:\E2E_TEST_SOURCE` +
  `E:\test_backup\E2E_TEST_FY` purged; bucket root `E2E_TEST_FY/` = 0 entries after purge;
- `NONEXISTENT_BUCKET/` (3) and `docs/` (3) untouched.

…but it also detected: **bucket `FY26-27/` had dropped from 572 objects / 54.343 MiB to exactly
5 objects / 5120 B** — the five 1 KB scratch files from `test_rt_06::test_pipe_01` (uploaded
04:26:48, mid-run).

**Root cause (a latent pre-existing test bug, same family as S2-20 — first detonation ever,
because the rt suite had never actually run on this machine before):**
`flow.py:29` does `from core.fy_router import get_fy_prefix`, so `flow` holds its **own**
binding. `test_pipe_01` patched `core.fy_router.get_fy_prefix` — a no-op for that binding. The
cloud pipeline therefore computed the **live** FY prefix (`FY26-27`) and mirror-synced the
5-file scratch source into the production prefix: 5 test files uploaded, **567 production
objects deleted** (the 2026-08-20 22:00 cloud backup).

**Recovery (deterministic — the local source was byte-intact, verified by SHA-256 before and
after):** re-synced `E:\FY26-27 → aam_gcs:aam-backup-demo-innovizta/FY26-27` through the app's
own production code path (`core.cloud_sync.run_cloud_sync`, i.e. the fixed M6/S2-30 command):
`CLOUD_COMPLETE`, exit 0. Verified: **572 objects / 54.343 MiB — byte-exact restoration**, and
`rclone check --size-only --one-way` = **0 differences, 572 matching files**
(`_s3_verify_recovery.py`). The 5 stray test files were removed by the mirror.

**Fix:** `test_pipe_01` now patches `flow.get_fy_prefix` (the binding flow.py actually calls)
and asserts, **before the pipeline runs**, that the patch took effect — a future refactor that
breaks the interception fails the test loudly instead of aiming a mirror at live data. All other
real-GCS call paths were audited: `rt_02`/`e2e`/`rt_07` pass explicit `fy_prefix="E2E_TEST_FY"`;
`pipe_03` fails preflight before any sync; `pipe_05` mocks the pipeline; the rollover
`run_final_backup` explicitly uses `fy_prefix=old_fy` (scratch FY). The clean re-run of the full
54-test suite (with the fix) is the final acceptance result:

```
54 passed, 0 failed, 0 skipped in 436.7 s — exit code 0   (run 2, _s3_rt_run2.log)
```
Post-run 2 bucket verification: `FY26-27/` = **572 objects / 54.343 MiB, `rclone check` 0
differences** (untouched by run 2 — the pipe_01 fix holds); `NONEXISTENT_BUCKET/` (3) and
`docs/` (3) untouched; root `E2E_TEST_FY/` purged to 0 entries; bucket root = exactly
`FY26-27/`, `NONEXISTENT_BUCKET/`, `docs/` as pre-incident. Dev `config.yaml` restored;
production `C:\BackupAgent` DB/logs verified untouched throughout.

## 8. Test-integrity statement

No test was weakened or deleted. The only assertion changes in pre-existing tests are
**contract corrections** forced by the fixes themselves:
- `--modify-window` presence assertions → absence assertions (the flag was the bug, S2-30);
- `cmd[0] == "rclone"` → resolved-binary assertions (M6);
- MagicMock `sendmail` return values set to `{}` to model the real smtplib contract (M4) —
  two new mock-based refusal tests + three real-STARTTLS-server tests added instead;
- `test_pipe_01`'s fy-prefix patch retargeted from `core.fy_router.get_fy_prefix` (a no-op —
  flow.py holds its own binding via `from ... import`) to `flow.get_fy_prefix`, plus a
  pre-run guard assert (§7.3). This strengthened the test from "silently targets live data"
  to "fails loudly if it ever would".
New coverage added for: M1 (5 cases), M2 (2), M3 (2), M4 (3 real-server + 2 mock), M5 (2 + 2
regression guards), M6 (3), S2-30 (4 + 1 real-hardware), M8 (3), S2-20 guard (3), S2-35 cleanup
(fixture), S2-30 real-hardware both-directions (1).

## 9. Residual notes

- The **production deployment** (`C:\AAMBackup`, NSSM service `AamBackupAgent`) still runs the
  pre-fix code from `d27198c`. The fixes live in this checkout on
  `reliability-2026-08-20`. Deploying them to `C:\AAMBackup` (copy changed files + restart the
  service) is a separate operator action — until then the M3/M4/M5/M6/S2-30 exposures remain in
  production (the G11 folder repair mitigates last night's specific crash, but the code that
  made it a crash is still deployed). **The §7.3 incident makes this deployment urgency
  concrete**: the pre-fix test suite contains a latent live-data hazard (pipe_01), which only
  became visible now that the suite actually runs; the production code likewise still lacks the
  M3/M4/M5 visibility fixes.
- The pre-fix 2026-08-19 runs failed with "Source drive not accessible: E:\FY26-27" (E: was not
  connected) — pre-existing, environmental, not in scope.
- Unknown junk at the NAS share root (`new_folder`, `document1.txt`, `invoice_2026/2027.pdf`,
  `large_dummy.bin`) was left in place and documented; only suite-owned namespaces are purged.
- Pre-existing test-hygiene quirk (observed, not introduced, not in mandatory scope): some
  watchdog **unit** tests that exercise the real `core.watchdog` code path with fake configs
  emit a few lines into the live service log (`C:\BackupAgent\logs\watchdog_svc.log`) with fake
  IPs (10.0.0.5 / 192.168.10.100 — no such hosts on this LAN; no WoL effect). Harmless log noise;
  a proper fix would give those tests a per-test log sink.
- The nested `E2E_TEST_FY/` folder *inside* `E:\FY26-27` (5 files, pre-existing pollution from a
  session-1/2 e2e run made with the production config) is part of the 572-file dataset — left in
  place (it is inside the live dataset now; removing it is an operator curation decision).

## 10. Artifacts

- `_s3_full_pristine.log` — first full-suite run on fixed code (6 contract-update failures, since fixed)
- `_s3_full_green.log` — full-suite run (1462 passed / 54 gated skips, exit 0)
- `_s3_rt_run.log` — real-hardware acceptance run 1 (gate ON; caught the §7.3 incident)
- `_s3_rt_run2.log` — real-hardware acceptance run 2 (gate ON, with the pipe_01 fix)
- `_s3_pre_state/` — pre-run SHA-256 / listing / bucket snapshots
- `config.yaml.s2dev.bak` — dev config backup (restored after the run)
- `_s3_check_prod_db.py`, `_s3_snapshot_pre.py`, `_s3_snapshot_bucket.py` — read-only probes
- `_s3_verify_post.py` — post-run state diff (live-data untouched proof)
- `_s3_recover_bucket.py`, `_s3_verify_recovery.py` — §7.3 recovery + verification
