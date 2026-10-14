# Audit Lab — Empirical Verification Report

**Date:** 2026-10-10
**Subject:** AAM Backup Automation V1 — hypothesis-based audit lab (`audit_lab/`)
**Method:** every experiment executes the **shipped production code** (`flow.py`, `core/*`, `serve.py`, `launch.py`, `ui.py`, `watchdog.py`) against a fully isolated rig (mock source, loopback SMB `\\127.0.0.1\C$` as the NAS, an rclone **local** backend as the fake GCS bucket) — zero network, zero cost.

---

## 0. Where the evidence lives (read this first — persistence)

| Artifact | Path | Durable? |
|---|---|---|
| Evidence ledger (human-readable) | `audit_lab/LEDGER.md` | ✅ in repo |
| Raw evidence (647 rows, machine-readable) | `audit_lab/raw/evidence.jsonl` + `evidence_snapshot_2026-10-10.jsonl` | ✅ **now in repo** (was `%TEMP%\opencode\evidence.jsonl` — outside the repo, purgeable) |
| Test suite run logs (stdout) | `audit_lab/raw/wave*.log` | ✅ in repo |
| Test suite run logs (stderr/loguru) | `audit_lab/raw/wave*.err` | ✅ in repo |
| Lab harness + 23 test files | `audit_lab/*.py` | ✅ in repo |

> ⚠️ **`audit_lab/` is NOT tracked by git yet** (`git status` → `?? audit_lab/`). To hand the whole lab to a new session/agent/colleague:
> ```powershell
> git add audit_lab docs/audit/LAB_VERIFICATION_REPORT_2026-10-10.md
> git commit -m "audit lab: empirical verification, wave 8 (Prefect orchestration), findings report"
> ```
> A **new chat session starts with no memory** of this conversation — this file plus `LEDGER.md` plus `raw/evidence.jsonl` are the complete handoff. Re-run any experiment with the commands in §5.

---

## 1. What was done in this session

