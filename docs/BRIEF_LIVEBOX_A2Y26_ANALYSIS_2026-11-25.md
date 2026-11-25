# Brief: analyse the finished 728-day live-box run (lane A2, `a2y26`)

Date: 2026-11-25. For: an outsourced agent. Mode: **READ-ONLY investigation and report**. Nothing is fixed in this task.

## 1. Context

- Product: nightly backup automation for an accounting firm on Windows Server 2016 (rclone to GCS, LAN mirror to a NAS, FY rollover, weekly integrity audit, mail reports, dashboard). Orchestrated by Prefect 3 as three NSSM services: `AamPrefectServer`, `AamBackupAgent`, `AamWatchdog`.
- The trial box runs branch **`baseline-v1-overlap`** (box code `C:\AAMBackup` at `dc50265`; runtime `C:\BackupAgent`). It is reached with `ssh innoviztawinserver2016`.
- A live-box test harness (code `tools/livebox`, copy on the box at `C:\AAMLivebox\code_fix`, commit `0ec61f2`) simulated 728 days (2026-10-01 to 2028-09-27) as lane A2, run name `a2y26`. It finished at 07:59 IST on 2026-11-25, exit code 3, not stuck.
- This is NOT the `rclone-engine` branch work (Phase 1 rclone LAN). The failures below are on the baseline product. Do not mix the two.

## 2. Rules (binding, from the owner)

- Never change product code or the live schedule. No reboot, no shutdown, never power off the NAS, no restarts or kills of services or processes, no file edits or deletes on the box, no config changes. Copy evidence files out to read them if needed.
- No product fixes are committed without the owner's go. Your output is a report, with a proposed fix described but not applied.
- Do not print secrets (mail passwords, tokens).
- Remote PowerShell over SSH: use `;` not `&&`, set `[Console]::OutputEncoding=[Text.Encoding]::UTF8`; there is no scp, so pipe files over ssh.
- Never write Malayalam or personal data into reports; this is a backup system, quote only paths and status strings.

## 3. Where to look

Read these first, in the baseline worktree `C:\Users\malliath\Desktop\aam_baseline_overlap`:
- `docs/LIVEBOX_HANDOFF_2026-11-25.md`
- `docs/LIVEBOX_TEST_PROGRESS_2026-11-25_ADDENDUM.md`
- the STATE block of `docs/LIVEBOX_TEST_PROGRESS_2026-11-24.md`
- `docs/LIVEBOX_TEST_PLAN_2026-11-24.md`
- `docs/PLAN_BASELINE_OVERLAP_2026-11-23.md` and the `TESTMATRIX_*` docs (how the overlap and audits were meant to behave)
- `docs/ARCHITECTURE.md` (flow, gates, section 10) and `docs/EMAIL_CONTENT_SPEC.md` (mail keys, `AAM-AUD-01`)

Evidence on the box (read-only):
- `C:\AAMLivebox\a2y26\A\evidence\result.json`, `checks.jsonl` (16,715 checks), `progress.json`
- `C:\AAMLivebox\a2y26_out.txt`, `a2y26_err.txt` (3 ERROR lines, 0 "Stale backup"), `a2y26_exit.txt`
- per-day run folders, logs and the harness DB under `C:\AAMLivebox\a2y26\A\` (explore; names not yet mapped)

Product code to read for the root cause (baseline worktree): integrity audit (`core/integrity*.py`), `flow.py` integrity-audit flow and LAN/cloud FY pass handling, `core/reporter`/`notify` for the audit mail, rollover and overlap code (`rollover.overlap_activate_on`).

## 4. Results to explain

Totals: 16,715 checks: PASS 16,525, KNOWN 182, FINDING 3, FAIL 5.

### 4.1 The 5 FAILs (all on 2027-01-03, first working day after the 1 Jan overlap activation)
1. `FLOW_STATE`: integrity audit raised an `ExceptionGroup`, "did not verify (2 sub-exceptions)".
2. `AUDIT` x2: LAN audit status `VERIFICATION_FAILED` (expected VERIFIED or PENDING_BACKUP).
3. `MAIL`: 1 problem mail, expected 0.
4. `MAIL M2`: an ACTION_REQUIRED mail on a healthy day, subject "ACTION NEEDED: Weekly safety check - Safety check found differences (AAM-AUD-01)".

Hypothesis to confirm or reject: one root cause, the weekly audit comparing source and NAS while the overlap (next FY next to the current one) just activated. Candidates: audit checks the wrong FY folder or both FYs while the new FY destination is still empty or not yet backed up; the two sub-exceptions are one per FY pass; the harness simulated NAS state is inconsistent on that day; or a real product bug. Decide which, with evidence.

### 4.2 The 3 FINDINGs (unclassified)
- 2026-12-31: NAS wake 2x for 2 passes (`SPY_WAKE_PER_PASS`; known as P6 in the handoff).
- 2027-04-01 and 2028-04-01: `rollover_check` wake 2, shutdown 0 (`SPY_ROLLOVER_NAS`).
Classify each as: expected behaviour (spec says so), harness artefact, or product defect.

### 4.3 The 182 KNOWN
Skim the grouping only. Confirm none hides a pattern that matches the FAILs.

## 5. What to gather

1. The exact exception text and traceback of the two sub-exceptions from the 2027-01-03 audit (flow run log / run artefacts for that day), and what each audit leg compared.
2. The audit's source and destination paths per leg that day, FY folders present on source and NAS, and the `run_history` / audit rows (status, counts, differences list).
3. Whether the same audit passed on other weeks and on later weeks (2027-01-10 and after): is it a one-day event or recurring? Check every `AUDIT`, `MAIL` check around 2026-12-28 to 2027-01-17.
4. The mail that was sent (subject, key `AAM-AUD-01`, body facts) and whether it was correct given the data (a true difference) or false.
5. For the 3 FINDINGs: the spec lines that define expected NAS wake and shutdown counts at the 1 April rollover and 31 Dec activation, and the actual counts from the harness spies.
6. Whether the box stays unchanged: `git -C C:\AAMBackup log -1`, config untouched, services still Running (last seen: all three Running, C: 1.44 TB free).
7. NAS reachability was not probed. Do not probe the real NAS with writes; a TCP 445 connect test to 10.10.151.53 is allowed.

## 6. What to infer and deliver

Write `docs/REPORT_LIVEBOX_A2Y26_2026-11-25.md` (new file, in the worktree you are given; do not commit). Sections:
1. Verdict per item (4.1 group, each 4.2 finding): root cause, confidence (confirmed / likely / guess), and the evidence line or file for it.
2. For each defect: is it in product code or in the harness? Name file and function. Does it also exist in the `rclone-engine` branch (check `git diff baseline-v1-overlap..rclone-engine -- <file>`)?
3. Proposed fix (described, with the test row that would catch it). Not applied.
4. Severity for production: would a real operator get a false "ACTION NEEDED" mail after the 1 January overlap activation? Would a real difference be missed?
5. What remains unknown and what extra data would settle it.
6. Handoff steps still pending for this lane (from the handoff doc): evidence copy to `evidence_keep_a2y26`, production-unchanged check, cleanup of the `a2y26` folder. Do not do them; list them.

Be factual: no guesses presented as facts; say "not verified" where you did not read it.
