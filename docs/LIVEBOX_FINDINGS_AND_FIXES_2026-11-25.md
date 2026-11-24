# Live-box test findings and possible fixes (as of 2026-11-25, lane A a728 up to 2092-11-06)

Status: proposals only. No product code changed. Owner decides. Product = baseline-v1-overlap @ dc50265.
Harness-only issues are listed at the end (they are not product bugs).

## Product findings

| ID | Problem | Possible fix | Severity |
|---|---|---|---|
| R1 (new) | One folder rename (Client_000/Invoices, 316 files) plans delete+copy; AAM-DEL-01 blocks cloud and LAN on every run until a human approves. Lane A stayed blocked 2091-04-04 to the end, 1022 cloud and 1020 LAN blocked runs, stale alerts, stuck rollover. | Rename/move detection (match by size+mtime, or rclone/robocopy move handling) so renames are not counted as deletes; or let one approval cover the same plan fingerprint for N days; show the folder names in the mail. | High |
| P1 | Escalation mail suppressed by the 24 h cooldown (440 SUPPRESSED notifications). Owner is not reminded as a problem lasts. | Key the cooldown on (alert, escalation level); always send level changes (day 1, 3, 7). | High |
| P2 | No internet is classified as a login/key error. | Connectivity probe (DNS/TCP to storage.googleapis.com) before classifying; new NETWORK_DOWN class with retry and its own wording. | High |
| P3 | A tiny new-year folder (1 file) trips AAM-DEL-01. | Skip or relax the guard for a leg with no prior successful run; apply the percentage to the leg's own baseline. | Medium |
| P4 | FY switch completes although the new-year cloud copy was blocked. | Make the switch precondition: new FY cloud leg has a success. | High |
| P5 | Blocked rollover leaves the NAS on (~15 h). Rollover retried 220 times (ROLLOVER_BLOCKED daily, config_switch pending). | Release/shutdown the NAS in a finally; cap attempts and send one escalating mail. | Medium |
| P6 | NAS woken once per FY pass (flow.py:1129). | Wake once per run, not per pass. | Low |
| P7 | Two near-identical PROBLEM mails 31 s apart. | Dedupe per run. | Low |
| P8 | Activation-blocked WARNING mailed as "ACTION NEEDED". | Map severity correctly. | Low |
| F-E1 | The 2090-12-31 Sunday audit fails verification (the next week's passes). | Investigate audit versus activation day; retry the verification once. | Medium |
| F-OWN-1/2/3/5 | See LIVEBOX_TEST_PROGRESS_2026-11-24.md STATE block. | Owner review. | - |
| R2 (new) | Two identical ACTION NEEDED mails for the two FY legs (437 pairs, same subject, same second). Subject has no FY tag. | Put the FY in the subject, or merge into one mail listing both legs. | Medium |
| R3 (new) | Mail volume: 1906 ACTION NEEDED of 2034 mails, 0 PROBLEM, 0 RESOLVED. Block mails repeat daily. | Digest plus escalating reminders (pairs with P1). | Medium |
| R4 (new) | run_history lost early rows (74 run_ids, 2090-12 to 2091-03); lane config had db_retention_days 90 (product default 730). | Check purge uses the real clock vs sim clock; confirm 730 is applied; re-test with 730. | Medium (needs check) |
| R5 (new) | After rollover the old-FY leg is recorded with no FY suffix; the new leg has FY92-93. Dashboards keyed on FY may misattribute. | Always tag run_id/leg with its FY. | Low-medium |
| R6 (new, inferred) | Watchdog logs 4 stale lines per leg from 2092-04 (one per FY leg), 2 before. | Verify in the full watchdog log; merge into one alert per leg. | Low |
| R7 | Integrity audit 2091-07-29: missing=118, extra=317, a consequence of R1. | No separate fix. | - |

Stale-backup alerts were true positives (last good runs 2091-04-03; blocked from 04-04); not a watchdog bug.

## Harness / test gaps (not product bugs)
- Lane A never approves blocks (needs an owner-approver step) so R1 hid the rest of year two.
- events.jsonl `ok` was always true on the old harness; fixed (flow_state, exc_type) for the next run.
- Not exercised: cron/serve catch-up, launch.py boot, watchdog loop, real WoL/smb_ready, reboot recovery, real GCS and outage classification. X17/X39 need a real-process lane.
- Open owner question: should an audit shut the NAS down when lan.shutdown_after_backup is off? (flow.py:3052 docstring vs code.)
