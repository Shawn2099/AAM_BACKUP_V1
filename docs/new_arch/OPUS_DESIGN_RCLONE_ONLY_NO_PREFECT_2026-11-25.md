# Design review: rclone for both legs, simpler verification and guards, no Prefect (2026-11-25)

Scope: worktree `aam_baseline_overlap`, branch `baseline-v1-overlap`, HEAD 4b12c73. This is a read-only design review.
No product code was edited, and the box and services were not touched. Line numbers refer to the current HEAD.
It builds on `docs/OPUS_AUDIT_PREFLIGHT_FLOW_2026-11-25.md` (cited as "the audit") and
`docs/LIVEBOX_FINDINGS_AND_FIXES_2026-11-25.md`. Note that the audit cites older line numbers (HEAD 10a4e29). Since then
0ec61f2 added the shared rclone `--track-renames` delete measurement, which moved flow.py by about 60 lines.

Evidence labels used below:
- **[CODE]**: read in our code at file:line.
- **[LAB]**: verified today with our pinned `deploy\bin\rclone.exe` v1.74.3 on local temp folders in the scratchpad
  (Windows 11, NTFS, not the NAS, not Server 2016). The script is `scratchpad\exp\exp.ps1`.
- **[DOC]**: from the rclone v1.74.3 built-in help (`rclone help flags`, `rclone sync --help`) on this PC.
- **[GEN]**: general knowledge, not verified here. **[UNSURE]**: real doubt, with the experiment that would settle it.

---

## 0. Summary verdicts

1. **Q1, rclone for the LAN leg: YES-WITH-CONDITIONS.** rclone already does most of the LAN work today. It measures
   the LAN deletes (flow.py:1160-1206, cloud_sync.py:180-257) and runs the weekly LAN hash audit (integrity.py:462-484).
   So robocopy is now only the copier, wrapped in about 800 lines of bitmask and log parsing. The conditions:
   a no-write shadow "agreement" trial first; a dated `--backup-dir` on the NAS as the safety net; locked files mapped
   to PARTIAL (rclone then deletes nothing that night); empty folders kept; and the owner accepts losing ACLs, attributes,
   creation times and `/ZB`.
2. **Q2, verification: three layers, all rclone.** (a) Every copied file is hash-checked before it gets its real name.
   rclone does this by default [LAB], so we must never pass `--ignore-checksum`. (b) Every night, one independent
   `rclone check --one-way --size-only` proves that every source file is at the destination at the right size.
   (c) Every week, a full hash `rclone check`, which already exists. The night is COMPLETE only when (a) and (b) are clean.
3. **Q3, guards: one dry-run per FY pass feeds every data guard.** `rclone sync --dry-run --track-renames --combined`
   gives the source count, destination count, planned copies, bytes, deletes and moves in one walk [LAB]. Remove robocopy
   /L, both LAN snapshots, the cloud `rclone size` walks, the cloud write probe and the robocopy-vs-rclone cross-check.
4. **Q4, remove Prefect: YES, after Phase 1-2.** In this code Prefect gives us cron, one concurrency slot, 3 task-retry
   settings and run ids. About 600 lines exist only to keep Prefect itself healthy (launch.py, watchdog.py, ui.py).
   Replace it with a small scheduler loop built on cronsim (a locked dependency) inside the agent service. It runs each
   job as a child process, does catch-up from our own run_history, uses tenacity for retries and keeps the PID lock.
   That means 2 services instead of 3, and no server or DB of its own.
5. **Q5, final version: one engine (rclone), one scheduler (ours), one truth (the SQLite DB).** The phases: 0 finish the
   live-box test and the audit's top fixes; 1 rclone LAN shadow, then a switch; 2 simplify (walks, guards, mails);
   3 remove Prefect behind a compatibility shim, so the roughly 1000 tests and the harness keep working while it is
   removed.

---

## 1. Q1: should rclone replace robocopy (and run every pre-flight)?

### 1.1 What the code does today
- The LAN copier is `robocopy /MIR /Z [/ZB] /XJ /MT:4 /R:3 /W:10 /V /TS /FP /BYTES /NJH /NDL /NP /XF marker probe /XD ...`
  (lan_sync.py:387-432). The result comes from `decide_lan_result` (lan_sync.py:215-308): the exit bitmask, plus
  "completed job summary present", plus a positional parse of the Files row, plus `** FAILED:` markers. The deletion
  count comes from streaming the `*EXTRA File` lines (lan_sync.py:46-68). Logs are UTF-16 /UNILOG with tail reading
  (lan_sync.py:147-177).
- The LAN preflight runs **two** dry-runs: robocopy `/L /MIR` (lan_preflight.py:70+) and, since 0ec61f2, the rclone
  `sync --dry-run --track-renames` measurement (flow.py:1160-1206). A cross-check then refuses to mirror if the two
  disagree (flow.py:1198-1205). [CODE]