1. **Ran the full lab** (waves 0–7) twice: 53–55 experiments end to end, real subprocesses, real SMB, real Prefect API.
2. **Added WAVE 8 — Prefect orchestration** (`audit_lab/test_w8_prefect.py`, 7 experiments). Waves 0–6 drove `flow.backup()` in-process; Wave 7 needed the deployed box. Wave 8 closes the gap by driving the **deployment layer** (what the scheduler, the dashboard's "Run Backup" button and the Prefect UI "Quick run" actually call) against a real ephemeral Prefect API server with the production `aam-backup` concurrency limit.
3. **Added WAVES 9 & 10 — stress testing** (`audit_lab/test_w9_stress.py`, `audit_lab/test_w10_frameworks.py`, 10 experiments): Hypothesis over real I/O, randomised fault injection, metamorphic destination mutations, `cronsim` schedule verification, `hypothesis.stateful` model-based sequences, and a `sys.monitoring` coverage map. See §8.
4. **Fixed 6 defects in the lab itself** — several tests were silently testing the wrong thing (see §3).
5. **Investigated the 8 numbered issues one by one** against live code and the live deployed rig (see §2 and §4).

**Final suite state (waves 0–6 + 8): `59 passed / 2 skipped / 0 failing`** (14 min). Wave 7 is environment-blocked (§4).

---

## 2. Confirmed defects in the software

Severity is judged for *this* deployment (CA firm, single LAN NAS + GCS bucket, nightly runs).

### CRITICAL

**C-1 — Empty (dirs-only) source wipes the backup and reports success.** *Evidence: `D1.cloud_purge`, `C2.dirs_only_no_zb`, `D1.guard`.*
- rclone `sync` against a source with no files deletes **every object in the bucket**, exits 9 → `CLOUD_NO_CHANGES_COMPLETE` — which `last_successful_run()`, the dashboard and the weekly/monthly reports all count as a **success**.
- robocopy `/MIR` against a dirs-only source: 12 destination files → **0**, exit 2 → `LAN_COMPLETE`.
- The guard `check_source_drive()` (`core/health.py:33-49`) only requires **≥1 entry in the source root** — an empty subdirectory satisfies it. Verified live: `check_source_drive(<dirs-only tree>) -> ok=True`.
- The canary does **not** protect against this: `.AAM_TARGET_MOUNTED` is on the *destination* and is excluded from the mirror (`/XF`, `core/lan_sync.py:374`) so it **survives the purge** — the next preflight passes and the system reports healthy with an empty backup. The cloud leg has no canary at all.
- **Live state on this box:** source `C:\BackupAgent\mock_source\FY26-27` = 0 entries → the M8 guard refuses → all 3 real runs failed closed. **Currently blocked, but one empty subfolder away from triggering.**

**Fix:** require ≥1 *file* in the source (O(1) version: mandatory canary **file in the source root** — the existing error message already instructs this), plus a hard stop when the destination is non-empty and the source has no files, applied to **both** legs; and stop counting an empty destination as a success.

**C-2 — `/ZB` fails without `SeBackupPrivilege` → LAN leg copies 0 bytes.** *Evidence: `C1./ZB`, `D1.robocopy_exit16`.*
- The production command includes `/ZB` (`core/lan_sync.py:367`). For any account without `SeBackupPrivilege`, robocopy exits **16** *before copying a single byte*.
- `AamBackupAgent` logs on as `.\innovizta_2`; `whoami /priv` confirms that account has **no** `SeBackupPrivilege` (verified this session).
- Not yet observable in production only because every deployed LAN run dies at the health check first (see C-1). The first night the source has files, the LAN leg copies nothing.
- Also breaks 5 tests in the repo's own suite.

**Fix:** grant `SeBackupPrivilege` to the service account (least privilege — *not* Backup Operators on a domain controller), **or** run the service as LocalSystem, **or** drop `/ZB` (proven: identical command without it exits 1 and copies fine; the exit-8 → `LAN_PARTIAL` fail-closed path still works). Note `_assert_source_not_empty` (`lan_sync.py:329`) calls the same weak source check, so fixing C-1 is independent.

### HIGH

**H-1 — The daily "verification" cannot see same-size content corruption.** *Evidence: `B2.size_only_blind`.*
- A cloud copy with identical size **and** mtime but different bytes: daily gate → `verified=True`; weekly hash audit → `VERIFICATION_FAILED`, `content-changed=1`.
- The daily gate is an *accounting* check, and the dashboard labels it "Verification Passed".
- **The weekly audit does NOT heal it**: it is read-only (`core/integrity.py`, no sync/copy/delete flags) and rclone sync matches on size+mtime, so the corrupted file is never re-copied — the corruption is **permanent** until the source file changes.
- **It has never run on this box** — `integrity_audits` = **0 rows** in the live DB, so the size-only gate is the only verification that has ever executed here.

**Fix:** treat "Verification Passed" as an accounting status in the UI (it already has a separate integrity field — make the distinction unmissable), and on a weekly `VERIFICATION_FAILED`, re-copy the divergent paths (or at minimum alert with the explicit list).

**H-2 — The watchdog's wedged-lock alert is dead code.** *Evidence: `BUG-1.watchdog_alert`.*
- `watchdog.py::_alert_wedged_lock()` does `from core.notifications import send_failure_alert` — **that module does not exist** (`Test-Path core\notifications.py` → `False`). The real module is `core.report` (which `flow.py` imports correctly).
- The `ModuleNotFoundError` is swallowed by a bare `except Exception` and logged at **DEBUG**, so the CRITICAL "lock held by a live PID while the Prefect API is unhealthy" alert **never reaches the operator**.
- (The watchdog service is currently stopped/unstartable, so it isn't even running.)

**Fix:** `from core.report import send_failure_alert`; narrow the `except` and log at ERROR so a future import break cannot vanish again.

### MEDIUM

**M-1 — One failed backup can page twice.** *Evidence: `W6.alert_policy` (new, found this session).*
- `_run_cloud_pipeline` sends its specific gate alert (`flow.py:665`) then raises a plain `RuntimeError`; `backup()` sends the generic alert for every non-`PartialRun` exception (`flow.py:1336`). Same double-send on LAN `PARTIAL` (`918`), cloud `PARTIAL` (`757`) and both SUSPECT paths (`798`/`970`).
- Verified live this session: 2 alerts for one event.

**Fix:** wrap the gate failure in `PartialRun`, or keep a per-run "already alerted" flag and skip the generic alert.

**M-2 — A failed run is recorded as a success.** *Evidence: `BUG-2.lan_status`.*
- In `_run_lan_pipeline` the `except` handler (`flow.py:947-957`) rewrites `status` only for `phase == "sync"` and `phase == "pre"`. **There is no `phase == "post"` branch** (set at line 863 for the DB-record / artifact / shutdown step). So a post-sync exception (e.g. the DB write) leaves `status = "LAN_COMPLETE"` while the flow FAILS, and `last_successful_run("lan")` reports it as a success.
- **The cloud leg has the identical hole** (its `except` at 779-791 handles sync/verify/pre but not post) — the lab had only confirmed the LAN variant.

**Fix — fail closed in the handler:**
```python
except Exception as e:
    error_msg = str(e)
    if phase == "sync" and sync_result.get("status") in ("LAN_FAILED", "LAN_SUSPECT"):
        status = sync_result["status"]     # keep the sync task's own verdict
    else:
        status = "LAN_FAILED"              # pre, post, verify — anything else
```
Same shape for the cloud leg.

**M-3 — Memory/disk cost at scale.** *Evidence: `W2.manifest`, `W2b.combined`.*
- 200k objects: +108 MB RSS, 2.2 s, 24 MB pickled per task result. At 1M objects: **~1.05 GB live** and **~102 MB written into the Prefect result store per night** (~37 GB/year at 1M files). At 2.5M: ~2.6 GB.
- On an 80 GB box the **RAM is a non-issue**; the number that matters is the nightly result-store/disk write. Times are fine (lsjson ~10 s at 1M vs the 900 s timeout).
- **Fix:** set a Prefect result retention/storage cap, or stop persisting the two LAN snapshots + cloud manifest as task results.

**M-4 — Cloud/LAN FY divergence is possible.** *Evidence: `W4.1.fy`.*
- The config-load guard (`models/config.py:490`) only compares `source_drive` FY vs `lan_destination` FY. The **cloud prefix is computed from the calendar** (`get_fy_prefix()`, `flow.py:598`) and is not in that check.
- Verified live: today both are `FY26-27` → **no live divergence**. The risk materialises if the April-1 rollover fails to rewrite `config.yaml` (agent down — and it currently cannot start at all): cloud writes `bucket/FY27-28/…` nightly while LAN mirrors `…/FY26-27`, and the "CRITICAL DATA LOSS" guard still passes.
- **Fix:** assert `get_fy_prefix() ==` the config FY at the start of `flow.backup` (before either pipeline) and refuse/alert. ~10 lines.

### LOW

**L-1 — A source file robocopy cannot read is *purged* from the destination.** *Evidence: `W5c.locked_file`* (exit 11 → only `LAN_PARTIAL`; the previous good copy is gone for that cycle). The "Excel held open" case.
**L-2 — Mirror race on rename.** *Evidence: `W5b.rename_mid_run`* — a folder renamed during the run ends with 0 destination copies for that cycle (recovers next run).
**L-3 — Concurrent DB *initialisation* can still raise `database is locked`.** *Evidence: `LOCK.db_concurrency`* — the legacy dedup DELETE runs **before** `PRAGMA busy_timeout` is set (`core/manifest.py:150-162`). Insert-only contention is clean; this is intermittent.
**L-4 — `_clean_path()` is not idempotent** (`"0 /"` → `"0 "` → `"0"`). *Evidence: `F.clean_path_idempotence`.*
**L-5 — WoL/SMB probe is a bare TCP connect.** *Evidence: `W5c.smb_false_positive`* — a hung NAS that accepts connections reads as "online"; the canary + robocopy access errors are the real backstop.
**M-5 (test suite) — The committed suite is not green.** *Evidence: `W3`* — 34 failed / 1632 passed / 199 skipped / 40 errors on a clean unprivileged box; **19 failures are code-level** (`tests/test_f2_trigger_remediation.py` references `ui._trigger_result`, `ui._trigger_timeout`, `ui.TRIGGER_WAIT_SECONDS` — none exist in `ui.py`). The rest are environmental. Running it also appends to `docs/SCENARIO_TEST_REPORT.md` and drops a `MagicMock\…` tree in the repo root.

**W16.schedule_collision (NEW, LOW — found by Wave 16, 2026-10-12).** *Evidence: `W16.schedule_collisions`* — a cronsim sweep of the six production deployments across a full year (real `config.yaml` schedules, IST) shows `weekly-report` and `monthly-report` start in the **same minute** whenever the 1st of the month is a Monday: **2027-02-01, 2027-03-01, 2027-11-01, all 08:00 IST**. No backup-leg or audit collisions exist. Impact: two report flows run concurrently — two emails land in the same minute and the flows contend for the shared concurrency slot and the manifest DB (the M-6 exposure path for any audit-row writes). Test is red by design; fix by offsetting one cron (e.g. `45 8 * * *` for monthly) or serialising the report deployments.

**W5c source-loss timing note (behavioural, not a defect).** *Evidence: `W5c.source_disappears`* — when the source directory is renamed mid-run, the reported status is genuinely **timing-dependent**: identical production code produced `LAN_PARTIAL` (fail-closed detection firing) in one full run and `LAN_COMPLETE` (mirror finished before the rename landed) in the next. The contract the lab asserts is only "the destination is never wiped to zero"; the status is recorded as evidence. If deterministic fail-closed detection is wanted, the pipeline needs an explicit pre-flight/post-run source-exists check, not a rename race.

**M-6 (NEW — found by Wave 9 stress) — Concurrent `ManifestDB` opens silently lose run-history rows.** *Evidence: `W9.db_concurrency_stress`*
- 12 randomised rounds (2-6 concurrent writers × 5-30 inserts each): **4 rounds lost rows** — e.g. one round stored **45 of 60** rows, another **20 of 25**. No exception reached the caller in the losing case other than a logged warning path.
- Root cause, reproduced with a traceback: `ManifestDB._get_conn()` applies `PRAGMA busy_timeout` at `core/manifest.py:162`, **after** the legacy dedup `DELETE … ; commit` at lines 150-158. So the DDL write-lock acquisition at `conn.executescript(DDL)` (line 163) runs with SQLite's *default* 0 ms busy timeout and raises `OperationalError: database is locked`. A controlled experiment confirmed the socket is honoured once set (an 8 000 ms wait was observed), i.e. the problem is purely the *ordering*.
- `record_run_history()` (`core/backup_repository.py:93-111`) catches that and returns `False`, so the row is dropped **silently** — the backup runs, the flow may even succeed, and its run-history record simply does not exist. `last_run()`/`last_successful_run()`/reports then under-report.
- Real-life exposure: `ui.get_db()` holds a `ManifestDB` for the dashboard's lifetime, `integrity_audit_flow` opens one, and both nightly pipelines open their own — a concurrent open during a backup's write can lose that run's audit row.

**Fix:** move `PRAGMA busy_timeout` (and ideally `journal_mode`) to be the **first** statement executed on a new connection, before the dedup DELETE; and make `record_run_history` log at ERROR (not DEBUG) when it returns `False`.

---

## 3. Defects fixed in the LAB (the tests were not testing what they claimed)

| File | Defect | Symptom before | Now |
|---|---|---|---|
| `test_w6_flow.py` (rig) | never created the calendar-derived cloud bucket prefix | every cloud flow run failed preflight ("directory not found") — both-legs and retry tests red for a lab reason | prefix pre-created → 6/6 pass |
| `test_w6_flow.py` (deployments test) | called `serve.deployments()` from the sandbox CWD and never registered deployments server-side | `FileNotFoundError` / 404 `Deployment not found` | chdir to repo root + `aapply()` → pause/resume reconcile verified |
| `test_w6_flow.py` (alert policy) | monkeypatched the task with a plain lambda → `AttributeError` **before the real gate ran** | hypothesis never exercised | patch `.fn` on the real task → **produced finding M-1** |
| `test_w7_deployed.py` (d3) | referenced an undefined `body` | `NameError` | parses `/status` JSON |
| `make_ledger.py` | index table showed the **first** recorded verdict per experiment | stale verdicts after fixes | shows the latest verdict per experiment |
| `test_w8_prefect.py` (rig) | 30 s concurrency-slot wait caused flaky aborts under load | intermittent `min()` on empty evidence | 300 s (production is 3600 s) + hardened evidence capture |

---

## 4. Live deployment state on this box (verified 2026-10-10)

| Fact | Value |
|---|---|
| `AamPrefectServer` | **RUNNING** (LocalSystem) |
| `AamBackupAgent`, `AamWatchdog` | **STOPPED — cannot start: error 1069 "logon failure"** (NSSM's stored password for `.\innovizta_2` is invalid) |
| Registered deployments | all 6 (backup-cloud, backup-lan, weekly-report, monthly-report, rollover-check, integrity-audit) with the config's cron + timezone ✅ |
| Source `C:\BackupAgent\mock_source\FY26-27` | **0 entries** (empty). Sibling `FY25-26` holds 4 files. |
| LAN destination `\\127.0.0.1\C$\BackupAgent\mock_lan\FY26-27` | **1 file: the canary only** |
| `run_history` | 3 rows, all failed closed ("Source drive appears empty") |
| `integrity_audits` | **0 rows** — the weekly full verification has never run here |

**Consequence:** no backups are running at all (the agent is the Prefect worker *and* the dashboard host). This is why Wave 7 cannot pass: `arun_deployment` runs stay SCHEDULED (no worker) and `/status` is unreachable.

**Remediation (Administrator PowerShell):**
```powershell
.\audit_lab\setup_service_account.ps1 -AccountName ".\innovizta_2" -Apply   # fixes 1069 + grants SeBackupPrivilege
```
Then:
```powershell
.\.venv\Scripts\python.exe -m pytest audit_lab/test_w7_deployed.py -q -s --timeout=1800
```
Expect the LAN leg to fail with exit 16 until `SeBackupPrivilege` is granted — that is finding C-2 becoming observable on the real rig.

---

## 5. Reproducing / re-verifying anything in this report

```powershell
# whole hermetic lab (waves 0-6 + 8) — no services needed, ~14 min
.\.venv\Scripts\python.exe -m pytest audit_lab --ignore=audit_lab/test_w7_deployed.py -q -s --timeout=900

# only the Prefect orchestration wave, ~4 min
.\.venv\Scripts\python.exe -m pytest audit_lab/test_w8_prefect.py -q -s --timeout=1200

# regenerate the ledger from the raw evidence
.\.venv\Scripts\python.exe audit_lab\make_ledger.py
```

Spot-checks used in §2/§4 (read-only, safe to re-run):
```powershell
# C-1: the guard's real behaviour on a dirs-only source
.\.venv\Scripts\python.exe -c "from core.health import check_source_drive; print(check_source_drive(r'C:\BackupAgent\mock_source\FY26-27'))"

# C-2: does the service account hold SeBackupPrivilege?
whoami /priv | findstr /i SeBackupPrivilege      # no output = not held

# H-2: is the module the watchdog imports really missing?
Test-Path core\notifications.py                  # False
```

---

## 6. What the lab PROVED GOOD (no findings)

Fail-closed contracts hold: a killed robocopy → `LAN_SUSPECT` with the forensic log retained; a killed rclone with a forged exit 0 → caught by the cloud evidence gate (`CLOUD_VERIFY_FAILED`, alert sent, no false success) · ACL-denied / unreadable subtrees are **never** purged on either leg · 13 hostile lock payloads never crash the reader, PID reuse detected, a live foreign lock is never stolen · robocopy exit-code classification is total and consistent · hostile filenames (Unicode, emoji, reserved device names, trailing dots/spaces) mirror correctly on both legs · deep >260-char paths are copied and enumerated consistently · junctions are not followed (no runaway duplication) · crash + stale-lock recovery completes the next night · double-trigger serialisation works · the Prefect deployment/orchestration layer is correct end-to-end (new Wave 8, 7/7) · the rollover flow performs the real rollover and atomically rewrites `config.yaml` · the weekly/monthly report flows contain an SMTP outage without failing.

## 7. Recommended order of work

1. **Unblock the deployment** — fix the 1069 logon failure (`setup_service_account.ps1 -Apply`).
2. **C-2** — grant `SeBackupPrivilege` (same script) or drop `/ZB`.
3. **C-1** — require a file (canary) in the source; stop counting an empty destination as success.
4. **M-2** — fail the status closed in the post phase (both legs).
5. **H-2** and **M-1** — one-line import fix; stop the duplicate alert.
6. **M-6** — apply `PRAGMA busy_timeout` before the dedup DELETE (one-line reorder) so run-history rows stop vanishing.
7. **H-1** — make the weekly audit's verdict visible and self-healing (or explicitly labelled "detection only").
8. **M-4** — cross-validate the calendar FY against the config FY.
9. Re-run the lab + Wave 7 to confirm.

---

## 8. Wave 9 & 10 — stress testing with additional frameworks

You asked for more tools and real-life stress. Two new waves were added, using only what is already in the venv (nothing installed):

| Tool / technique | Wave | What it does |
|---|---|---|
| **Hypothesis over real I/O** (previously only pure functions were fuzzed) | 9.1/9.2 | generates random source trees (hostile names, nesting, 0-byte files, random content) and mirrors them through the **real** LAN and cloud pipelines |
| **Randomised fault injection** | 9.3 | share-none lock (deny-read), read-only attribute, deep >260-char paths, future mtimes, zero-byte files |
| **Metamorphic testing** | 9.4 | random destination mutation (delete / truncate / same-size corruption / extra object / mtime shift) then a second nightly run — which mutations does the mirror actually repair? |
| **`cronsim`** | 10.1 | computes the **real** next fire time of every deployment's cron in the configured timezone (the dashboard only shows English text, which says nothing about the timezone conversion actually applied) |
| **`hypothesis.stateful.RuleBasedStateMachine`** | 10.2 | model-based testing: random *sequences* of add/modify/delete/rename/corrupt with the invariant re-checked after each step |
| **`sys.monitoring` (PEP 669)** | 10.3 | line-level tracing of the production code during a real run → a **coverage/blind-spot map** |

### Results

**I1–I4 hold.** Across **225 random trees**, **275 fault-injected runs** and **159 metamorphic mutations**:
* every source file lands byte-identically (lossless) — `sha256` compared, not just file counts;
* a second run is a no-op (convergent);
* the destination never contains files the source does not have;
* **a success status never hides missing or altered data** — under share-none locks, read-only attributes, deep paths, future mtimes and zero-byte files the run either fails closed or is byte-identical. This is the invariant that C-1/B-2 would break.

**Specifically verified:** junctions are excluded by `/XJ` — 0 files copied through them, no recursion, no duplication, while the real tree still mirrors exactly (`W9.junction_excluded`). Same-size destination corruption is **not** repaired by the next nightly run and is still reported as success (`W9.mutation_repair`) — finding H-1 reproduced as a property rather than an anecdote. All other mutations (delete, truncate, extra object, mtime shift) are repaired.

**Schedules verified (`W10.schedules`, OK).** All 6 deployments fire at the intended *local* wall-clock time in `Asia/Kolkata` — backup-lan 01:00, backup-cloud 18:00, rollover-check 06:00, weekly-report Monday 08:00, monthly-report the 1st at 08:00, integrity-audit Sunday 03:00 — and across ~13 months of simulated firings the two backup legs never collide. *(The experiment itself also documents a trap: seeding a cron simulator in UTC instead of the configured zone falsely suggests a 5 h 30 m shift — a useful guard when the timezone is ever changed.)*

**Coverage / blind-spot map (`W10.line_coverage`)** — production lines executed during one real LAN + cloud run:

| module | executed | note |
|---|---|---|
| `core/cloud_sync.py` | 91/94 (97%) | |
| `core/cloud_preflight.py` | 28/43 (65%) | |
| `core/cloud_verify.py` | 49/67 (73%) | |
| `core/lan_preflight.py` | 38/56 (68%) | |
| `core/lan_sync.py` | 101/162 (62%) | |
| `core/health.py` | 46/77 (60%) | |
| `core/manifest.py` | 114/205 (56%) | |
| `flow.py` | 362/521 (69%) | |
| `core/integrity.py` | **0/145 (0%)** | the weekly audit code has never executed here — consistent with `integrity_audits = 0` on the deployed box |

**New finding: M-6** (concurrent `ManifestDB` opens silently drop run-history rows) — see §2. This is the strongest outcome of the stress waves: a data-integrity defect in the audit trail that only shows up under concurrency, not in any single-run test.

### Reproducing

```powershell
.\.venv\Scripts\python.exe -m pytest audit_lab/test_w9_stress.py audit_lab/test_w10_frameworks.py -q -s --timeout=1800
```
