# Opus final verification - release gate (2026-11-23)

Verifier: Opus, read-only. No tracked file edited, no tests or services run. Branch `fy-overlap`, HEAD `e6e4210`
(local, 1 docs commit ahead of `origin/fy-overlap` = `a9576db`).
Evidence read: `docs/RELEASE_BREAK_SESSION_2026-11-22*.md` (Parts 1-6), `docs/TEST_DESIGN_RELEASE_2026-11-22.md` (D3),
`docs/PLAN_E2E_SIMULATION_2026-11-23.md` (section 4), `docs/LAB_RUN2_2026-11-23.md`, `docs/AUDIT_MAIL_CONTENTS_2026-11-23.md`
(CRITICAL list), `C:\AAMTest\live\LIVE_RESULTS.md`, `agent_runs/unit_full_after_lab.txt`, `agent_runs/lab2_*.txt`,
`agent_runs/` file times, and spot checks in `core/manual_leg.py`, `core/fy_overlap.py`, `core/guards.py`, `flow.py`, `ui.py`.

## Verdict: GO WITH CONDITIONS

The core data path is well evidenced. Real robocopy and real rclone to real GCS were checked with MD5 on a live
NSSM stack. The kill, power-loss, share-loss, SMTP-down and bad-config faults were run live. Fourteen calendar
simulations cover both boundaries. Nothing I found writes outside the active FY folders, deletes beyond the guards
without approval, or leaves a lock stuck. I found no release-blocking defect in the product code. Three gate items are
still unproven on the code being released. Two findings must be fixed or explicitly accepted before the date they
affect. The conditions are in section 4.

## 1. Gate walk

### 1a. TEST_DESIGN D3 "must be green"

| Item | Result | Evidence |
|---|---|---|
| Full unit suite incl. new files | PASS | `agent_runs/unit_full_after_lab.txt`: 4265 passed, 21 skipped, 1 xfailed. It finished 07:40 on merge `66d653a`. The later commits change only docs plus the `cronsim==2.8` pin in pyproject/uv.lock, and that pin was already installed through Prefect. The 21 skips are environment skips (Part 6). The xfail is finding B (old-schema DB), moot because the install is fresh. |
| Audit lab (full, minus known w16/w7) | NOT EVIDENCED on the release code | The last full run was `agent_runs/lab_release.txt` on 22 Nov 22:40 (249 passed after the w23 re-run, Part 3). Many product changes merged after it: D1/D2/D3/D6, L1-L3 silence fixes, stale-approval supersede, D15, manual-only mode (flow.py, rollover, audit), F1-F8 buttons, trigger lock, LC2a. Only w26/w27 ran since then. |
| LS1 baseline | PASS | LIVE_RESULTS table: bucket, NAS and source MD5 equal; audits VERIFIED |
| LS2 kill agent mid-LAN | PASS (borderline) | ABANDONED, 1 mail, next run COMPLETE. The slot was freed after 323 s against a 300 s criterion (295 s in LS10). Cosmetic. |
| LS3 NAS share lost mid-run | PASS | LAN_PARTIAL, never COMPLETE, mail sent |
| LS4 GCS key revoked | NOT EVIDENCED (live) | Skipped by design (needs gcloud admin). Covered only by unit U14 and sim S4 (401: no retry, FAILED). |
| LS6 overlap Jan-Mar live (patched run date) | NOT EVIDENCED | Never run (Wave 2 "skipped by instruction"). Jan 1 is covered only by unit sims S1/S3/S4/S5/S13 and lab w25/w27a/w27b (real robocopy, local rclone remote). |
| LS10 power loss | PASS | CLOUD_ABANDONED, 1 mail, slot freed in 295 s, next run COMPLETE, audit MD5 clean |
| LS12 secret scan | PASS | 0 hits in 223 runtime files and in mails incl. attachments |
| Mutation table (D1) for every committed Haiku test | PARTIAL / NOT EVIDENCED | Done for the calendar sims: 12 mutations, 5 vacuous tests fixed (Part 5, merge `f86f751`), and for the overlap T-cards (Part 2). I found no record of the D1 table for U1/U2/U8/U10/U12/U14/U16/U17/U20/U23/U24/U25 or for L1/L6. Part 1 TODO 5 was never closed. |

### 1b. D3 "release-blocking" conditions

