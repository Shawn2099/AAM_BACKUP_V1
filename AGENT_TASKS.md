# AGENT_TASKS.md - work board for worker agents

You are a **worker agent**: you run checks and investigate, write full logs and a
report, and stop. The **lead session** (the one building with the owner) owns the
code, this board, commits and every change to the trial box.

Read once before your first task: `CLAUDE.md` (commands, gotchas), and section 4 of
`SESSION_HANDOFF.md` (current position). Do not read the historical `*_REPORT*.md`.

---

## 1. Rules (workers)

1. **Do not edit tracked files.** No code, test, config or doc changes, not this board
   either, and no `git add/commit/push/pull/checkout/stash`. Your only writes go under
   `agent_runs/` (gitignored). If a fix looks obvious, describe it in your report
   (file:line + proposed diff); the lead applies it test-first.
2. **Trial box (`ssh innoviztawinserver2016`) = READ-ONLY for workers**, unless the task
   says otherwise in writing. Allowed: `Get-*`, `sc query`, `git log/status`, reading
   logs, `agent_ops/box_status.py` (opens the DB read-only). Forbidden: starting/stopping
   services or flows, `git pull`, editing files, touching `E:\` or the NAS share, Prefect
   `deployment run`.
3. **Never use the real `config.yaml`** in tests (the conftest sandbox does this for you).
4. **Full logs, always**: redirect the WHOLE output, never `| tail` into the log:
   `<command> > agent_runs/logs/<TASK-ID>_<YYYYMMDD-HHMM>.log 2>&1`
5. **One task at a time per agent. Only one `audit_lab` task may run at a time** (all of
   them share Prefect and robocopy/rclone). The lead may be editing code: if a test fails
   in a file changed in the last hour (`git log --since="1 hour ago" --stat`), say so in
   the report rather than calling it a regression.
6. Stop and report (status `BLOCKED`) instead of guessing when something is unclear or
   would need a change. Reliability over speed (owner rule).

## 2. Protocol

1. **Pick** the first task in section 4 with state `OPEN` whose `needs` are met.
2. **Claim** it atomically. `mkdir` fails if another agent already has it, and then you pick another:
   `mkdir agent_runs/claims/<TASK-ID>` and write your name + start time into
   `agent_runs/claims/<TASK-ID>/owner.txt`.
3. **Run** exactly the commands given (Windows, from the repo root
   `C:\Users\malliath\Desktop\aam_backup`; venv python `.venv\Scripts\python.exe`).
   Long runs: run them in the background and wait. Do not poll faster than every few minutes.
4. **Report**: write `agent_runs/reports/<TASK-ID>.md` with the template below, then
   create `agent_runs/claims/<TASK-ID>/DONE`. The lead reads the reports and updates
   this board.

### Report template

```markdown
# <TASK-ID> - <title>
- Agent: <name>   Started: <ISO time>   Finished: <ISO time>
- Repo HEAD: <git log --oneline -1>     Box HEAD (if used): <...>
- Status: PASS | FAIL | FINDINGS | BLOCKED
- Full log(s): agent_runs/logs/<file>.log   (size, line count)

## Result
<counts, e.g. "1886 passed / 0 failed / 19 skipped in 163 s">

## Failures / findings (one block each)
- What: <test id or observation>
- Evidence: <exact error lines, quoted from the log, with the log line number>
- Likely cause: <only if verified; otherwise "unknown">
- Proposed fix: <file:line + idea or diff; do NOT apply>