- The weekly LAN audit is already `rclone check` (local backend, MD5 on both sides, read over SMB), with no `--size-only`
  (integrity.py:8-31, 462-484). [CODE]
- The cloud leg is rclone sync with `--check-first --modify-window 2s --error-on-no-transfer --max-delete --track-renames`
  and a JSON log (cloud_sync.py:330-405). It also has exit classification (280-310) and a log scan (72-120). [CODE]

So the product already relies on rclone reading the whole source as the service account, and on rclone walking the NAS
over UNC. robocopy is the odd one out.

### 1.2 rclone local→UNC behaviour, point by point

| Topic | rclone behaviour | Evidence | Consequence for us |
|---|---|---|---|
| Long paths | The local backend converts paths to `\\?\` form. A file at more than 280 characters was copied and checked. | [LAB] (`//?/C:/...` in the log, `deep.txt` copied); `--local-nounc` disables it [DOC] | Never set `--local-nounc`. Prove `\\nas\share` → `\\?\UNC\...` on the NAS in the shadow trial [UNSURE on SMB]. |
| Locked / open files | `Failed to copy: failed to open source object: ...used by another process`, then **"not deleting files as there were IO errors"**. It retried 3 times and exited 1. | [LAB] | Safe (no deletions on a night with a read error), but one file left open (for example the accounting package left running at 21:01) means **no deletions at all that night**. Map this to LAN_PARTIAL, naming the files, with "deletions waiting". Never use `--ignore-errors`. |
| /ZB (backup mode) | rclone has no backup mode and no VSS. | [GEN] | /ZB bypasses **ACL** denials. It does not get past **sharing locks**: robocopy fails on an exclusively locked file too [GEN]. The cloud rclone reads the same source as the same account without /ZB, so a clean cloud log proves /ZB buys nothing on this data. Check: in the shadow trial, any `Access is denied` from rclone that robocopy did not have. |
| File changed while copying | By default rclone errors "source file is being updated". `--local-no-check-updated` turns that off. | [DOC] | Keep the default. A half-written file must be PARTIAL and retried, never COMPLETE. |
| Partial files | It writes `name.<rand>.partial`, checks the size and MD5, **then** renames to the final name. | [LAB] (`new1.txt.9982fb15.partial: size = 12 OK`, `md5 = ... OK`, `renamed to: new1.txt`) | Better than robocopy /Z, which leaves a half file under the real name after a kill. A killed run leaves `*.partial` orphans; `--partial-suffix` lets us find and delete them [DOC]. |
| SMB drop / reconnect | The local backend relies on the Windows SMB redirector. A failed write fails that file, rclone `--retries` re-runs the whole sync (already-copied files are skipped quickly), and the file restarts from 0 (no /Z resume). | [GEN] | Acceptable on a LAN. A multi-GB file restarts after a drop. `run_tool` kills the process tree on timeout (CLAUDE.md, tool_runner). `--timeout`/`--contimeout` apply to network backends, not local file I/O [UNSURE]. |
| mtime | Default compare is size + modtime. rclone sets the destination modtime. We already use `--modify-window 2s` (cloud_sync.py:370). | [CODE][DOC] | Never `--size-only` for sync: accounting data files often keep the same size when edited. Never `--checksum` nightly: it rereads about 400 GB on both sides. |
| `--track-renames` | Real moves on the destination. A renamed folder of 5 files became 5 moves, and the files did not go to `--backup-dir`. | [LAB] | Removes R1 at the copier too: robocopy re-copies a renamed folder (flow.py:1161-1165 comment). Keep strategy `hash`: `modtime` could "move" the wrong content and size+mtime would never fix it. Hash cost over SMB on rename candidates is [UNSURE]; measure it (config.py:164-168 already allows 1800 s). |
| `--max-delete` | Deletes up to N, then a fatal error, **exit 7**, no retries ("failed to delete 3 files"). | [LAB] | Backstop works on local too. Partial deletion up to N happens first. With `--backup-dir`, those N are recoverable. |
| `--backup-dir` | Deleted and overwritten files move into a dated folder (`versions\run2\target\t.txt`). | [LAB] | **Strong safety net**: a mistaken mirror becomes reversible on the NAS. It must be on the same share, outside the mirror root (`\\nas\share\AAM_versions\<date>`, a sibling, not inside the destination), so the move is a cheap rename. Purge with `rclone delete --min-age <N>d` plus `rmdirs`. The space check must count it. |
| `--immutable` | Fails if existing files changed. | [DOC] | Wrong for a mirror of live accounting files. Do not use. |
| `--check-first` | All checks, then the transfers. | [DOC][CODE cloud] | Use it on LAN too (cleaner logs, stable G3). Memory grows with the change set [GEN]. Measure the RSS on the 1M-file scale (config.py:160-162). |
| Junctions / symlinks | `junction: Can't follow symlink without -L/--copy-links`, a NOTICE, skipped. | [LAB] | The same as `/XJ`. Add `--skip-links` to silence it [DOC]. |
| Empty folders | **Not created** by default. | [LAB] (`Empty` missing on the destination) | Add `--create-empty-src-dirs` so the client folder structure survives a restore. |
| Exit codes / logs | 0 ok, 1 errors, 3 dir not found, 7 fatal, 9 no transfer (with `--error-on-no-transfer`), JSON log with final stats. | [CODE] cloud_sync.py:280-310, 260-277 | Reuse the cloud classification and log scan. No bitmask, no UTF-16, no tail-seeking. |
| Reports | `rclone sync --combined/--differ/--missing-on-dst/--dest-after` exist. `--combined` also works with `--dry-run`. | [DOC][LAB] | The basis for Q2 and Q3. Note: renamed files show as `+`/`-` in `--combined`, so count deletes from the JSON `"skipped":"delete"` lines (cloud_sync.py:146-162), not from `-`. |
| Hash on the NAS | The local backend computes MD5 by **reading** the file over SMB. The NAS does not need to compute hashes, and `--download` is not needed. | [CODE] integrity.py:14-18 (measured v1.74.2) | Cost = one full read of the destination. That is fine weekly, not nightly. |
| Performance on a spinning NAS | `--transfers 4 --checkers 8` against `/MT:4`. Multi-thread copy for big files to a local destination may cause seeks. | [GEN][UNSURE] | Measure in the shadow trial. Start with `--transfers 4 --checkers 4 --multi-thread-streams 1`, then tune. |
| ACLs, attributes, creation time | Not copied. | [GEN] | The owner says ACLs do not matter. Restores come back with the default share ACL. Write it down as accepted. |

