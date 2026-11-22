# Release break-it session - PART 2 (newest; read PART 1 `RELEASE_BREAK_SESSION_2026-11-22.md` first for rules/env, then this)

Written 2026-11-22 ~22:30 IST because the session was about to hit its limit. Everything below happened AFTER Part 1.

## A. State of the test stack on this PC (C:\AAMTest)
- Services **installed and Running**: AamPrefectServer, AamBackupAgent, AamWatchdog, run from the worktree `C:\AAMTest\code` (branch `release-candidate`), service account `.\aamtest`,
  config `C:\AAMTest\config\config.yaml`, runtime `C:\AAMTest\runtime`, dashboard http://127.0.0.1:8080 (passwords in `C:\AAMTest\secrets\dashboard.txt` - never print/commit), Prefect http://127.0.0.1:4200.
  Venv built with `UV_PYTHON_INSTALL_DIR=C:\ProgramData\uv\python`. 6 Prefect deployments exist. Details/commands/uninstall/AAMMerger-restore: `C:\AAMTest\INSTALL_REPORT.md`.
- Config deviations for this test PC: `lan.use_backup_mode: false` (aamtest has no SeBackupPrivilege), `dashboard.public_url` set, bind 127.0.0.1, W32Time started.
- **First real cloud backup: CLOUD_COMPLETE** (41 files, 21 MB, verified) to bucket `aam-backup-qa-bilcvx`.
- **First LAN backup: LAN_BLOCKED by guard G4_DESTINATION_IDENTITY** (destination \\127.0.0.1\AAMTestNAS is this machine; expected for loopback). Approval id `9069b43e32ca25bb29354938dbd60f58` is PENDING.
  The install agent's attempt to approve via the API was refused by the permission classifier: **do not work around it.** Two legitimate options: (1) the OWNER approves on the dashboard (type APPROVE),
  or (2) set the documented config key `guards.forbid_local_destination: false` in `C:\AAMTest\config\config.yaml` (models/config.py ~754, default true; made for this exact loopback test setup), restart AamBackupAgent, re-run LAN.
  Option 2 also leaves G4 itself untested, so also keep one run with the default to prove it blocks (it did).
- Not yet verified on the stack: bucket listing (the key file is locked from user malliath - use the service account or `rclone` as aamtest), NAS files (none yet), `C:\AAMTest\mails.jsonl` (nothing seen yet).
- IMPORTANT: the services execute the code in `C:\AAMTest\code`. Commits made there (see C) only take effect after `Restart-Service AamBackupAgent` / `AamWatchdog` (elevated).
- AAMMerger is STILL STOPPED + DISABLED (restore steps in Part 1 section 6 and INSTALL_REPORT.md). Elevated queue + worker: see Part 1 section 6 (worker may be dead in a new session; a UAC click restarts it).

