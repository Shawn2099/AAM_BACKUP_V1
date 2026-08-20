# SESSION 2 — Final Reconciliation Report

**Date:** 2026-08-20 (IST)
**Companion documents:** `SESSION_2_INDEPENDENT_AUDIT.md` (independent findings register), `SESSION_2_AUDIT_PROGRESS.md` (evidence index)
**Prior material reconciled (read in Phase 6, AFTER forming all independent findings):**
`AAM_AUDIT_REPORT_2026-08-18.md` (the 18-finding principal-architect audit), `SESSION_1_HANDOFF.md` (fix list F1/F12/A1/P1/P2a-f/L1/G1-2 + open items O1–O11 + T1–T9 live test matrix), `docs/audit/AUDIT_REPORT.md` + `LIBRARY_AUDIT_2026-06-02.md` + `REVIEW_2026-06-02.md` (June audits), `archive/production_hardening_review_final.md` (P0#1–3, P1#4–7, P2#8–10), `archive/ARCHITECTURE.md`, `.planning/*`, `_regression_full*.log`, `tests/REAL_WORLD_TEST_PLAN*.md`.

---

## 1. Scope and method

Independent fresh-eyes audit of AAM Backup V1 (dev checkout `AAM_BACKUP_V1` @ `d27198c` on `reliability-2026-08-20`; deploy `C:\AAMBackup` @ `2bb8024`, content-identical source). Nine-phase protocol: discovery → file-by-file audit → interaction tracing → **real execution on the authorized test environment** (real GCS bucket, real NAS `10.10.186.231`, real robocopy/rclone, NSSM service experiments, fault injection) → own findings register → *then* prior-material review → comparison → classification → verdict. No product code was modified; all experiments used isolated destinations (`AUDIT_S2_*` GCS prefixes, scratch NSSM service, temp files) and are cleaned up except documented test-pollution findings.

Bias control: findings register was completed and frozen before any prior audit document was opened; classification below was done afterwards. Where my findings pre-date reading theirs, that is noted.

## 2. What I verified by execution (evidence index)

| # | Experiment | Outcome |
|---|---|---|
| E-suite | Full pytest suite, pristine uv env on production-parity Python 3.12.3 | **1439 passed / 53 skipped / 0 failed** (reproduced ×2; matches session-1's log exactly) |
| E-suite2 | Skip-reason analysis | All 53 skips = `AAM_RUN_REAL_HARDWARE` gate |
| E-rthw | Real-hardware suite (7 rt files) from `C:\AAMBackup` on prod venv, gate on | **47 passed / 1 failed** — failure was my own concurrent rclone (test pollution); isolated rerun passed. Includes **real NAS** golden path, mirror-delete, canary, **locked-file → exit 8 → LAN_PARTIAL with `files_failed=1`**, 50 MB hash integrity, cloud sync/verify/record vs real GCS, rollover detect/atomic-config, full pipelines |
| E1 | `--modify-window 2s` same-size resave (real GCS, project's real sync command) | **CONFIRMED permanent silent skip** (details §5/S2-30) |
| E5 | Disposable NSSM service, production chain `NSSM→uv→python→rclone`, production stop methods, stop mid-transfer | **No orphaning**: whole tree dead ≤5 s; no partial GCS object. Scratch service removed |
| E8 | Local STARTTLS SMTP server, 1 of 2 recipients refused (550), real `_send_email_with_attachments` | **CONFIRMED false success** (returned True, "Email sent" logged) |
| E-live | Prefect API, manifest.db, GCS bucket metadata, dashboard `/status`, NSSM config, machine PATH | 5 active deployments; 111-run history; versioning ON + noncurrent-only lifecycle (90 d live / 92 d repo); LAN last success 07-10, cloud 08-18; two rclone versions (system32 1.74.2 / bin 1.74.3) |
| E-log | Live agent logs 2026-07-10/11 21:00 runs | Raw `PermissionError [WinError 5]` from `lan-preflight` canary `exists()` — matched to CPython 3.12.3 `pathlib` source (WinError 5 not in ignore set) |

## 3. Findings CONFIRMED by both sessions (convergent)

| ID | Topic | Prior source | My independent evidence |
|---|---|---|---|
| C-1 | Suite green, 1439/53 | SESSION_1_HANDOFF §1 | My pristine-env run, identical counts (190 s / 194 s / their 180 s) |
| C-2 | F12 `files_failed` fix works against real robocopy | Session-1 T3 live | My rt_01 locked-file run: exit 8 → `LAN_PARTIAL`, `files_failed` parsed from job summary (F12 code path executed live) |
| C-3 | Real-hardware tests gated (F4/F5) | 08-18 F4/F5, session-1 | Verified skipifs + 53/53 skips are gated tests |
| C-4 | rollover-check deployment served (L1) | Session-1 L1 | Live API: 5 deployments incl. `rollover-check` `0 6 * * *` active |
| C-5 | F1 verify-failure now changes status + alerts + fails run | 08-18 F1 | Code: `CLOUD_VERIFY_FAILED` + raise (flow.py:469–505); rt_06 pipeline tests pass live |
| C-6 | F3 NAS shutdown gated on LAN_COMPLETE + PARTIAL alert | 08-18 F3 | Code: flow.py:659–691 (but see D-3: over-corrected) |
| C-7 | F6 watchdog self-starts STOPPED service via `sc start` | 08-18 F6 | Code: watchdog.py F6 comment + `sc start` path (L225–243, L440) |
| C-8 | F8 LAN preflight timeout config-driven | 08-18 F8 | Code: `config.lan.dry_run_timeout_seconds` (flow.py:230) |
| C-9 | F15/F16 (seek-from-end log tail; monotonic duration) | 08-18 F15/F16 | Code: lan_sync.py F15 comment + `f.seek(size-max)`; flow.py F16 + `time.monotonic()` |
| C-10 | F18 suite was red → now green | 08-18 F18 | My run green; their `_regression_full2.log` identical |
| C-11 | GCS lifecycle safe for current data (noncurrent-only rules) | 08-18 §4/F17 | Live bucket metadata: rules noncurrent-only; versioning ON |
| C-12 | June findings fixed (S1 XSS escaping, S2 rate limit, C2/C4/C5/C6/C7/C8) | June AUDIT_REPORT | Code: escapeHtml on all dynamic fields; `_check_rate_limit` wired; atomic lock; etc. |
| C-13 | Hardening P0#1–3 fixed | hardening review | Code: lock inside concurrency block; ui TTL + DB eviction; watchdog no-unlink |
| C-14 | 07-10/11 LAN failures were environmental (NAS FY26-27 folder state) | dashboard/log history | Logs + dashboard: `Source drive not accessible` / WinError 5 on the canary; source folder recreated 08-20 08:17 |
| C-15 | P2b logging bridge survives reconfigure | Session-1 P2b | Code: bridge sink restore + per-run-id cache (cap 128) |
| C-16 | O5 (gcloud side-effect in cloud_sync) | Session-1 O5 | **RESOLVED**: no gcloud reference in `core/cloud_sync.py` anymore; only the intentional rollover call (fy_rollover.py:340) remains |
| C-17 | O10 (real NAS untested) | Session-1 O10 | **SUPERSEDED**: my rt_01/rt_06 runs exercised the real NAS share (`\\10.10.186.231\lan_backup\E2E_TEST_DEST`) — golden path, mirror-delete, fault injection all pass |
| C-18 | O9 (loguru atexit noise, exit 0) | Session-1 O9 | Observed in my runs too (`_s2_pytest_skips.log` tail); harmless, still present |

## 4. DISAGREEMENTS (and resolution)

### D-1 — `--modify-window 2s` is "correct" (08-18 §4) vs my E1: **DISAGREE — mine is right**
08-18 §4: "GCS flags (--fast-list, --modify-window 2s, --check-first, --error-on-no-transfer) are correct and consistent with NTFS granularity." That acceptance was reasoned, not negative-tested. My E1 (real GCS, the project's own sync command) shows the window's failure direction is inverted: a same-size resave whose mtime lands within 2 s of the object's mtime is **never re-uploaded — ever** (verified permanent across repeated syncs). Size-change and >2 s mtime controls both re-upload, isolating the window as cause. This is a real (narrow-trigger) data-integrity hole: **S2-30, Medium.**

### D-2 — Double verify-failure alert: session-1 T6 logged "2 alerts emailed" as the passing outcome; I classify it as a defect
The F1 fix (pipeline alert + raise → flow summary alert) deterministically sends two emails per verify failure (M2/S2-02). They observed it in T6 and accepted it; I rate it Medium (alert fatigue + doubled `[ALERT_NOT_DELIVERED]` bookkeeping). **Disagreement of judgment, not fact.**

### D-3 — F3 remediation over-corrected (my M1 is a regression of their fix)
Their F3 recommendation ("gate shutdown on COMPLETE; alert on PARTIAL") was implemented correctly for exit 8–15 but **not** for the anomaly-only 4–7 case, which their own codebase defines as *complete backup, no alert* (`core/lan_sync.py:248–251, 309`) and which their own P1 fix (fy_rollover) honors via `exit_code & 8`. The flow's LAN_PARTIAL branch alerts and skips NAS shutdown for 4–7 anyway → false nightly "files were not copied" alerts. **S2-01, Medium.** Neither prior audit caught it (the 08-18 audit pre-dated the fix; session-1's own P1 comment shows the team knew the distinction).

### D-4 — Severity of NSSM stop behavior
Their O8 (observed live): `nssm stop`/SCM can leave the service STOP_PENDING (control dispatcher blocked), needing host-kill workaround. My E5 observed the *clean* path (whole tree dies ≤5 s, service Stopped by T+276 s) and did not reproduce the hang. **Not a disagreement — complementary**: the hang is a separate, intermittent SCM-state failure mode that stays open (ops P2, consider NSSM upgrade / `AppKillProcessTree` evaluation — note `AppKillProcessTree` is available in this NSSM build but not configured).

### D-5 — "Production Ready" claims in `.planning/STATE.md` (156 tests, 2026-05-27)
Stale by three months and three test-generation turnovers. Misleading but superseded; no action beyond hygiene (see S2-25 class).

### D-6 — F11 config defaults: they called the demo-bucket default dangerous
Partially remediated (bucket default now `""`), but `project_number` default `"920173882190"` persists (S2-17). I would close F11 as *partially fixed* with S2-17 + S2-18 (cron defaults) + S2-19/silent-unknown-keys as the remainder.

## 5. NEW findings (session 2 only — not in any prior audit)

**Medium:**
1. **S2-30 — `--modify-window 2s` permanent same-size skip** (see D-1; reproduced, permanent, byte-verified).
2. **S2-11 (M4) — SMTP partial-recipient refusal → false "Email sent"** (reproduced with real code).
3. **S2-12 (M5) — raw WinError 5 from unguarded `Path.exists()` canary** (live logs 07-10/11 + CPython 3.12.3 source + probes). Bypasses the G11 self-recovery message.
4. **S2-13 (M6) — rclone version drift: system32 v1.74.2 (sync) vs deploy\bin v1.74.3 (preflight/verify)** via bare-`"rclone"` in `build_rclone_sync_command`. Drift already manifested.
5. **S2-01 (M1) — anomaly-only exit 4–7 alerted as "files were not copied"** (see D-3).
6. **S2-03 (M3) — concurrency-slot timeout (1 h) → `TimeoutError` with no email and no run_history row** (Prefect 3.7.2 source-verified; only visible in Prefect console). The one failure mode invisible to every operator channel the system provides.
7. **S2-20 (M7) — `test_fy_07` would `rmtree` the live `E:\FY26-27`** on a production-like checkout (excluded from my run).
8. **S2-26 (M8) — DEPLOYMENT_GUIDE wrong config key names** (`notification.*` vs `notifications.*`, `email_from/to` vs `sender/recipients`) + unknown keys silently ignored → operator following the guide silently misconfigures alerting.
9. **S2-15 (M9) — generation-pinned restore vs 90-day noncurrent deletion**: `restore_manifest.json` generations 404 once they age 90 days as noncurrent; point-in-time DR silently degrades (current-version fallback always exists but is undocumented).

**Low:** S2-04 (`files_failed=0` hardcoded for cloud), S2-05 ("Cryptographic Checks: Passed" label for size-only verify), S2-06 (rollover flow takes no slot/lock), S2-07 (cloud retry = up to 3×6 h, slot held — feeds M3), S2-08 (UI blind to mode="all"), S2-09 (`/health` exposes source path), S2-10 (cookie Secure flag), S2-14 (lifecycle 92 repo vs 90 live — see §7/F17), S2-16 (dev venv broken; 3.14 vs 3.12 drift), S2-17 (project_number default), S2-18 (cron defaults 18:00/01:00 vs 21:00/22:00), S2-22 (PENDING runs always cancelled at startup), S2-23 (dead tag-based limit), S2-24 (watchdog transfer detection matches any rclone/robocopy — proven by my own wd_05 false positive), S2-27 (plaintext SMTP app password on disk, not in VCS), S2-28 (`wmic` in 09_restore bat), S2-29 (uninstall taskkill breadth), S2-32 (pre-phase failures recorded `*_SKIPPED` = F2 residual), S2-34 (ambient gcloud auth in rollover).

**Info/negative:** S2-31 (E5 negative: no orphaning on stop), S2-25 (stale ARCHITECTURE.md), S2-35 (test pollution in live destinations — GCS junk prefixes, NAS E2E folders, source-folder test dirs; root cause: e2e cleanup only under `__main__`).

## 6. Session-1 fixes — verification status

| Fix | Claim | My status |
|---|---|---|
| F12 | files_failed from job summary | **CONFIRMED** (live rt run, exit 8 → count parsed) |
| F1 | verify labels unswapped + status change | **CONFIRMED in code**; live label-direction not re-sabotaged (their T6 evidence accepted) |
| A1 | `[ALERT_NOT_DELIVERED]` annotation | **CONFIRMED in code** (all 4 sites + idempotent marker); live double-failure not re-performed (their T9 accepted) |
| P1 | rollover blocks on `& 8` PARTIAL | **CONFIRMED in code** — and reveals the F3-branch inconsistency (D-3) |
| P2a | WalkIncompleteError | **CONFIRMED in code** (lan_manifest) |
| P2b | bridge sink survives reconfigure | **CONFIRMED in code** |
| P2c | test loguru isolation | **CONFIRMED** (conftest no-op patch; my runs never wrote prod logs) |
| P2d | report send result checked | **CONFIRMED in code** |
| P2e | `/status` DB off event loop | **CONFIRMED in code** |
| P2f | empty api_key fail-closed | **CONFIRMED in code** |
| L1 | rollover-check served | **CONFIRMED live** (5 deployments) |
| G1/G2 | gcloud script committed; .gitignore scratch | **CONFIRMED** (git ls-files / git status clean) |

## 7. Prior (08-18) findings — status in current code

| 08-18 ID | Status (my verification) |
|---|---|
| F1 | **Fixed** (residual: my M2 double alert) |
| F2 | **Partially fixed** — sync/verify failures get true statuses; pre-phase (health/WoL/preflight) still recorded `*_SKIPPED` (my S2-32) |
| F3 | **Fixed** (regression: my M1 anomaly-only over-alert) |
| F4/F5/F18 | **Fixed** (gates + green suite, live-verified) |
| F6 | **Fixed** (`sc start` for STOPPED service) |
| F7 | **Partially fixed** — `_error`/`_partial` markers added but not consumed by callers (their O2 remains); zeros still shown on listing timeout (low impact at 572-object scale) |
| F8 | **Fixed** (config-driven timeout) |
| F9 | **Addressed differently** — AU Active Hours 12:00→06:00 (18 h window) now carries the headless-protection claim, documented honestly in DEPLOYMENT_GUIDE; old policy kept as belt-and-braces. Acceptable. |
| F10 | **Fixed** (P2b) |
| F11 | **Partially fixed** (bucket `""`; project_number + cron defaults remain — S2-17/S2-18) |
| F12 | **Fixed** (files_failed; MD5 scope still open — PENDING_CHECKSUM remains, documented) |
| F13 | **NOT RE-VERIFIED** (ui TTL/DB-eviction race; low probability, low impact; no evidence of occurrence) |
| F14–F16 | **Fixed** (F14 ordinal not re-checked — cosmetic; F15/F16 verified) |
| F17 | **Partially applied — NEW INSIGHT**: the repo lifecycle file was edited 90→92 days (exactly their recommended remedy: 92 days in COLDLINE avoids the early-delete penalty) **but the live bucket was never updated (still 90)** → the cost penalty they identified is still active in production. My S2-14 records the drift; the correct reading is "fix authored, deployment of fix missing." |

## 8. Open items from session-1 handoff — status

| O# | Status |
|---|---|
| O1 | Still present (bucket junk outside FY26-27/; my AUDIT_S2_* prefixes removed during this audit). Decision still needs user approval |
| O2 | Still open (marker unconsumed) — confirmed by my code read |
| O3 | Not re-verified (cosmetic) |
| O4 | Not verified (untested path) |
| O5 | **Resolved** — gcloud call no longer in cloud_sync.py (C-16) |
| O6 | Not verified (clean-exit flush path only) |
| O7 | Not re-verified (self-resolved by their account) |
| O8 | **Still open** (intermittent; my E5 did not reproduce the STOP_PENDING hang but didn't stress the control dispatcher) |
| O9 | Still present, harmless (observed again) |
| O10 | **Superseded** — real NAS now exercised (C-17) |
| O11 | Unchanged (scratch/ dir left as-is) |

## 9. Combined open / NOT-VERIFIED list (both sessions)

1. Concurrency-lease loss during >5 min Prefect API outage mid-backup (slot re-acquirable) — NOT VERIFIED (needs induced outage).
2. NSSM stop STOP_PENDING hang (O8) — observed by session-1, not reproduced by me; keep open with NSSM-upgrade/AppKillProcessTree evaluation.
3. Real-world frequency of robocopy exit 4–7 on this NAS — NOT VERIFIED (111-run history contains none).
4. M9 404 after 90-day version expiry — NOT VERIFIED (time).
5. F13 ui TTL/DB-eviction race — NOT RE-VERIFIED.
6. O3/O4/O6/O7 — low, unverified, carried.
7. Live lifecycle rule 90 vs repo 92 — fix exists in repo, **not applied to bucket** (see §7/F17).

## 10. Severity re-classifications vs prior

- **--modify-window 2s**: prior "correct" → **Medium data-integrity finding** (S2-30) — my strongest disagreement.
- **Verify double-alert**: prior "observed, accepted" → **Medium** (M2).
- **Concurrency timeout silence**: prior "comment typo only" (F16) → **Medium** (M3) — the exception-type observation in F16 was exactly right; the operational consequence (no alert, no DB row) was never raised by either prior audit.
- **F17 lifecycle cost**: prior P3 → **P2-ish**: fix authored (92 d) but never deployed to the live bucket; penalty accruing.
- **O10**: prior "user action needed" → closed (I ran the real-NAS suites).
- No prior finding was downgraded.

## 11. Data-integrity assessment

- Mirror semantics proven live both directions (LAN: mirror-delete, locked-file partial + count; cloud: add/modify/purge, verify, DB prune). GCS versioning ON + noncurrent-only lifecycle = current data safe from auto-delete (FACT).
- **Gaps:** (a) S2-30 same-size resave skip (cloud only, narrow, permanent until size/mtime shifts); (b) size-only verify by design (no checksums — accepted trade-off, but the "Cryptographic Checks: Passed" label overstates it); (c) M6 two rclone binaries per pipeline (drift already occurred).
- 572/572-file dataset verified intact after the August purge/restore (session-1, re-confirmed by dashboard + bucket listing this session).

## 12. Alerting & observability assessment

The alerting architecture is the system's strongest feature (4 alert sites, CRITICAL-on-failure annotation, `[ALERT_NOT_DELIVERED]` trace) — but it has three holes, all independently found by me:
1. **M4** — partial SMTP refusal reports success (reproduced).
2. **M3** — concurrency timeout fails with zero operator-visible signal (source-verified).
3. **M5** — WinError 5 bypasses the self-recovery message (live-confirmed).
Plus M1/M2 (over-alert / double-alert) that cut the other way. Net: *most* failures are visible; the three holes are all "quiet" failure modes.

## 13. Operational / deployment readiness

- Services, schedules, watchdog, dashboard, DR scripts all present and mostly correct on a Windows Server 2016 box (the documented EOL target — ES ends 2027-01-12, inside the 5-year retention; guide's upgrade advice stands).
- Deploy scripts are clean and sequenced; two ops nits (S2-28 wmic, S2-29 taskkill breadth, S2-27 plaintext SMTP password on disk).
- **Docs debt is the biggest operational risk**: DEPLOYMENT_GUIDE wrong key names (S2-26/M8) + silent-unknown-keys validation means a fresh deployment following the guide will silently misconfigure alerting; ARCHITECTURE.md stale; STATE.md stale; lifecycle fix not applied to live bucket.
- Live system state at audit time: healthy, but 08-19 runs SKIPPED (source folder was mid-restore) and the NAS FY26-27 folder state that caused 07-10/11 failures should be confirmed before tonight's 21:00/22:00 runs (environmental, not code).

## 14. Test infrastructure assessment

- 1492 tests: 1439 green in ~3 min on a clean env; reproducible (3 independent runs across two sessions and two interpreters — prod venv and pristine uv env).
- All real-hardware coverage is gate-gated (53 tests) and I ran it: 47/48 pass (1 self-inflicted). The real-hardware layer is the best part of the suite — zero mocks, isolated `E2E_TEST_*` destinations, log assertions.
- **Two defects in the test layer itself**: S2-20 (test_fy_07 destructively cleans up the live source folder on a production-like checkout) and S2-35 (e2e pollution accumulates in live destinations because cleanup only runs under `__main__`).
- Dev venv broken (S2-16): "run pytest from the checkout" fails until a fresh env exists — a real onboarding/CI trap.

## 15. FINAL VERDICT

### **PRODUCTION READY — WITH CONDITIONS**

**Basis.** The core backup contract is demonstrably met on real hardware: nightly LAN mirror + cloud sync with verification, correct failure statuses for sync/verify failures, real alerts with delivery tracing, safe FY rollover, live-recovered services, and a reproducible 1439-test green suite plus a real-hardware suite that passes on the actual NAS/GCS. No silent data loss was found; no uncontrolled destructive path in production code; the August incident (purged cloud data) was fully recovered and is evidenced as such.

**Conditions (must-fix before relying on the system for the next 12 months — all small, surgical changes):**
1. **M3** — concurrency-timeout: record run row + alert (currently the one fully-silent failure mode).
2. **M4** — SMTP partial-refusal: consume `sendmail()` return; surface per-recipient failures.
3. **M5** — guard `Path.exists()` (lan/cloud preflight) → HealthError with recovery message.
4. **M1** — LAN_PARTIAL branch: honor `returncode & 8` (alert + skip-shutdown only for real copy errors; 4–7 = warning + shutdown per the core contract).
5. **S2-30** — remove/tighten `--modify-window 2s` (or add explicit same-size-change detection) after operator sign-off on the trade-off.
6. **M6** — `build_rclone_sync_command` must use `resolve_binary("rclone")`; align or remove the system32 rclone copy.
7. **M8** — fix DEPLOYMENT_GUIDE key names; add `extra="forbid"` (or at minimum warn on unknown root keys).
8. **S2-14/F17** — apply the 92-day lifecycle to the live bucket (the fix exists in-repo; deploy it).
9. **S2-20** — make test_fy_07 abort if its targets equal live config paths (the "run the acceptance suite on the server" workflow must not rmtree production data).

**Should-fix (next maintenance window):** M2 (dedupe verify alert), M7→covered by #9, M9 (restore fallback + document 90-day horizon), S2-07 (bound total retry time), S2-06 (rollover slot), S2-22 (PENDING-cancel consideration), S2-24 (scope watchdog transfer detection), S2-27 (move SMTP password out of the source tree), O8 (NSSM stop hardening).

**Not a blocker:** all remaining Low/Info items; documented trade-offs (size-only verify, missed-run policy, WU active-hours approach).

## 16. Recommended remediation order (for the user)

1. One flow.py pass covering M3 + M1 + M2 + S2-32 (pre-phase true statuses) — they all touch the same two functions; add regression tests: concurrency-timeout (assert run row + alert fired), anomaly-only exit 4 (assert NO alert, shutdown executed), verify-fail (assert exactly ONE alert).
2. core/report.py: M4 (sendmail return dict) + test with a partial-refusing SMTP server (my E8 script is a ready-made fixture).
3. core/lan_preflight.py + core/cloud_preflight.py: M5 exists() guard (2-line change each) + unit test with a Path whose stat raises PermissionError.
4. core/cloud_sync.py: M6 resolve_binary (1 line) + delete/reconcile system32 rclone (ops).
5. cloud sync flags: S2-30 decision (remove `--modify-window 2s` and re-run rt_02 real suite as proof of no false-positive re-upload storms).
6. Docs: M8 guide fix, S2-14 lifecycle apply (`gcloud storage buckets update ... --lifecycle-file=deploy/gcs_lifecycle.json`), S2-25/S2-26 stale-doc sweep, STATE.md refresh.
7. Tests: S2-20 guard, S2-35 (move e2e cleanup into a module teardown), dev-venv repair instructions in DEPLOYMENT_GUIDE.

---

### Auditor's notes on process
- No product code was modified at any point; every experiment ran against isolated destinations and my scratch artifacts (NSSM service, GCS prefixes) were removed after use.
- Three of my findings were initially hypotheses and were promoted to FACT only after execution (S2-30, M4, M5) or source-level proof (M3). Where I could not execute (lease loss, O8 hang, M9 404, exit-4-7 frequency), the report says NOT VERIFIED rather than speculating.
- The single most valuable fresh-eyes contribution of this session: the three *quiet* failure modes (M3, M4, M5) — exactly the class the 08-18 audit framed the system's risk around ("the remaining risk concentrates … in the failure paths") — two of which neither prior audit found, plus the one data-integrity gap (S2-30) in a flag the 08-18 audit had blessed.