## Anything odd (even if passing)
<warnings, slow tests, flaky reruns>
```

## 3. Layout

| Path | What |
|---|---|
| `AGENT_TASKS.md` | this board (lead edits only) |
| `agent_runs/claims/<TASK-ID>/` | claim (`owner.txt`, `DONE`) |
| `agent_runs/logs/` | full raw logs |
| `agent_runs/reports/<TASK-ID>.md` | your report |
| `agent_ops/box_status.py` | read-only box status (code version, services, runs, alerts, approvals, audits) |

---

## 4. Task board (lead updates states: OPEN -> CLAIMED -> DONE / NEEDS-LEAD)

### T01 - Full unit suite at HEAD - OPEN
needs: nothing
```
.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600 > agent_runs/logs/T01_<stamp>.log 2>&1
```
Expect about 1886 passed / 0 failed / 19 skipped, around 3 min. Report the counts and every failure.

### T02 - Full audit lab (plan M5 5.1) - OPEN
needs: no other audit_lab task running
```
.venv\Scripts\python.exe -m pytest audit_lab --ignore=audit_lab/test_w7_deployed.py -q -s --timeout=1800 -p no:cacheprovider -o addopts="" > agent_runs/logs/T02_<stamp>.log 2>&1
```
Real robocopy/rclone against a sandbox, about 20 to 30 min. Last full run 238/0 before today's
changes. Report the counts, and for each failure the test id and its assertion text. Also list
the 10 slowest tests (`--durations=10` may be added).

### T03 - Box morning check (read-only) - OPEN, repeat daily after 08:30 IST
needs: nothing
```
cat agent_ops/box_status.py | ssh innoviztawinserver2016 "C:\AAMBackup\.venv\Scripts\python.exe -" > agent_runs/logs/T03_<stamp>.log 2>&1
```
For each run since the previous T03 report (LAN 21:00, cloud 02:00): status, files/bytes
copied, duration, error_message. Check that **every non-success run has exactly one
FAILURE/ACTION_REQUIRED notification**, and that every success after a failure has a RECOVERED one.
Flag anything that is "Running" for more than 6 h, any notification `status` FAILED, and
`backup.lock` PRESENT outside 21:00-08:00. This is plan 5.3 (seven truthful nights): report
the night number (night 1 = 2026-10-16 21:00).

### T04 - Investigate: cloud integrity audit "20 files missing in GCS" (read-only) - OPEN
needs: nothing
Box audits on 2026-10-04 and 2026-10-11 (OLD install, before the 2026-10-15 fresh install)
reported `cloud VERIFICATION_FAILED missing-from-dest=20`, while every cloud backup says
`CLOUD_NO_CHANGES_COMPLETE`. Find out, **without running any rclone write or any flow**, why:
- Read `core/integrity.py` (audit_cloud) and `flow.py` `_effective_fy_prefix` / the cloud
  pipeline: which local path and which bucket prefix does each compare?
- On the box, read-only: `C:\AAMBackup\config.yaml` paths/cloud section (do NOT copy secrets
  into the report), the old install `C:\AAMBackup_v113\config.yaml` for comparison, and
  `rclone lsf` of the bucket prefix ONLY if a read-only rclone config is already available
  on the box (`rclone lsf` / `rclone size` are read-only; no `sync`/`copy`/`touch`/`delete`).
- Question to answer: is it (a) an old-install config difference that the new code no
  longer has, (b) a real mismatch between what the backup uploads and what the audit
  expects (prefix / path / filter), or (c) unknown? Give the evidence.
The next scheduled audit (Sunday 10:00) runs on the new code; say what result you predict.

### T05 - Code read: what a wrong clock can still affect (read-only) - OPEN
needs: nothing
The lead fixed: a FY rollover now refuses an unproven clock (commit acde356). Check what
ELSE uses the wall clock for a decision that could lose or hide data if the clock jumps a
long way forward or back: `core/run_artefacts.purge_older_than`, `ManifestDB` purge /
retention, `core.attention` stale check, `core.notify` cooldown, the budget code, the
dashboard. For each: file:line, what happens with clock +1 year and -1 year, and whether it
can delete backups/logs or silence an alert. Findings only, no fixes.

### T10+ - reserved for the lead
On-box scenario tests (plan 5.2: mass delete, NAS share down, renamed source, tiny budget,
watchdog restarts) change data or services. They are **lead + owner only**; do not start them.

---

## 5. Done (lead moves finished tasks here with a one-line result)

- 2026-10-16 lead: unit suite 1886/0; audit lab W6+W12 13/13 after the rollover clock fix (acde356).