## B. FY overlap deep review results (Opus review in `docs/OVERLAP_REVIEW_2026-11-22.md`; test design = 37 cards)
Tests written (all in main checkout, UNTRACKED until committed; run each alone with `.venv\Scripts\python.exe -m pytest <file> -q -p no:cacheprovider -o addopts="" --timeout=900`):
| File | Cards | Result at write time (pre-fix code) |
|---|---|---|
| `tests/test_ov_timeline_a.py` | T-a1..a6, P-3, P-4, P-12 | 8 passed, 4 xfailed (D3 x3, D5 x1) |
| `tests/test_ov_timeline_bc.py` | T-b1,b2,T-c1..c5 | 6 passed, 1 xfailed (D1) |
| `tests/test_ov_review_points.py` | P-1,2,5,6,8..11,13,T-b4 | 8 passed, 4 xfailed (D6, D8, D12, D14) |
| `tests/test_ov_timeline_def.py` | T-d1,d2,e1..e3,f1..f3,P-7 | 10 passed, 4 xfailed (D9 T-e1 - already fixed in release-candidate by fix #13, D10, D2 T-f3, D7 P-7); T-f4 (live) skipped. Verified: early folder never created / no create right -> final backups still run, switch blocked, WARNING; overlap OFF all year = old behaviour; overlap toggled off/on mid-window; interrupted rollover (crash after config switch) resumes next morning; archive failure retries only `gs://b/<old FY>/` never the new FY. Non-vacuity mutants all went red. NOTE: T-d1 sends two near-duplicate mails on 1 Apr (WARNING AAM-ROL-04 + later ACTION NEEDED for the same cause): cosmetic, owner to judge |

NOTE for the final merge into `fy-overlap`: the four `tests/test_ov_*.py` files (and `test_break_release_{ops,approvals,s1}.py`) are UNTRACKED in the main checkout but also exist (with xfails flipped) in release-candidate / the fixer branches: DELETE the untracked copies before `git merge release-candidate`, or git refuses to overwrite them. Take the release-candidate versions.
Verified OK by those tests (dual-folder logic is sound): Jan 1 activation once (INFO mail, folder+placeholder, run order new FY first), no duplicates, NAS folder+marker created, one real wake/shutdown per night, NAS stays on after a partial night,
Jan-Mar failure on ONE year only is truthful (own approval, RECOVERED threads correctly, other year stays green), April 1 rollover with an early year holding 90 days of history (final backup of the old year only, then switch, then archive of
`gs://bucket/<old FY>/` only; old-year approvals CLOSED, new-year approval still runnable; guards G2/G4 baselines carry over, no first-run block; one dashboard card; weekly report counts per year), post-switch audit covers only the new year, G5 blocks a
pre-existing NAS folder without marker, pre-filled cloud prefix gets a G3 first-run block, case-only/hand-made early folders are adopted.
Defect triage (D = from the Opus review):
| ID | Status | Decision |
|---|---|---|
| D3 deferred activation = 1 mail only, no dashboard item / repeat / RESOLVED (silent 3-month miss) | confirmed (xfail test) | **FIX before 1 Jan 2027** - in the Sonnet fixer's batch (see C) |
| D1 stale flow syncs the closed FY into the archived prefix after the April switch | confirmed (xfail T-c5) | **FIX before 1 Apr 2027** - in the fixer's batch |
| D2 `_close_old_fy` never retried after the journal is complete (old approvals stay open; approving does nothing) | confirmed (xfail T-f3: PENDING on 2 Apr after a failed close on 1 Apr) | **FIX before 1 Apr 2027** - sent to the Sonnet fixer (idempotent close at the top of every rollover_check_flow for FYs whose switch is finished) |
| D6 `switch_pending` uses state==DONE vs rollover_journal finished set | confirmed | fixer's batch |
| D9 approving an approval of a non-active FY | DONE (release-break #13, 409) | - |
| D11 mapped drive refused by share-root validator | NOT a defect (PathsConfig already refuses drive letters) | closed |
| D4 early-FY first big upload starves the configured FY (3x CLOUD_MISSED, documented by T-b4) | owner decision | recommend: process the configured FY first while the early FY has no success yet, or reserve budget |
| D5 vanished early NAS folder + marker silently re-created (LAN_COMPLETE) | confirmed (xfail P-3) | owner decision; recommend: create only on the first LAN run after activation, later a missing folder = G5 block |
| D7 final-backup SLOT_TIMEOUT becomes an approvable G6 block | pending test | owner decision (recommend WARNING + retry tomorrow) |
| D8 `close_fy` closes only cloud/lan incidents (audit / overlap incidents stay open) | confirmed | owner decision / low |
| D10 report window of an FY that left the active set without a switch | pending test | owner decision |
| D12 raising `early_start_month` after activation drops the early FY | confirmed | owner decision (recommend: once activated stay active until the switch) |
| D13 unscoped `age 365 -> ARCHIVE` lifecycle rule in `deploy/gcs_lifecycle.json` | known (WP12) | apply `deploy/gcs_lifecycle.fy_overlap_proposed.json` after owner review, before Jan 2028 |
| D14 NEW: `flow.py:3290` stamps each folder's run row with its own pipeline start (Mar 31 night second folder dated Apr 1) | xfail P-11 | low; settle after the timeline results |
The plan (`docs/PLAN_FY_OVERLAP_2026-11-20.md` rev 4) is BINDING: only bug fixes inside its intent were delegated; semantic changes are the owner's.

## C. Code state (branches) - what is where
- `fy-overlap` (main checkout): commits up to `50e7d95` (docs). Untracked tests listed in Part 1 section 1 + the 4 `test_ov_*` files above: commit them once green.
- `release-candidate` (worktree `C:\AAMTest\code`): `aa561bb` (all 12 bug fixes + SILENCE-2) then `48eb4af` (restore tool log timestamp without wmic: removed on Windows 11 24H2+) then `23cdbaf`
  (**deploy/_common.ps1: `-split '\'` is an invalid regex on PowerShell 5.1, it threw and made `04_check_readiness.ps1 -ServiceAccount` skip its service-account probe since commit 9461ac2; now `-split '\\'`, verified `Get-ServiceAccountState` finds the account**).
  This `-split` bug applies to ANY PS 5.1 box (Server 2016/2019, Win10/11): it is a real production-readiness fix.
- Sonnet fixer RUNNING in a worktree off release-candidate (branch `worktree-agent-a73b65ebaa78332e0`): D3, D1, D6, and `core/services.py query()` parsing `sc queryex` by numeric state code (works on non-English Windows). It copies the xfail tests from main and flips them.
  When it reports: review the diffs (`git diff release-candidate <branch>`), merge into release-candidate, rerun, then `Restart-Service` the stack services to pick the code up.
- Merge order at the end: `release-candidate` -> `fy-overlap` (main checkout, only when the audit lab is finished), then run the full unit suite (`.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600`; last known: 3832 passed in the fixer worktree before later merges).

## D. Windows 2016 / 2019 / 10 / 11 compatibility (Haiku research `agent_runs/COMPAT_WINDOWS.md`, ~150 lines; copy not committed - re-derive from this summary)
- Python 3.12 (Win 8.1+), uv and the rclone Go build (Win 10 / Server 2016+) cover all four. All 11 deploy/tool `.ps1` parse with the PowerShell 5.1 parser; no PS7-only syntax; BUT see the `-split '\'` runtime bug above (found by the live install).
- Fixed: `wmic` in `deploy/09_restore_from_gcs.bat`. Queued: localized `sc queryex` parsing. Risk to know: Windows 11 24H2+ **Pro** requires SMB signing / blocks guest access (a guest-only or unsigned NAS fails there; this dev box is Home).
- NOT provable here (needs real machines): actual runs on Server 2016/2019; NSSM OS support; per-wheel OS floors; SMB1 defaults. The per-OS checklist is the last section of COMPAT_WINDOWS.md. Server 2016 is the validated trial baseline.

## E. Other facts to keep
- LAN robocopy exit 4-7 with clean summary = `LAN_COMPLETE` is deliberate (BUG-05). SILENCE-1 does not exist. SILENCE-2 (bad config.yaml = total silence) FIXED. SILENCE-3 (old-schema manifest.db): launch continues by design, mail behaviour to be checked live (LS8b).
- Audit lab: started ~21:20 IST in the background (`agent_runs/lab_release.txt`), was ~90% and showed one `F` at last look; **triage the F when it finishes** (rerun that test alone with a clean PREFECT_HOME). The lab runs from the main checkout's venv; dev packages there were restored with `uv sync --frozen` after the readiness script stripped them.
- Flaky once: `tests/test_property_config_fuzz.py` failed in one full run, not reproducible on 3 seeds.
- Model split (owner): Opus only for design/final verification, Sonnet orchestrates/reviews/fixes, Haiku mechanical work.

## F. TODO (in order)
1. Collect the running agents: Sonnet timeline d/e/f test agent; Sonnet fixer (D3, D1, D6, sc parse); audit lab (triage its F).
2. Add D2 (and D14 if confirmed) to the fixer; review + merge into release-candidate; restart stack services; verify they still run (`/health`, `http://127.0.0.1:4200/api/health`).
3. Unblock LAN on the test stack (A above), run both legs, verify bucket + NAS + mails.jsonl; then the live runbook from `docs/TEST_DESIGN_RELEASE_2026-11-22.md` section C (LS1, LS2, LS3, LS6 overlap Jan-Apr via a patched run date, LS8/8b, LS9, LS10, LS11, LS12, LS13) and a browser check of the dashboard (Origin middleware must not block real use).
4. Merge release-candidate into fy-overlap, full unit suite, commit all untracked tests, update `SESSION_HANDOFF.md`.
5. Final verification by Opus against the release gate (TEST_DESIGN D3); report to the owner: D4/D5/D7/D8/D10/D12/D13 decisions, what could not be verified (Server 2016/2019 runs).
6. Cleanup when finished: uninstall the test services (`deploy\08_uninstall_services.bat`), remove user `aamtest` + share `AAMTestNAS`, delete bucket `aam-backup-qa-bilcvx`, **restore AAMMerger**, remove `C:\AAMTest\code` worktree (`git worktree remove`), stop the SMTP sink.