| Condition | Result | Evidence |
|---|---|---|
| Silent false success (COMPLETE while destination differs) | ONE KNOWN CASE, low likelihood | Lab L2 (`lab2_w26b_names.txt`): the source holds `café.txt` in both NFC and NFD forms. The NAS keeps both. The cloud copy keeps one, and the row is CLOUD_COMPLETE. rclone normalises names when matching, and the product's log scan has no rule for it (grep: no "duplicate"/normalisation handling in core/). All other cases are clean: L1 corruption caught by the audit, L4/L5 CLOUD_FAILED, L7 not COMPLETE per log, LC2a loud. |
| Write outside active FY folders/prefixes | PASS | U16/U17 properties, D1 fix (`_rederive_after_slot`), S11 (A), LC1 frozen FY26-27 trees |
| Deletion beyond G3 without approval | PASS | T-b3 / L6 (w26d 3/3), live B7 |
| Leaked secret | PASS | U12, LS12 |
| Stuck lock/slot after restart | PASS | U1/U2, LS2, LS10, LS7b. The slot frees in about 300 s (fixed lease). |
| Boot failure with zero mail | PASS for config (LS13: 1 mail). ACCEPTED for old-schema DB (LS8b silent; finding B, fresh install, owner) | LIVE_RESULTS LS13, Wave 2 Task 1 |
| Alert storm > 1 mail per key per cooldown | PASS (unit/sim) | U6/U25 properties, S10/S12 bounds. Alert-controls plan steps D-G (follow-up limits) are not in code; owner item. |

### 1c. PLAN_E2E section 4 additions

| Item | Result | Evidence |
|---|---|---|
| S0-S13 (only listed xfails) | PASS | `tests/test_sim_e2e_00..13` are in the green suite. The D14/D7/D15 xfails were flipped to hard tests after the fixes (Parts 4-6). |
| Sim mutation list red | PASS | Part 5, merge `f86f751` |
| L1 corruption | PASS | lab2 w26a 1/1: audit VERIFICATION_FAILED on both legs, 1 mail, red item |
| L2 unicode/case | FAIL (cloud), owner decision | See 1b. The NAS case-only rename keeps the old case with the same content (NTFS, cosmetic). |
| L3 long paths | PASS | `lab2_w26b_names.txt` line 6: CLOUD_COMPLETE, remote equals source, audit VERIFIED |
| L4 / L5 growing / locked file | PASS | lab2 w26c 2/2: CLOUD_FAILED, never COMPLETE, mail sent |
| L6 + T-b3 | PASS | First w26 run: w26d 3/3 (`lab_w26_guards.txt`) |
| L7 ACL-denied | PASS by log, assertion NOT REACHED | The test rig cleanup fails. The product log shows robocopy exit 11, rclone size exit 6, and "completed with 2 error(s)", but the exact cloud status was not captured. |
| LC1 best calendar | PASS | w27a 1/1 (first run) |
| LC2 worst calendar | PASS | lab2 w27b 4/4. LC2a junction is now loud (AAM-ROL-06). |
| LS7a Prefect stopped idle | PASS by analysis | It was not run to the threshold live. `INVESTIGATION_WAVE2_FINDINGS` confirms a restart after about 240 s, and the restart mail AAM-SVC-05 was added after it. |
| LS7b Prefect killed mid-LAN | PASS | One robocopy, LAN_COMPLETE, cmp equal |
| LS2b agent dies, robocopy survives | NOT EVIDENCED | Not run |
| LS5 NAS volume full | NOT EVIDENCED (live) | Skipped (Win 11 Home has no New-VHD; G4 loopback). Unit/fake rigs cover robocopy exit 8/16 only. |
| LS6 | NOT EVIDENCED | See 1a |
| LS8b old schema | FAIL (silent), ACCEPTED | Owner: fresh install, finding B moot |
| Full audit lab | NOT EVIDENCED on release code | See 1a |
| Live buttons (Wave 3, extra) | PASS | B1-B3 and B5-B8 PASS. B4 double-click race is fixed since (trigger lock `3f07c60`) but not re-run live. |

Unverifiable here (accepted per plan): Server 2016/2019 real runs, the live April 1 switch on the real date, real
ARCHIVE transition, real NAS power-off, DST zones, real mail provider, clone-and-run on a new device.

## 2. Residual risks not covered by a test or live run

Ranked by harm x likelihood.

