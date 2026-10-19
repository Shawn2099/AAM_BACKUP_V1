# Plan: the safety check tells "not backed up yet" apart from "wrong" (2026-10-19)

Status: **approved by the owner 2026-10-19; in progress (see section 11).** Owner decisions of 2026-10-19 are in section 7.
Follows the project rules: fail closed, every tunable is a config key, reuse the shared building
blocks, tests first, one commit per step.

## 1. Problem (seen on the trial box, 2026-10-19 15:01)

The safety check (`core/integrity.py`, run by `flow.integrity_audit_flow`) compares the LIVE source
with each backup copy. Its measurements are correct (hash-based, fails closed, verified against
the files that were seeded). But it cannot tell:

* a file the next backup has not copied yet (new, edited, moved, deleted since the last backup)
* a copy that is really wrong (corrupted, missing, changed on the NAS)

so ordinary work is reported as `VERIFICATION_FAILED`: a red banner that stays until the next
check, and an "ACTION NEEDED ... Weekly safety check found differences (AAM-AUD-01)" email, even
when the owner only pressed "Check now". Real alarms get lost among these. Today also shows that
the false alarms double as an accidental mass-change tripwire; the design below keeps that.

## 2. Principle

The check answers one question: **"Is every file that was already backed up still correct on the
copy?"** Files changed after the last successful backup of that leg are *waiting for the next
backup*; they are reported, never alarmed, never counted as verified.

Architecture (no patching of the existing check):

```
rclone check (unchanged, the measurement)  ->  raw diff: + missing, * changed, - extra, = equal
        |
core/audit_pending.py (NEW, pure, read-only)  ->  partition each difference: PENDING | REAL
        |                                          using: reference time + stat() of the source
flow.integrity_audit_flow  ->  decides the verdict:  REAL > 0            -> VERIFICATION_FAILED
                                                      REAL = 0, PENDING>0 -> PENDING_BACKUP
                                                      both 0              -> VERIFIED
core/statuses.py  ->  the only definition of what each verdict means / how it is shown
```

`_run_rclone_check` keeps its fail-closed order: transport error, process error, missing summary,
read error, size-only and contradictory evidence are decided BEFORE and are never classified.
Only the "completed with differences" branch hands its raw lists to the classifier.

## 3. The classification rule (single place: `core/audit_pending.py`)

Inputs: raw `added`, `modified`, `removed` path lists; the source root; `reference` = `started_at`
of `db.last_successful_run(leg)` (the shared success definition in `core/statuses`); `now`;
`audit.modify_window_seconds` (existing).

A difference is **PENDING** only when the source proves a change after `reference`:

| Raw result | PENDING when | Otherwise (REAL) |
|---|---|---|
| `+` missing from the copy | `max(mtime, birthtime, parent-folder mtime)` is after `reference` and not later than `now` + window | file older than the last backup yet absent from the copy |
| `*` content differs | file `mtime` after `reference` (and not in the future). LAN extra: if the NAS copy has the SAME mtime as the source, it is REAL (same version, different bytes = corruption) | file unchanged since the last backup but the copy differs |
| `-` only in the copy (deleted/renamed away) | nearest existing source parent folder has `mtime` after `reference` | folder untouched since the last backup |

Why the parent-folder time: a moved/renamed file keeps its own old times, but moving, deleting or
creating an entry always updates the containing folder's time. This makes renames and deletes
(normal accounting-office work) PENDING without trusting file times alone.

Always REAL: no `reference` (the leg never succeeded), a future time, any `stat()` failure,
a path that cannot be resolved. Unknown = red (fail closed).