### 1.3 What we lose from robocopy
- `/ZB`: relevant only for files the service account is denied by ACL. The cloud leg's clean runs are the evidence.
- `/Z` mid-file resume: a dropped big file restarts from 0. That is minor on a LAN.
- robocopy's own per-file `/R:/W:` retry: replaced by rclone `--retries` plus `--retries-sleep` over the whole remaining
  set. That is coarser, but it re-reads only what is still different.
- robocopy deletes even when one file failed. rclone does not (safer, but see "locked files" above).
- The NTFS metadata copy (we do not use /COPYALL now anyway).

### 1.4 What breaks, and the size of the rewrite
- Goes away: `build_robocopy_command`, `decide_lan_result`, `scan_robocopy_log`, the summary parsers, tail reading and
  orphan robocopy log cleanup (most of lan_sync.py, 568 lines). Also the robocopy /L part of lan_preflight.py
  (`run_lan_dry_run`, lan_preflight.py:70-189), the robocopy↔rclone cross-check (flow.py:1191-1205, plus
  lan_guards_task's `extras` check at flow.py ~1350), the SeBackupPrivilege check (permissions.py:61-100), the robocopy
  binary check (health.py:262-263), and the robocopy config keys (`mt_threads`, `use_backup_mode`, `log_tail_kb`,
  `dry_run_timeout_seconds`, `privilege_check_timeout_seconds`, config.py:158-191). These move to `DEPRECATED_KEYS`.
- Changes: `lan_sync_task`, `_run_lan_pipeline` (`deletes_of`, `retryable`, `lan_deletes_of` flow.py ~839), the watchdog
  transfer-process set (watchdog.py:198, which keeps `rclone.exe`), `test_support/fake_tools.robocopy_log()`, and the
  status texts.
- New: about 200 lines. A `run_rclone_mirror(source, dest, profile)` shared with cloud (`build_rclone_sync_command`
  already takes most of the flags), plus a LAN classification: exit 0 → COMPLETE, exit 9 → NO_CHANGES, exit 1 with only
  source-read errors → PARTIAL (files named, "deletions waiting"), exit 7 or a max-delete hit → BLOCKED/backstop,
  anything else → FAILED.
- Tests: 78 test files mention robocopy (1348 mentions). About 15 are LAN-specific (test_lan_sync*.py,
  test_lan_result_contract.py, test_lan_preflight_comprehensive.py, test_lan_canary_integrity.py, ...) and get rewritten
  against fake rclone JSON logs. Most of the rest only use `fake_run_tool(..., robocopy_log())` and follow if the fake
  writes an rclone log. Estimate: 3-5 working days with test-first agents, plus a targeted audit-lab rerun (real rclone
  local→local).

### 1.5 Verdict: **YES-WITH-CONDITIONS**
Conditions:
1. A shadow trial passes (1.6).
2. `--backup-dir` on the NAS, with a retention purge and a space check.
3. A source read error gives PARTIAL plus "deletions waiting", never COMPLETE and never `--ignore-errors`.
4. `--create-empty-src-dirs --skip-links --check-first --modify-window 2s --track-renames` (hash), and no
   `--size-only`, `--checksum`, `--ignore-checksum` or `--local-no-check-updated`.
5. G4 identity, G5 canary and WoL plus smb_ready stay. They are not tool-specific.
6. The owner signs off the losses in 1.3.
7. A `lan.engine: robocopy|rclone` config key during the transition (default `robocopy` until the trial passes), so
   the switch back is a config change, not a deploy.

### 1.6 Minimal safe trial (no risk to the real backup)
- **Step A, agreement check, no writes (2 weeks, needs no approval to touch data).** After the nightly robocopy finishes,
  run `rclone sync <source> <real NAS mirror> --dry-run --track-renames --create-empty-src-dirs --modify-window 2s
  --combined f.txt --use-json-log` with the same excludes as flow.py:1180-1181. Pass criterion: 0 planned copies and
  0 planned deletes, or every line explained by files changed after robocopy started. This proves path encoding, long
  UNC paths, mtime fidelity on the NAS, case handling and the excludes, all without writing. Record the time and RSS.
- **Step B, real copy into a test folder (owner approval: NAS space).** `rclone sync` of one big client folder (or the
  whole source, if space allows) into `\\nas\share\AAM_rclone_trial\` with `--backup-dir \\nas\share\AAM_rclone_trial_versions\<date>`.
  Then `rclone check --one-way` (hash) of source↔trial and of robocopy-mirror↔trial. On purpose: leave a file open
  (FileShare None) during one run, rename a folder, delete 3 files, and pull the NAS network cable for 30 s mid-copy.
  Expected: PARTIAL plus no deletes; moves; the backup-dir holds the 3 files; the next run completes.
- **Step C, switch** `lan.engine=rclone` on the box, with robocopy kept as a config fallback for 4 weeks. The weekly
  hash audit is the independent judge.

---

## 2. Q2: verifying that the data arrived

### 2.1 Today
- Cloud: `rclone check --one-way --size-only`, then `rclone size` of the bucket, then `rclone lsjson -R`, then
  `rclone check --combined --size-only`, then the evidence gate (flow.py `cloud_verify_and_report_task` ~1001-1107;
  cloud_verify.py:116-160; cloud_reporter.py:62-148). That is 2 source walks and 3 bucket listings, and both checks
  are size-only. [CODE]
- LAN: no independent nightly check. COMPLETE = robocopy's own summary (lan_sync.py:269-303; its docstring says
  "NOT cryptographic verification"). The post-sync destination walk only feeds a cross-check log line that nothing
  reads (audit row 24). [CODE]
- Weekly: a hash `rclone check` on both legs, PENDING vs REAL classification (integrity.py, audit_pending.py). [CODE]

### 2.2 Design (the same for both legs)
1. **Per-file transfer proof, free.** rclone hashes while copying and compares before the rename: local→NAS
   `md5 = ... OK` [LAB]. For GCS it compares with the object's stored MD5 [GEN; settle with `-vv` on one test-prefix
   upload]. Rule: never pass `--ignore-checksum`. A transfer hash mismatch is an ERROR in the JSON log, which makes the
   run not COMPLETE (the existing `scan_rclone_log_for_errors`, cloud_sync.py:72).
2. **Nightly arrival check: one independent command.**
   `rclone check <src> <dst> --one-way --size-only --combined check.txt --modify-window 2s` (cloud adds `--fast-list`).
   - It catches: files missing on the destination (`+`) and size differences (`*`). It uses no hash (cheap: one listing
     of each side).
   - Counts come from the check's own lines: "N matching files" and "0 differences found" [LAB output]. Bytes come
     from `rclone size --json <dst>`. Cloud keeps this one call; on LAN it is a second SMB walk, so we may accept the
     source bytes from the dry-run listing instead (owner choice, §7).
   - Differences on files whose source mtime is after the run start are "changed during backup, next night"
     (reuse `audit_pending`), counted and shown, but not a failure.
3. **Weekly content proof (already built).** A full hash `rclone check` (integrity.py:462-506). Cloud uses GCS's
   stored MD5, so only the source is read. LAN reads both sides (about 400 GB × 2 at disk/LAN speed, roughly 1.5-3 h
   [GEN]; integrity.py:30 notes a dual-core Xeon and HDD). If too slow: check one top-level folder group per night on
   a rotation, so every file is hashed within 7 days.

**Gate (COMPLETE only if all hold):** sync exit 0 or 9; final stats `errors == 0`; the arrival check exit 0 with its
completion line; `differences − changed_during_backup == 0`. Otherwise PARTIAL (named files) or FAILED (no completion
evidence). This is the existing evidence-gate idea (audit row 26), applied to LAN too.

**Truthful report line** (built only from those outputs):
"NAS FY25-26: verified 412,318 files, 398.2 GB present at the right size, 0 missing, 0 different
(3 changed during the backup, next night). 1,204 files copied tonight, each hash-checked. Last full content check:
Sun 23 Nov, 412,301 files identical."

Cost per night: the LAN arrival check is one walk of each side (`lan.dry_run_timeout_seconds` already plans 5-25 min
at 1-2.5M files, config.py:160-163). That replaces today's post-sync snapshot walk, so the net cost is about zero.
Cloud: one check instead of check + size + lsjson + check.

---

## 3. Q3: the minimal guard set with rclone only

**Once per leg per night (environment):**
1. Source listable plus G1 first file (guards.py:73-85). Stop at the first file.
2. Binary present; (cloud) key file, clock, `rclone lsjson --max-depth 0` on the prefix. Classify **before** naming
   it AUTH: run a 3 s TCP connect to `storage.googleapis.com:443`. If DNS or TCP fails → NETWORK_DOWN ("internet down",
   retry next night). Only if TCP works and rclone says 401/403/invalid_grant → AUTH. Reorder `rclone_errors` so
   `dial tcp|i/o timeout|connection refused` beats the `oauth2: cannot fetch token` wrapper (audit P2, rclone_errors.py:29-36).
3. (LAN) WoL plus smb_ready **once** (wol.py:133-171, smb_ready.py), then G4 identity (resolve the host, not a local
   IP, same host as last time; guards.py:167-196, "cannot resolve" → FAIL, not BLOCK), G5 canary, a write probe plus
   free space (`shutil.disk_usage` on the UNC; permissions.py).

**Once per FY pass (data), from ONE dry-run:**
`rclone sync <src> <dst> --dry-run --track-renames --check-first --create-empty-src-dirs --modify-window 2s --use-json-log --combined dry.txt`
- From `dry.txt` [LAB]: `source_files = count(=,*,+)`, `dest_files = count(=,*,-)` (G3 denominator; this replaces the
  pre-sync snapshot walk and the cloud `rclone size` of the bucket).
- From the JSON log [CODE cloud_sync.py:146-177]: `planned_deletes` (`skipped == delete`), `moves`
  (`skipped startswith move`), planned copies plus bytes (`skipped == copy`, `size`) → the NAS free-space check
  (bytes plus `--backup-dir` growth plus min_free_dest_gb).
- G2 source shrink: `source_files` against the DB baseline. It is free now, so keep it on both legs or drop it
  (audit row 12 says it is redundant with G3; owner choice).
- G3: `planned_deletes` against `max(min_floor, pct × dest_files)` capped at `max_delete_files`. This is the audit's P3
  small-tree floor, and `--max-delete` = what is left of the allowance as a backstop.
- **G3 block → copy-only fallback** (audit F.2.1): `rclone copy`, same flags. New work is protected; the deletions wait
  for approval. With `--backup-dir` present the owner may raise the floor (deletions are recoverable).

**Unmeasurable → FAILED** (retry next night), never BLOCKED (audit row 17).

**Remove:** robocopy /L (lan_preflight.py:70-189); the robocopy↔rclone cross-check (flow.py:1191-1205);
`lan_snapshot_before/after` (flow.py:1210, 1420); cloud probe A (cloud_preflight.py:70-97); the `rclone size` source
walk for G2 (flow.py ~757); the separate bucket `rclone size` before the sync; the cloud write probe (flow.py:819-838,
already switchable); the Prefect retries on preflight/verify (flow.py:1762-1772, 2147-2149) → at most one tenacity retry
for the network probe only; the duplicate SMB probe (lan_preflight.py:111-124 when WoL just ran); `file_entries` and the
`_cloud_diff_count` cross-checks (no consumer, audit row 24).

**Keep:** G1, G3 plus the backstop, G4, G5, slot/lock, run budget/MISSED, the clock proof before one-way FY steps, the
evidence gate, the watchdog stale check.

Walks per pass after this: LAN 2 source plus 2 destination (dry-run, sync), plus 1 each for the arrival check. Today it
is 3 source plus 5 destination walks (two dry-runs, two snapshots, the sync). Cloud: 3 plus 3 instead of 5 plus 7.

---

## 4. Q4: removing Prefect

### 4.1 What Prefect does for us today [CODE]
| Prefect feature | Where | Real need | Replacement |
|---|---|---|---|
| Cron schedules (`serve`, 6 deployments) | serve.py:21-108, 134 | yes | cronsim loop (cronsim 2.7 already locked) |
| Global concurrency `aam-backup`=1, wait 3600 s | flow.py:225; launch.py:76 `_ensure_concurrency_limit` | yes (serialise) | one job queue in the scheduler plus the existing `backup.lock` (flow.py:229-266), which already refuses a second live process |
| Task retries | `with_options` flow.py:1762-1772, 2147-2149 | small | tenacity 9.1.4 (locked) on the network probe only |
| run_id = flow_run.id | flow.py:131-136 | yes (stable id) | uuid4 made by the scheduler/CLI |
| `auto_scheduled`, `expected_start_time` (lateness) | flow.py:542-566, 2470 | yes | the scheduler passes `scheduled_for`; the pipelines already take `late_seconds` (flow.py:1739-1756, 2126) |
| Markdown artifacts | flow.py:1548-1616 | no (cosmetic, "the run row is the record", flow.py:1979) | delete |
| Loguru→Prefect bridge | core/logging.py:52-115 | no | delete; loguru files plus run_artefacts stay |
| Flow-run UI | Prefect UI :4200 | no; the owner uses our dashboard | the dashboard reads run_history |
| Dashboard "run now" / "is a run active" | ui.py:28-35, 202-318, 400-421 (`arun_deployment`, `read_flow_runs`) | yes | an in-process queue call; "active" = the RUNNING row plus a live lock PID |
| Code that only exists to manage Prefect | launch.py:62-340 (API check, limit creation, cancel orphaned runs, pause/resume deployments); watchdog.py:445-560 plus most of `main` (restart the server/agent stack on API health); config.py:1172-1200 (Prefect cron validation) | - | mostly delete |

Footprint: 10 non-test product modules import prefect; flow.py has 24 `@flow/@task` decorators and 46 mentions.
In tests, 75 files touch prefect/`.fn`/`with_options`, with 133 `.fn` uses. The live-box harness uses
`prefect_test_harness()` (tools/livebox/rig.py:1150-1171, run_lane.py:1197-1223) and calls `backup`,
`rollover_check_flow` and `integrity_audit_flow` directly (rig.py:642).

### 4.2 Prefect failure modes on this box
- 3 services, where the agent depends on the server (core/services.py:8). The watchdog exists largely to restart the
  server when the API hangs (watchdog.py:1-45, 504-560). [CODE]
- Its own SQLite DB with migrations. A DB migrated by another Prefect version hangs runs (CLAUDE.md gotcha:
  `~/.prefect/prefect.db`). Upgrades are risky. [CODE/doc]
- The concurrency slot is server state. A leaked slot means every backup waits 1 h and then gets SLOT_TIMEOUT
  (flow.py:604 tells the office to "restart AamPrefectServer"). [CODE]
- A disabled leg keeps firing unless it is paused server-side (serve.py:24-29, launch.py:252). [CODE]
- Catch-up after downtime: the scheduler creates runs ahead of time, and a runner may start late runs after boot. Our
  budget/MISSED logic copes (flow.py:572-599), but the exact `serve` behaviour in 3.7.2 was never exercised
  (LIVEBOX_FINDINGS "not exercised: cron/serve catch-up"). [UNSURE]
- Memory: the server plus the API plus the agent are several hundred MB on a DDR3 box [GEN].

### 4.3 Options
| | Own cronsim loop in AamBackupAgent | APScheduler 3.11 in one service | Windows Task Scheduler + CLI | Keep Prefect, trimmed |
|---|---|---|---|---|
| New dependency | none (cronsim, tenacity, psutil locked) | yes (owner approval); persistence needs SQLAlchemy | none | none |
| Catch-up after a reboot | our rule, from run_history (truth) | `misfire_grace_time` + `coalesce` (with a persistent jobstore) | "Run as soon as possible after a missed start" | as today, unverified |
| Schedule source | config.yaml | config.yaml | OS task store (re-register on every edit, admin rights) | config.yaml + server |
| Testable with a fake clock (harness) | yes | partly | no | partly |
| Moving parts | 1 agent + watchdog | 1 agent + watchdog | OS + CLI + dashboard service | 3 services + DB |
| Risk | our own code (~250 lines) | library semantics plus a second store | split truth, opaque history | today's failure modes stay |

**Recommendation: the own cronsim loop.** It is the smallest number of moving parts, has no new dependency, keeps one
source of truth and fits the harness's simulated clock. Task Scheduler is a good second choice if the owner prefers
"OS-proven" over "our code".

### 4.4 Target architecture (under 25 lines)
```
AamBackupAgent (NSSM, auto-restart)  -> python launch.py
  thread 1: FastAPI dashboard (unchanged UI; "Run now" -> scheduler.enqueue(job))
  thread 2: scheduler loop, every 30 s (injectable clock):
            for each job in config (cloud 02:01, lan 21:01, rollover 06:00, audit Sun 08:00, weekly Mon 08:00, monthly 1st 08:30):
              due = latest cron time <= now (cronsim, config tz)
              if due > job_state.last_handled(job):
                  if now - due <= job grace (its run budget): enqueue(job, scheduled_for=due)   # catch-up, coalesced to 1
                  else: record MISSED once (existing budget logic), mark handled
            run the queue SERIALLY: Popen([python, -m, aam_cli, run, job, --scheduled-for, due, --run-id, uuid])
              kill the process tree at the budget plus a margin (psutil); child exit code -> logged; row written by the child
            heartbeat row/file every loop
  child (aam_cli run <job>): take backup.lock (exclusive PID lock, existing) -> plain pipeline functions
            (today's _run_*_pipeline, with tenacity on the network probe) -> run_history row in finally (existing)
AamWatchdog (NSSM): heartbeat stale > 5 min and no live lock -> restart AamBackupAgent; stale legs -> mail (existing 382-432)
On agent start: sweep ABANDONED RUNNING rows (existing), delete *.partial orphans, then the scheduler catch-up as above.
Logs: loguru service log + per-run run_artefacts (existing). DB: the one SQLite manifest (job_state table added).
```
A child process per job means a hung or crashed job cannot take the scheduler or the dashboard down, and the kill is
a real process-tree kill.

### 4.5 Migration order (tests and harness stay useful)
1. **Shim first.** Add `core/runtime.py` with `task(...)`/`flow(...)` decorators that return the plain function with
   `.fn = f` and `.with_options(**kw) -> f`. Switch `from prefect import flow, task` to the shim only behind a config or
   env switch. The 133 `.fn` patches and the `with_options` MagicMocks in tests keep working unchanged.
2. Pull the Prefect context out of the pipeline bodies: `run_id`, `scheduled_for` and `late_seconds` become arguments
   (the pipelines already take `late_seconds`). `_backup_slot` becomes "lock plus in-process mutex" (drop the
   PREFECT_TEST_MODE branch, flow.py:214).
3. Add the scheduler plus `aam_cli` with fake-clock unit tests (catch-up, coalesce, MISSED, serial queue, kill on
   budget, reboot at 20:55/21:30).
4. The dashboard trigger and the "active run" check move to the queue plus DB (ui.py). The watchdog drops the
   Prefect-API logic and watches the heartbeat.
5. Harness: `LaneRig.prefect_harness()` becomes a no-op context. The lane keeps calling `backup(...)`,
   `rollover_check_flow(...)` and `integrity_audit_flow(...)` (same names, now plain functions). Add one lane mode that
   drives the **scheduler loop** with the simulated clock, which finally covers the "cron catch-up / reboot" gap the
   findings list.
6. Remove `serve.py`, the Prefect parts of launch.py, the artifacts and the log bridge, the AamPrefectServer service in
   deploy/, and the `prefect` dependency. Exit check: `git grep -i prefect -- '*.py'` matches only DEPRECATED notes.

---

## 5. Q5: the final version and the phases

### 5.1 Target design
- **Components:** AamBackupAgent (dashboard plus scheduler), AamWatchdog, rclone.exe, the SQLite manifest DB,
  config.yaml, SMTP. No robocopy, no Prefect server.
- **LAN night (21:01):** RUNNING row → lock → env once (source/G1, binary, WoL plus smb_ready, G4, G5, write probe) →
  per FY: one dry-run (`--combined`) → G2/G3/free space → `rclone sync --track-renames --check-first
  --create-empty-src-dirs --max-delete <left> --backup-dir \\nas\share\AAM_versions\<date>` (on a G3-only block:
  `rclone copy`) → arrival check → gate → row. One mail per leg per night (both FYs listed). The NAS is shut down after
  the last pass whenever this run woke it (owner choice). The versions are purged by age.
- **Cloud night (02:01):** the same shape, with the GCS remote. `--backup-dir` is optional (GCS versioning ~2 is an
  accepted risk).
- **Rollover 06:00, audit Sunday 08:00, reports:** unchanged logic. The audit uses smb_ready, and a not-yet-backed-up
  FY means "not backed up yet", not a failure. The rollover counts the verified nightly runs after the closing moment
  instead of re-running backups (audit C2).
- **Removed:** robocopy and its parsing, the second dry-run, the snapshots and cross-checks, the duplicate probes,
  Prefect (server, serve, artifacts, bridge, reconcile/cancel code, the API watchdog).
- **Stays:** core/statuses, tool_runner, run_artefacts, notify, guards (G1, G3-G6), approvals, fy_overlap, the
  integrity audit, budget/MISSED, the dashboard, the DB.

### 5.2 Phases
| Phase | Entry | Work | Exit criteria (what proves it) | Biggest risk |
|---|---|---|---|---|
| **0 Finish the live-box test** | lane A data (to 2092-11) plus the audit | Finish the 2-year run with an owner approver. Fix the audit's F.2 items 1-5 (copy-only on a G3 block, small-tree floor, P2 classification, cooldown/escalation, subject with FY), test first. | A full 2-year lane with no permanent BLOCK, every block clearable, the mail count per incident following the ladder, a truthful weekly/monthly report on 3 sampled dates; unit suite green; targeted audit-lab files green | Fixing on the old engine work that Phase 1 then rewrites → keep the fixes engine-neutral (they are in guards/notify/flow logic) |
| **1 rclone LAN trial** | Phase 0 exit; `lan.engine` key | Build the rclone LAN mirror behind `lan.engine` (default robocopy). Steps A→B→C of 1.6 | 14 nights of the step A agreement (0/0 or explained); step B scenarios as expected (locked → PARTIAL plus no deletes; rename → moves; deletes → backup-dir; cable pull → next night OK); time within `lan_max_hours`; RSS measured; weekly hash audit 0 REAL differences after the switch | **A silent difference between the tools** (mtime rounding on the NAS, name encoding, case-only renames) that causes mass re-copies or a missed file → this is exactly what step A measures before any write |
| **2 Simplify** | 4 weeks on rclone without a fallback being needed | Delete robocopy code and keys (DEPRECATED_KEYS), one rclone runner for both legs, one dry-run feeds all guards, the arrival check plus the gate on LAN, trim the cloud verify, backup-dir retention, one mail per leg/night | Unit plus rewritten LAN tests green; the audit lab on real rclone local→local; the live-box lane rerun for 1 simulated year with the same expectations as Phase 0; stage_seconds before/after recorded | **Losing a safety check while deleting code** → the keep-list in §3 is a test checklist: each kept guard has a test that fails if it is removed |
| **3 Remove Prefect** | Phase 2 exit | Steps 1-6 of 4.5 | Scheduler fake-clock tests (catch-up, coalesce, MISSED, serial, kill); a 2-week live run on the box fired by the scheduler; real tests on the box: reboot at 20:55, power loss at 21:30 mid-sync (ABANDONED, then the next night OK, `*.partial` cleaned), agent kill (watchdog restart), manual run while scheduled (serialised); no prefect import left | **The scheduler silently not firing** (a timezone/DST bug, or the loop dead but the process alive) → heartbeat plus the watchdog stale-leg mail (which already exists independently of the scheduler), plus a fake-clock test over a whole year including the 31 Dec/1 Apr dates |

---

## 6. Risks and experiments

| # | Risk | Status | Experiment that settles it (safe) |
|---|---|---|---|
| 1 | One file left open every night → rclone deletes nothing on LAN for weeks and the mirror grows | [LAB] behaviour confirmed; frequency on the box unknown | Step A: count source-read errors per night over 2 weeks (cloud logs already show it: grep "used by another process" in the box's cloud run_logs) |
| 2 | mtime or name differences between rclone and the NAS cause a full re-copy or permanent `*` differences | [UNSURE] | Step A dry-run against the real mirror (no writes) |
| 3 | The `--track-renames` hash over SMB is slow on a big rename (reads candidate files on both sides) | [UNSURE] | Step B: rename a 5 GB folder in the trial folder and time the dry-run |
| 4 | `--check-first` memory at 1-2.5M files on the DDR3 box | [UNSURE] | Step A: record the peak RSS of the dry-run (psutil in the harness) |
| 5 | /ZB was hiding ACL-denied files that rclone cannot read | [UNSURE], likely not (cloud rclone reads the same tree) | grep the box's cloud logs for "Access is denied"; compare file counts of the robocopy mirror and the source with `rclone check --one-way --size-only` |
| 6 | Scheduler catch-up/DST bugs in our own code | design | Fake-clock year test plus a reboot test on the box |
| 7 | GCS post-upload MD5 check assumed | [GEN] | One `-vv` upload to a test prefix (allowed: memory "real GCS final runs OK, test prefixes only") |
| 8 | `--backup-dir` fills the NAS | design | Space check includes the versions; purge by age; report its size in the weekly mail |
| 9 | A partial deletion up to `--max-delete` happens before the stop | [LAB] | Accepted with `--backup-dir` (recoverable); the G3 dry-run still blocks first |
| 10 | Rewrite churn breaks the ~1000 tests | design | The shim (4.5 step 1) and `lan.engine` keep both paths alive until each phase exits |

Local experiments run today (scratchpad only): long path, locked file, junction, empty dir, `--track-renames` moves,
`--backup-dir`, `--max-delete` exit 7, `--combined` with `--dry-run`, post-copy MD5 plus `.partial` rename. **Not run:**
anything on the NAS, on SMB or on Server 2016.

---

## 7. Owner decisions
1. Accept losing NTFS ACLs, attributes, creation times and `/ZB` on the NAS copy (restores come back with the share's
   default ACL)?
2. Approve the **shadow trial** steps A (no writes) and B (needs NAS space for a test folder, and a deliberate cable
   pull on the test folder)?
3. **`--backup-dir` on the NAS**: keep deleted and overwritten files for how many days (proposal: 60), and how much NAS
   space may it use?
4. With `--backup-dir` in place, may G3 be relaxed (higher small-tree floor), and may a G3-only block do a copy-only run
   (audit G1)?
5. A file left open every night: is "PARTIAL, deletions waiting, file named in the mail" acceptable, or should the
   office be told to close the application before 21:01?
6. LAN nightly bytes: accept bytes from the dry-run listing (no extra SMB walk), or a separate `rclone size` of the NAS?
7. Weekly LAN hash audit: the full read every Sunday, or a 7-day rotation if the full read is longer than the audit
   budget?
8. Scheduler: our own small loop (recommended), APScheduler (a new dependency), or Windows Task Scheduler?
9. Missed-run rule after a power cut: run once as soon as possible while the leg's budget lasts, else record MISSED
   and wait for the next night (recommended), or always run on boot?
10. Order: Phase 1 (rclone) before Phase 3 (Prefect), as proposed, or Prefect first?