1. **FV-1 (NEW, mine): manual-only LAN + NAS off on 1 April means the closed FY's NAS copy can never be completed, and
   the product says it can.**
   - What happens: `flow.py` `_note_lan_not_final` mails "Start a LAN backup from the dashboard when the NAS is on to
     bring FY26-27 up to date". After the switch, `fy_overlap.active_folders` returns only the new FY, and
     `ui.py:1080` refuses `?fy=` for a non-active FY. So no manual run can ever back up FY26-27 again.
   - Effect: `lan_not_final_fy` stays set for good, the amber note never clears, and the NAS lacks the last weeks of
     FY26-27 work.
   - Test gap: the unit test clears the note only by inserting a fake FY26-27 row (`tests/test_manual_lan_rollover_audit.py:61`).
   - Harm: the second copy of a closed year is incomplete and the mail instruction is false. The cloud copy is intact,
     so this is not data loss while GCS holds it.
   - Likelihood: HIGH for a client in manual-only LAN mode, which the owner expects ("mostly will be failing").
     It does not occur for nightly LAN.
   - Deadline: before 1 Apr 2027.
2. **Contradictory G5/G4 approval text (mail audit CRITICAL #1).**
   - The mail says "Do NOT approve" while the guard detail from `core/guards.py:255-265` says "Approve only if ..." /
     "Approve to initialise". A captured mail offered the server's own disk for approval.
   - An approval leads to `robocopy /MIR` into that location, and /MIR deletes whatever is there that the source
     lacks. This is a human-in-the-loop data-loss path driven by wrong text.
   - Likelihood: low (a block plus a typed APPROVE), but the harm is real.
   - The owner deferred mail work. This one is not only wording: it is the decision text for a destructive action.
3. **Rollover mails with wrong instructions on 1 April (mail CRITICAL #2, #3).**
   - The final-backup-wait WARNING renders as "Unexpected program error AAM-SYS-06 ... forward to support" (code bug in
     `email_compose.effective_code`).
   - `blocked:rollover` and `switched_by_hand` carry ROL-02 steps ("fix the backup first; the switch then runs
     automatically").
   - Harm: the office acts wrongly or ignores a real block on 1 April. Not data loss, because the guards still fail closed.
   - Likelihood: medium (any busy slot or block on 1 April).
   - Deadline: before 1 Apr 2027.
4. **Unicode NFC/NFD twins on the cloud leg (L2): silent CLOUD_COMPLETE with one file missing from GCS.**
   - The weekly audit probably uses the same name matching, so it would not catch it either (not tested).
   - Mitigation: the NAS copy keeps both files.
   - Likelihood: very low. It needs two names that differ only in Unicode form in one folder. Note that Malayalam
     names can have composed/decomposed vowel signs, so this is not zero for this firm.
5. **Jan 1 activation never exercised on a live service stack (LS6).**
   - Untested live: the service account creating `FY27-28` under the real source parent and the NAS share, the new
     prefix in real GCS, and two cards on the real dashboard.
   - The failure modes are loud (D3 deferred activation repeats, LC2a mail), so the residual risk is mostly
     permissions on the production DC / NAS.
   - The test stack is still installed, so LS6 can be run before cleanup.
6. **Production host differences (Windows compat review).**
   - Production is a domain controller (GPO may deny the service/batch logon rights or SeBackupPrivilege).
   - The code has no NAS credential handling: the trial needed a manual `net use`, which does not carry into the
     service session.
   - rclone 1.76 has not run against GCS on Server 2016.
   - These are loud failures (LAN_FAILED/CLOUD_FAILED plus mail), not silent ones. Install-time checks per
     `REVIEW_WINDOWS_COMPAT` section 7.
7. **Total silence when watchdog and agent are both down, or the server is off.**
   - Nothing outside the box alerts. Missed nights write no row.
   - This is in the accepted "server down" risk (2026-10-19). Restating it for the owner report.
8. **Code merged after the last full audit lab.**
   - Lab-level real-tool behaviour of the manual-only mode, F1-F8, the stale-approval supersede and D15 is evidenced
     only by unit tests and the Wave 3 live buttons.
   - Medium likelihood of a lab regression, low of data harm (the guards are unchanged).
9. **D15 caveat.** "Leg approval given" means any approved request of that leg+FY, so a January approval makes a
   March block go straight to G6. This only adds noise; it still fails closed.
10. **D4.** A large early-FY upload can starve the configured FY (CLOUD_MISSED, loud). Low likelihood in January,
    because the new folder starts nearly empty.

Not a risk: the "database is locked" lines in the lab outputs are from the Prefect temporary server's own DB
(TELEMETRY_SESSION insert, `lab2_w26c_upload.txt:127`), not the manifest.

## 3. Open findings: classification

**Must fix (or explicit written owner acceptance) before the date they affect, not before cutting the branch:**
- FV-1 manual-only LAN, closed FY never final-synced plus a false instruction: before 1 Apr 2027. Options: allow a
  one-off LAN run of the `lan_final_missing` FY after the switch, or change the text and accept.
- Mail CRITICAL #1 (G5/G4 approve vs do-not-approve): before the first customer go-live. It is a small text change in `core/guards.py`.
- Mail CRITICAL #2 and #3 (rollover steps, final_wait rendered as AAM-SYS-06): before 1 Apr 2027.
- Mail CRITICAL #4 (invalid-config mail echoes raw validator text): before go-live (truthfulness, possible value echo).
- `deploy/08_uninstall_services.bat` prints "COMPLETE" after a failed `nssm remove`: before it is used at a customer.
  Check with `sc query` meanwhile.

**Can ship as known issues (list them in the owner report):**
- L2 Unicode twins on cloud (low; the NAS keeps both).
- Finding B / LS8b old-schema DB silent (fresh install).
- D4, D13 (GCS lifecycle JSON before Jan 2028), alert-controls D-G, restore-engine default `forbidden_paths` (documented).
- Watchdog-down gap, server-down silence (accepted).
- Row started_at includes the slot queue wait (B4/wave 2).
- Cloud-only config still needs a UNC `lan_destination` and `wol.enabled: false`.
- Audit alerts when `audit_lan` returns AUDIT_INCOMPLETE for an unreachable share with WoL off (manual mode).
- `nas_left_on` not added for nightly legs.
- Rollover `nas_folder` step retried each morning on an unreachable NAS.
- A queued LAN run can end LAN_SLOT_TIMEOUT after a long manual cloud run.
- Late LAN_MISSED dated by its actual start.
- English-only robocopy/icacls label matching (non-English OS).

**Cosmetic:**
- The remaining HIGH/MEDIUM/LOW mail wording (audit doc), the April-1 "New financial year started" wording and the
  reboot ACTION NEEDED wording.
- `/status` still shows the cron in manual mode.
- The LS3 subject not naming the share.
- The slot freed at 323 s against 300 s.
- `test_wol_comprehensive::test_already_online` has no assertion (test hygiene).
- ARCHITECTURE.md:166 stale wording.
- The L7 lab rig cleanup and the leftover ACL deny on the lab temp folder `case_capbcc68` (test-only).

## 4. Conditions

### C. To cut `production-release` (all required)
1. **Full audit lab once on the release candidate** (`--ignore=audit_lab/test_w7_deployed.py`, clean PREFECT_HOME,
   nothing live running, stack watchdog stopped). Only the known w16 and the classified w26b-L2 / w26e-L7 may be red.
   This matches the owner rule ("only occasionally if needed"): it has not run since all of today's product merges.
   If the owner waives it, record the waiver in Part 6.
2. **Owner accepts in writing** (one line each in Part 6): L2 Unicode twins as a known issue, LS4/LS5/LS2b not run
   live, the missing D1 mutation table for the U/L cards (or a Sonnet runs it: about 14 working-tree mutations, never
   committed).
3. **Push the local docs commit** (`git push origin fy-overlap`, fast-forward), then create the branch from the pushed
   tip: `git fetch; git checkout -b production-release origin/main; git merge --no-ff origin/fy-overlap`.
   Then check that `git diff origin/fy-overlap production-release --stat` is empty (no content drift).
   Then `git push origin production-release`. No force; do not touch `origin/production`.
4. Update the test stack to that tip and re-run the Wave 3 smoke (B1 cloud, B2 LAN, B4 double click now refused),
   because the trigger lock and LC2a code changed after Wave 3 (stack at `93036fd`).

### G. Before go-live at the customer / before 1 Jan 2027
5. Run **LS6** (overlap with a patched run date) on the test stack **before the cleanup step destroys it**.
6. Fix mail CRITICAL #1 and #4 (G5/G4 approval text; invalid-config mail).
7. Install-time checks from `docs/REVIEW_WINDOWS_COMPAT_2026-11-23.md` section 7 on the real host: the DC logon
   rights and SeBackupPrivilege, NAS credentials for the service account, rclone 1.76 against GCS, Edge/Chrome for the
   dashboard.

### A. Before 1 Apr 2027
8. Fix FV-1, mail CRITICAL #2 and #3, and run a patched-date April-1 rehearsal (in-process, real tools) with the then-current code.

## 5. Owner summary (one paragraph)
The product passes every release-blocking check that could be run here. Full unit suite: 4265 passed. Live faults on
real GCS: all PASS. Calendar simulations of both boundaries: PASS. The lab data-integrity cards PASS, except one
low-likelihood cloud name-merge case. Before cutting the branch: run the full audit lab once, record the owner
acceptances and push. Before go-live, run the live overlap step (LS6) while the test stack still exists and fix two
misleading mails, one of which gates a destructive approval. Before 1 April, fix the manual-only LAN year-end gap (FV-1).