**Mass-change tripwire (deleted files only):** if the number of PENDING `-` differences exceeds the
EXISTING `guards.max_delete_files` / `guards.max_delete_percent` (percent of the copy's file count),
the whole result stays REAL with the detail "unusually many files deleted since the last backup".
Changed or added files have no guard limit and get none: in-place encryption by ransomware is an
accepted risk (owner, 2026-10-19), and a new key would be a new tunable for no accepted threat.
No new setting.

Bounded delay: a file skipped silently by a backup is PENDING only until the next successful
backup starts; then it is REAL. So a hidden miss lasts at most one backup cycle.

## 4. Status, display and alerting

* New verdict `PENDING_BACKUP` in `core/statuses.AuditStatus` (label, description, category).
  It is NOT a success verdict: it never reads as "all files match".
* Persisted in `integrity_audits` (`manifest.record_audit` allow-list gains the value;
  `mismatches` = REAL count only; `detail` carries "N files waiting for the next backup",
  the reference time, and the REAL paths as today). No new table or column.
* Flow: `PENDING_BACKUP` is not an `excs` entry, so no AAM-AUD email and the flow ends COMPLETED.
  A failed persist is still loud (unchanged).
* Dashboard (`core/attention.py`, both renderers in `static/js/dashboard.js`): a small AMBER
  note, action first: "Nothing to do: N newer files will be copied by the next backup" (wording
  through `core/messages.act`, checked by `test_messages_style`). Not red, not in `/health` red.
* Reports (`core/report.py` `_audit_row` + summary line): "every backed-up file matches; N newer
  files wait for the next backup".
* A real `VERIFICATION_FAILED` keeps the red banner and email; its first sentence becomes
  "Press Check now to re-check once the cause is fixed" (it stays red until a later check passes).

## 5. Manual "Check now" (no backup inside it)

`trigger_verify` stays read-only (the flow's rule: it NEVER modifies). New pre-step in `ui.py`:
compare "now" with `last_successful_run(leg).started_at`; when changes may be waiting, answer
`409 {"status": "backup_first"}`; the page shows, action first:
"Back up now first, then check again. The last <leg> backup was at <time>." with two buttons:
**Back up now** (existing run-now) and **Check anyway** (the pending rules make it safe).
Shows only the time of the last backup; no file scan (the classifier does the real work, and a
scan can miss deletes and preserved mtimes). No new setting beyond an age threshold reused from
`dashboard.stale_after_hours`-style config if needed (decided in step 3).

## 6. Weekly "backup first" chain: DEFERRED (owner chose order: build last)

Built only after steps 1-4 have run for at least one real week and the real audit/backup
durations are known. Design when built (so it is not redesigned later):
chained backup per leg runs through the normal pipeline OUTSIDE the audit's slot (slot is
not re-entrant), `run_id = <audit_id>-pre-<leg>`, a per-leg slot for each check, one
`RunBudget(audit_max_hours)` for the whole flow with the chained backup capped by
`audit.backup_first_max_hours` (default 2, >= the pipeline reserve), scheduled runs only
(`auto_scheduled`), `audit.backup_first` default OFF until proven, a failed backup still lets the
check run and the flow end FAILED without a second AUD email, and the NAS shutdown is attempted
on EVERY path after a wake (today `BudgetExceeded` and "slot not obtained" skip it).
Keys and the schedule rule (`core/budget.py`) are re-checked at that time.

## 7. Decisions (owner, 2026-10-19)

1. "Check now" never runs a backup by itself: inform, then offer. YES.
2. Deleted/renamed files are PENDING (folder-time rule), bounded by the mass-change limits. YES.
3. PENDING_BACKUP shows a small amber note, no email. YES.
4. Manual pre-check shows the last-backup time only. YES.
5. Chain: last, after a real week of data. YES (see section 6).

## 8. Files

`core/audit_pending.py` (new), `core/integrity.py`, `core/statuses.py`, `core/manifest.py`
(allow-list only), `core/attention.py`, `core/report.py`, `core/messages.py` (if wording helpers
are needed), `flow.py` (verdict + persist), `ui.py` (pre-check), `static/js/dashboard.js`,
`docs/ARCHITECTURE.md` (section 10 gates), `SESSION_HANDOFF.md`. No new dependency, no new table.
New config keys: none in steps 1-4 (reuses `audit.modify_window_seconds` and the guard limits).

## 9. Steps (tests first, one commit each; `tests/test_audit_pending.py` is the main new file)

1. **Verdict plumbing.** Tests: `record_audit` accepts `PENDING_BACKUP`; flow does not email or
   raise for it; attention gives amber not red; report/dashboard text; status never reads as
   verified. Then the code.
2. **Classifier.** Tests with real temp files and `os.utime`: new file, edited file, moved file
   (old times, folder touched), deleted file, unchanged-but-differs (REAL), same-mtime-different-
   bytes on LAN (REAL), future time (REAL), stat failure (REAL), no reference (REAL), mass count
   over the limit (REAL), mixed result (REAL wins), fail-closed branches untouched (transport,
   read error, size-only, contradictory). Then `core/audit_pending.py` and the one call site in
   `_run_rclone_check`.
3. **Manual pre-check.** Tests: 409 `backup_first` when stale, passes when recent, text passes
   `looks_action_first`; page shows both buttons. Then code.
4. **Deploy and prove on the box:** the 30 new + 16 changed seed files must give
   `PENDING_BACKUP`, no email, amber note; then delete one seeded file and corrupt one NAS
   copy's bytes (same mtime) on a COPY test folder with the owner informed: expect REAL/red.
   Update ARCHITECTURE.md gates, handoff, tick this plan.
5. (Later) Weekly chain per section 6.

## 10. Risks and limits (accepted, stated honestly)

* A copy that is corrupted and then edited at the source is PENDING until the next backup
  overwrites it (nothing is lost; the next backup replaces the copy).
* Ransomware that rewrites files in place is not caught (accepted risk, owner 2026-10-19); mass
  deletes are still caught by the delete limits, same as G3.
* A copy-overwrite that keeps an old creation time can stay REAL (false alarm on the safe side).
* The classifier trusts source times from the server clock; a wrong clock is already a red
  clock check on the cloud leg.

## 11. Task checklist (tick when committed)

Step 1 - verdict plumbing
- [x] T1.1 Tests `tests/test_audit_pending_verdict.py` (verdict stored, no email, amber, report, both renderers)
- [x] T1.2 `AuditStatus.PENDING_BACKUP`, `record_audit` allow-list, flow exemption, attention amber, report row, dashboard.js texts

Step 2 - classifier
- [x] T2.1 Read the delete limits in `models/config.py` (`guards.max_delete_files/percent`); fix the exact tripwire rule
- [x] T2.2 Tests `tests/test_audit_pending.py` with real temp files and `os.utime` (new, edited, moved, deleted, unchanged-differs, same-mtime LAN, future time, stat failure, no reference, delete tripwire, mixed)
- [x] T2.3 `core/audit_pending.py` (pure classifier)
- [x] T2.4 Call site in `_run_rclone_check` / audit flow: reference time from `last_successful_run`; verdict PENDING_BACKUP; fail-closed branches untouched
- [x] T2.5 Failed-check text: "Press Check now to re-check once the cause is fixed"

Step 3 - manual Check now
- [x] T3.1 Tests: 409 `backup_first` when the last backup is older than the stale limit, passes when recent, text action-first
- [x] T3.2 `ui.py` `trigger_verify` pre-check and the page's two buttons (OK = check anyway, Cancel = back up first; new key `audit.backup_first_prompt_minutes`, default 60)

Step 4 - prove on the box
- [ ] T4.1 Full suite + audit lab green; deploy to the box
- [ ] T4.2 30 new + 16 changed seed files give PENDING_BACKUP, no email, amber note (owner informed before touching data)
- [ ] T4.3 Real-corruption case stays red (on a COPY test folder, owner informed first)
- [ ] T4.4 Update `docs/ARCHITECTURE.md` section 10, `SESSION_HANDOFF.md`

Step 5 - weekly chain (section 6): built later, after one real week of data. Not part of this checklist.
