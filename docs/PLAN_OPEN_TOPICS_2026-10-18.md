# Plan: open topics after the UI + email audit (agreed 2026-10-18)

Status: **FINAL (rev 5, owner 2026-10-18) - verdict GO (§2b).** Rev 2 = re-review gaps
G-1..G-9 (§2a); rev 3 = two passwords only, DPAPI secrets (S), filelock (K); rev 4 = HTTPS
removed (post-MVP); rev 5 = final pre-development review, corrections F-1..F-14 (§2b);
rev 6 = fresh install everywhere, no database / config migration (D-16).
No open decisions. Nothing built yet.
Branch: `mvp-hardening` (HEAD 781cf01 when written). Box (`innoviztawinserver2016`) at 6adcfb2,
**full control** (owner) - but deploy only at step 10.

This file is the plan to follow. `SESSION_HANDOFF.md` §4g holds a short copy; when they differ,
this file wins. Tick the boxes as work is committed and write the commit hash next to the task.

---

## 1. Rules for every task

1. **Test first.** Each task starts with a regression test that is red on the current code.
2. Full unit suite green before every commit:
   `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600`
3. GitNexus: `impact` (upstream) before editing a symbol; `detect_changes --scope all` before
   every commit. HIGH/CRITICAL risk is reported to the owner before editing.
4. One commit per task, message cites the task ID (e.g. `A3`), ends with
   `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
5. Every new tunable = config key with default + bounds in `models/config.py`, added in the same
   commit, documented in `config.example.yaml` and `core/config_help.py`.
6. Dependencies: stdlib + locked libs (pydantic, pendulum, tenacity, psutil, cronsim, Prefect) +
   `filelock` (approved, D-11). Any other new dependency needs the owner's OK first (D-10).
7. Fail closed but never silent: every failure = a recorded state + an email + a dashboard entry.
8. One source of truth per concern; outputs only render it. Multi-step actions are idempotent and
   resume from persisted state.
9. Do not edit modules the audit lab imports while it runs.

## 2. Owner decisions (2026-10-18)

| # | Decision |
|---|---|
| D-1 | Topic 4 (test mail settings): keep the built "Send test email" only; no pre-save test. |
| D-2 | Recipients: everyone gets every email for now. |
| D-3 | Preview: send one real sample of every email type to the owner before deploy. |
| D-4 | Reports: **scheduled emails = calendar periods** (weekly = previous Mon-Sun, monthly = previous calendar month). **Dashboard downloads and the dashboard "send email" buttons = last 7 / last 30 days up to now.** |
| D-5 | Server IP is static, but the dashboard link is **resolved dynamically**. |
| D-6 | Two passwords: one to enter the dashboard, one for Advanced settings. **Both changed / reset inside Advanced.** A server-side reset script for a forgotten Advanced password: yes. |
| D-7 | Windows Update is stopped on the box. A reboot / power cut mid-run must simply **resume on the next run**. |
| D-8 | Clock check "block" stays a FAILED backup (never approvable). |
| D-9 | Prefect `database is locked` (SQLite): accepted for the MVP. |
| D-10 | New dependencies are allowed when they make the system more stable (owner, rev 2). |
| D-11 | Add `filelock` for the config-file lock (K): **yes** (owner). |
| D-12 | **HTTPS removed from this plan (owner, rev 4)** - post-MVP. Accepted risk for now: the dashboard is plain HTTP on the office LAN (port open to LocalSubnet only), so the two passwords travel unencrypted on the LAN. Group T below is kept only as a record of the options. |
| D-13 | Exactly **two passwords** (dashboard + Advanced), stored only as hashes in config.yaml; changed in Advanced; forgotten -> reset script on the server. `api_key` and the `X-API-Key` header are removed (no setup key either: the installer / reset script asks for the new passwords at the server). |
| D-14 | Encrypt the other secrets (SMTP password, Google Cloud key) with Windows DPAPI **now** -> new group **S**. |
| D-17 | **Rollover retry repeats the final backup** (owner 2026-10-18): a retry that still has to do the config switch re-runs the final backup of the old FY first (resets `final_backups` to pending), so files changed in between are never left behind. |
| D-16 | **Fresh install, no migration** (owner, rev 6): the new version is installed fresh on every system (new database, new config.yaml; old config only as reference). No database upgrade path, no old-config compatibility, no plain-secret migration. |
| D-15 | Everyone on the office network views the dashboard, but **nothing can be installed on the PCs** - only on the server. (Recorded for a later HTTPS decision: a private CA is ruled out; a public certificate needs a domain + DNS API.) |

## 2a. Review rev 2 (2026-10-18) - what changed after re-checking the code

| # | Gap found | Where it goes |
|---|---|---|
| G-1 | **No cross-process lock on config.yaml.** Three writers (dashboard settings, password change, FY rollover) each read-modify-write -> a lost update is possible. On Windows `os.replace` also fails with "Access is denied" while another process has config.yaml open for reading (Python opens files without FILE_SHARE_DELETE) -> a rollover config switch can fail at random. | new group **K** (before A) |
| G-2 | **`X-API-Key` header = master key.** `ui._require_auth` accepts `dashboard.api_key` in a header for every endpoint (approvals, settings). With the new passwords this would bypass both logins. Nothing in production uses it (only tests). | **E** (E9) |
| G-3 | **Scheduled report emails are not recorded** in `notifications` (they call `send_email` directly) -> a report that fails to send is invisible on the dashboard. | **C** (C8) |
| G-4 | The attachment fall-back must cover **any** permanent rejection of a message with attachments (content filters reject .zip with 550/554, not only size 552). Report CSVs use the same ladder. | **B** |
| G-5 | Multi-NIC server: the route to the NAS can be a NAS-only NIC, not the office NIC. Use the default-route interface first. | **D** |
| G-6 | Budget-from-scheduled-start must also be defined for the rollover's final backups and the report flows; several missed nights at boot must give ONE email per leg, not one per night. | **I** |
| G-7 | Abandoned-run email says "ROLLOVER backup abandoned" / uses key `backup:rollover:*` for non-backup modes. | **I** (I3) |
| G-8 | ~~Schema v4 is a one-way upgrade -> DB copy + rollback note.~~ Superseded by D-16 (fresh install, no upgrade). | **10.3** |
| G-9 | Report periods must be computed from the flow run's scheduled time (a report sent late still covers the right week / month). | **C** (C1) |

## 2b. Final pre-development review (rev 5, 2026-10-18) - corrections applied to the tasks

| # | Issue found | Correction |
|---|---|---|
| F-1 | Rule 6 still said "no new dependencies" (contradicts D-10/D-11). | Rule 6 updated. |
| F-2 | **`dashboard.auth_enabled: false` turns sign-in off completely** - the two passwords could be bypassed by one config line, contradicting D-13. | **E10**: sign-in always on; `auth_enabled` removed from the model (D-16: no deprecation entry - a stale key fails config validation loudly); tests sign in through the session helper. |
| F-3 | The reset script runs in another process; dashboard sessions live in the agent's memory. | E2: each session stores a fingerprint of the hash it signed in with and is checked against the CURRENT config on every request (config refresh <= `dashboard.config_refresh_seconds`), so a reset from the script signs everyone out within that time. |
| F-4 | Schema changes were spread over A1 (journal), B3 (rung column) and C8 (report rows) -> two or three migrations. | **SCH (new, first in step 1)**: ONE schema change in the new-database schema (D-16: no upgrade path): `rollover_journal`, `notifications.attachments_rung`, report kind. A1/B3/C8 only use it. |
| F-5 | H1 (dependency upgrades) was near the end: everything would be built and lab-tested on library versions that then change. | **H1 moved to step 0b** (with K1 `filelock`): build on the final libraries. |
| F-6 | The audit lab ran only at step 0 and at release; A, I, B touch the real tools / flows. | **Milestone labs**: full audit lab after step 2 (A + I), after step 5 (B + C + E), and at release. |
| F-7 | S: `load_config` must stay read-only (tests, lab, sandbox configs load it constantly); a migration on load would rewrite files. | S2: plain secrets still load (dashboard amber "secret not encrypted" - dev / tests only); encryption happens only in the installer / Settings save through K (D-16: no start-up migration). |
| F-8 | S: rclone reads the key from several call sites (sync, verify, reporter, preflight, audit). An env var set in one place only would leave others on the file. | S3: ONE place builds the rclone environment (`core/rclone_config.py`) and every rclone call goes through it; use the remote-specific variable `RCLONE_CONFIG_<REMOTE>_SERVICE_ACCOUNT_CREDENTIALS` (beats the config file in rclone's documented precedence); a unit test fails if any rclone `run_tool` call misses it. |
| F-9 | A7: the LAN pipeline creating the NAS folder from the journal must not create a folder in the wrong place. | A7: only when the journal's `config_switch` is `done` AND `paths.lan_destination` equals the journal's new folder AND the NAS share root exists; otherwise the normal G5 path. |
| F-10 | Journal steps need a `skipped` state (cloud disabled -> no archive; NAS shutdown off). | A1: states `pending / done / failed / skipped`. |
| F-11 | C8: report rows in `notifications` must not affect incident threading or the attention banner (they select by mode). | C8: mode `report`, excluded from incident / banner queries; test. |
| F-12 | D: a server without a default gateway (isolated office LAN) makes the default-route probe fail; link-local 169.254.x must never be chosen. | D1: default route -> NAS route -> hostname; exclude 127.x / 169.254.x / 0.0.0.0; tests. |
| F-13 | G3: after S the SMTP password is encrypted per machine, so the preview must run where the secrets can be decrypted. | G3: run the preview on the dev PC with its own local config (or on the box after 10.3); decided at G3, owner's inbox either way. |
| F-14 | 10.3 did not list the new deploy steps of S / E / K. | 10.3: + encrypt secrets (S5), set both passwords (E4), check the service account can write the lock file beside config.yaml (K). |

**Verdict (rev 5): GO.** No open decisions, no blocking risk. Accepted risks: HTTP on the office LAN
(D-12), DPAPI secrets readable by any administrator on the server + re-entry after a server
loss (S), Prefect SQLite locks (D-9).

New dependencies considered (D-10): **`filelock`** - recommended (K; small, pure Python, lock is
released by the OS if the process dies). Rejected: Postgres for Prefect (operations burden for a
small office; D-9), `argon2-cffi` (PBKDF2 600k from the stdlib is OWASP-accepted; a C extension
adds deploy risk), a mail library (smtplib already gives SIZE / reply codes).

## 3. Order of work

| Step | Task group | Size | Done |
|---|---|---|---|
| 0 | Groundwork: commit docs, fix the 7 audit-lab reds, rerun the lab | S | [x] 887123f, 93e8d75, lab 238/0/3 |
| 0b | **H1** dependency upgrades + **K** config store (`filelock`) | S-M | [x] 8806c45, df255a3, 0f20cd1, f6f0a32, a13e389; lab 236/2/3, the 2 reds (w13, w6) fixed + re-run green |
| 1 | **SCH** schema v4 (one migration), then **A** FY rollover journal | L | [x] 04aa69a ... 2d601bb; independent review F1-F4 fixed |
| 2 | **I** Interruption / late start -> **milestone audit lab** | M | [x] e42d2af ... 6c38038; lab 242/0/3 |
| 3 | **B** Emails sized to fit | M | [x] e63ad59, a7480ae |
| 4 | **C** Reports: one model | M | [~] C1-C6, C8 done (619d69b, d7a5b1b); C7 proof at step 10 |
| 5 | **E** Passwords (incl. E10 sign-in always on) -> **milestone audit lab** | M | [~] E1-E10 done; milestone lab pending |
| 6 | **D** Dashboard address | S | [ ] |
| 6c | **S** Secrets encrypted with Windows DPAPI | M | [ ] |
| 7 | **F** Dated log names | S | [ ] |
| 8 | **H** Older items | S-M | [ ] |
| 9 | **G** Email preview -> owner review -> fix feedback | S | [ ] |
| 10 | Release: suite + audit lab + code review -> deploy -> M5 -> 7 nights | - | [ ] |

---

## Step 0 - Groundwork

- [x] **0.1** Commit the doc changes of 2026-10-18 (CLAUDE.md + SESSION_HANDOFF.md: box = full
  control; this plan).
- [x] **0.2** Tidy stale handoff parts: §3 test status, "Built but NOT wired yet" title (all wired),
  §4 item 6 (duplicate of M4), §6 old v1.1.3 facts marked background.
- [x] **0.3** Fix the 7 audit-lab reds from §4f (log `agent_runs/audit_lab_2026-10-18_email.log`):
  - w14 auth + w19 session: use `dashboard.session_hours` instead of the removed `ui._SESSION_TTL`.
  - w19 flash test: covers removed dead code -> remove / replace with the current behaviour.
  - w19 email endpoints: fake gets the new `legs` argument.
  - w8 report SMTP outage: seed runs in the period the report covers.
  - w10: coverage floor `core/health.py` 44.8% < 45% -> add real coverage (preferred) or set the
    floor with the reason written in the test.
  - w14 `/report/weekly` + `/report/monthly` 404: find out if it is seed-date rot or real.
    (The report period itself is rebuilt in C; here only make the lab truthful.)
- [x] **0.4** Rerun the full audit lab (background, ~20-30 min) -> 0 red. Record the result in the
  handoff.

---

## K - Config store: one locked writer for config.yaml (rev 2, G-1)

**Why:** dashboard settings (`config_edit.apply_changes`), the password change
(`config_edit.set_admin_password_hash`) and the FY rollover (`fy_rollover.update_config_yaml`)
each read-modify-write config.yaml with no lock between processes; `write_atomic`'s
`os.replace` fails on Windows while a reader has the file open.

**Design:** `core/config_file.py` becomes the only writer: `with config_transaction(path) as doc:`
= cross-process lock (`filelock`, D-11; lock file beside config.yaml, timeout = config key) ->
round-trip load -> caller edits -> backup -> atomic write -> `os.replace` retried on
`PermissionError` with a short bounded back-off (config keys) -> reload + validate with
`load_config` -> restore the backup if invalid. Every writer uses it; compare-and-set callers
(rollover) check the current value inside the lock.

**Tasks:**
- [x] **K1** (df255a3) Add `filelock` (owner OK, D-11) to `pyproject.toml` / `uv.lock`.
- [x] **K2** (0f20cd1) `config_transaction` + config keys `maintenance.config_lock_timeout_seconds`,
  `maintenance.config_replace_retries` / `_delay_ms` + tests: two processes writing at once
  (no lost update); a reader holding the file open during the replace (retried, succeeds);
  invalid result restored.
- [x] **K3** (f6f0a32, a13e389) Move the three writers onto it (settings, admin hash, rollover); existing tests green.

---

## A - FY rollover as a persisted, resumable step journal (topic 5)

**Why (findings in `flow.rollover_check_flow`, `core/fy_rollover.py`):**
- **R1** The final LAN backup runs the normal LAN pipeline, which **shuts the NAS down** at the end
  (`lan_shutdown_task`). Only afterwards does `create_new_fy_folders` try to create the new NAS FY
  folder -> fails on a powered-off NAS -> only a log line, config still switches -> first new-FY
  LAN night is blocked (marker missing) without explanation. (Hidden on the box: shutdown is off.)
- **R2** An exception in the switch (backup slot busy, source mkdir, config backup / write) = no
  run row, no email, possibly half done (folders created, config not switched).
- **R3** A crash between the config write and the run row = switched silently; the next morning
  the check sees "nothing to do" -> never reported.
- **R4** Archive transition failure = log warning only, never retried (the config has moved on),
  and the "New financial year started" INFO email still says "Nothing to do".

**Design (saga pattern: persisted state, idempotent steps, resume after crash):**
- Table `rollover_journal` (in the new-database schema, SCH; no upgrade path, D-16): one row per
  `(from_fy, to_fy)`; per step: state `pending | done | failed`, attempts, last error, started /
  finished times, run id.
- Steps, each idempotent (safe to run twice):
  1. `final_backups` - final backup of the closing FY on every enabled leg, then G6.
     The final LAN backup runs **without** the NAS shutdown.
  2. `source_folder` - new source FY folder + placeholder (exist_ok, never overwrite).
  3. `nas_folder` - NAS awake / SMB ready (same mechanism as the LAN leg), new NAS FY folder +
     mount marker.
  4. `config_switch` - through K's `config_transaction`: compare-and-set inside the lock (write
     only if `config.yaml` still points at `from_fy`); backup, validate, restore on failure.
  5. `archive` - move `gs://<bucket>/<from_fy>/` to ARCHIVE (already idempotent).
  6. `nas_shutdown` - shut the NAS down (normal shutdown task, only if configured).
  7. `announce` - INFO email; says "Nothing to do" only when every step is `done`.
- The daily 06:00 rollover check is the **resumer**: it reads the journal and continues at the
  first step that is not `done`. Nothing guessed, nothing done twice.
- A `ROLLOVER_RUNNING` row is written before step 2 -> a crash is swept as ABANDONED + one email
  (`backup_repository.sweep_abandoned_runs`; verify it covers mode `rollover`).
- `nas_folder` failed (NAS unreachable): the config still switches (the cloud must protect the new
  FY from day 1); the step stays `pending`; the **next LAN pipeline** (NAS awake) completes it from
  the journal before the guards -> no approval card for our own folder.
- Failure before `config_switch`: nothing changed except harmless folders -> `ROLLOVER_FAILED` row +
  ACTION_REQUIRED email **AAM-ROL-04**; the next morning resumes.
- Failure in / after `config_switch`: restore the backup, re-validate; restore failed too ->
  ACTION_REQUIRED with the exact manual step.
- `archive` failed: WARNING **AAM-ROL-05** ("your data is safe; the old year is still in the more
  expensive storage class"); retried every morning; ACTION_REQUIRED after
  `rollover.archive_alert_after_days`.
- Dashboard: a rollover card listing each step and its state while a rollover is open.

**Design rules added before building (rev 7, 2026-10-18, pre-build review):**
1. The resumer looks up `rollover_incomplete()` FIRST and classifies second (after
   `config_switch` the config already points at `to_fy`, so classify says NONE). The clock
   proof gates only a pending `config_switch`.
2. Step semantics are pure data in `core/rollover_journal.py`: BLOCKING steps (final_backups,
   source_folder, config_switch) stop the run on failure; non-blocking steps (nas_folder, archive,
   nas_shutdown, announce) do not; dependencies: nas_shutdown needs nas_folder done, announce needs
   config_switch done. The resumer walks every unfinished step, not only the first.
3. Config switch = compare-and-set, three outcomes decided inside the transaction: at `from_fy`
   -> switch; already at `to_fy` -> step done (R3, crash after the write); anything else -> refuse,
   write nothing, ROL-04.
4. D-17: a run that still has to do `config_switch` first resets `final_backups` to pending.
5. A4 suppresses EVERY `lan_shutdown_task` call site in `_run_lan_pipeline` (complete, partial,
   exception branch), incl. `shutdown_on_all_retries_exhausted`.
6. `first_failed_at` per step (cleared on done) is what `archive_alert_after_days` counts from.
7. ROL-04 / ROL-05 use stable incident keys (`rollover:<to_fy>:<step>`), no run id.
8. `nas_folder` raises on failure (no swallow), reuses the LAN leg's wake + `smb_ready` and
   `guards.MOUNT_MARKER`; A7 runs after wake/ready and before G5, only under the F-9 conditions.
9. Final backups stay OUTSIDE `_backup_slot` (the pipelines take the slot; limit 1 = deadlock);
   folders + switch inside one hold.
10. `ROLLOVER_RUNNING` / `ROLLOVER_FAILED` in `core/statuses.py` (category, label, description);
    a rollover row must never count as a backup's "last success".

**Tasks:**
- [x] **SCH** (04aa69a; `first_failed_at` added with A5) (rev 6, D-16) ONE schema change for the whole plan, in the schema a NEW database is
  created with: `rollover_journal` (states pending / done / failed / skipped),
  `notifications.attachments_rung`, report rows (mode `report`). **No upgrade path from old
  databases** (fresh install); test = a fresh DB has every table / column.
- [x] **A1** (04aa69a) `rollover_journal` repository functions (create / get / mark step) + tests.
- [x] **A2** (f59f90a) Fault codes AAM-ROL-04 / AAM-ROL-05 in `core/faults.py` (+ email wording in
  `core/email_compose.py`, `docs/EMAIL_CONTENT_SPEC.md`).
- [x] **A3** (f59f90a) Config key `rollover.archive_alert_after_days` (default + bounds, example, help).
- [x] **A4** (c00878d) Final LAN backup inside the rollover runs without NAS shutdown (pipeline parameter);
  test: shutdown task not called during the rollover's final backup.
- [x] **A5** (ada3955) Steps as idempotent functions in `core/fy_rollover.py`; config switch as
  compare-and-set + validate + restore.
- [x] **A6** (1c12d27, 2d601bb) `rollover_check_flow` rewritten as the journal resumer; `ROLLOVER_RUNNING` row;
  ROLLOVER_FAILED / ROLLOVER_COMPLETE rows; truthful INFO email.
- [x] **A7** (1919a33) LAN pipeline completes a pending `nas_folder` step (NAS awake) before the guards.
- [x] **A8** (5cfa561) Dashboard rollover step card (`ui.py` + `static/js/dashboard.js`).
- [x] **A9** (1c12d27, a6 tests) Calendar-simulation tests (`tests/test_calendar_simulation.py` style) on 1 April:
  slot busy; NAS off; config not writable; crash after the config write; restore failing;
  archive failing (+ escalation after N days); resume on 2 April; each = correct row + exactly
  one email + journal state.
- [x] **A10** (this commit) Audit lab W6: NAS switched off after the final backup -> folder still created,
  first new-FY LAN night COMPLETE without approval.

**Done when:** every failure path above has a row + email + journal state, resume works after a
crash at every step, the full suite and lab W6 are green.

---

## I - Interruption (reboot / power cut mid-run) resumes on the next run (D-7)

**Already OK (verified 2026-10-18):** Prefect 3.7.2 `concurrency()` holds the `aam-backup` slot on
a 300 s lease renewed while held -> a dead process frees it; robocopy runs with `/Z` (restartable);
GCS uploads are atomic; `sweep_abandoned_runs` marks a dead-owner RUNNING row ABANDONED + one
email; `backup.lock` is checked by PID.

**Gap:** the run budget counts from the moment the job really starts. After a reboot Prefect starts
a missed scheduled run late (e.g. 09:30) with a full budget -> it runs into office hours, and the
schedule proof (`core/budget.py`) assumes jobs start at their scheduled time.

**Design:** the budget counts from the flow run's **scheduled** start (Prefect
`expected_start_time`); manual runs count from the actual start. No budget left at start ->
`*_MISSED` row ("the server was off / busy at the scheduled time") + WARNING email; the next night
continues incrementally. No new key: the budget itself is the limit.
Rev 2 (G-6): the rollover's final backups count their budget from their own start (they are part
of a rollover run, not a schedule slot); report flows have no budget - they run late and still
report the right period (C1). Several missed nights at boot -> one `missed:<leg>` incident per leg
(the incident threading of eaf998d suppresses repeats while unchanged), not one email per night.
Verify in I4 that `prefect serve` really starts late runs after a restart and that the
server-side lease expiry frees the slot (Prefect 3.7.2 `concurrency(lease_duration=300)`).

**Tasks:**
- [x] **I1** (e7a53d1) `*_MISSED` statuses in `core/statuses.py` (category, label, description) + fault code.
- [x] **I2** (e7a53d1) `RunBudget` from the scheduled start; late start with no budget -> `*_MISSED` row +
  WARNING email; partial budget -> runs with what is left (ends `*_PARTIAL` if needed).
- [x] **I3** (e42d2af) Verify `sweep_abandoned_runs` covers cloud, LAN, audit and rollover rows; add tests
  for any mode not covered. Fix the wording / key for non-backup modes (G-7: today "ROLLOVER
  backup abandoned", key `backup:rollover:*`) -> "FY rollover interrupted" etc.
- [x] **I4** (e42d2af) Audit-lab scenario: hard-kill the process tree mid-sync (cloud and LAN), mid-audit,
  mid-rollover; restart -> ABANDONED + exactly one email, lock gone, slot free within the lease,
  next run completes, destination == source, no false guard block.

**Done when:** I4 is green on the real tools and a late start can never run past its scheduled
budget.

---

## B - Emails sized to fit; never lost because of attachments (topic 1)

**Why:** the 5 MB cap is fixed; the mail server is never asked what it accepts; a 552 "too large"
is permanent -> not retried -> the alert is lost; a server that drops the connection on a large
message gets the same message three times, then the alert is lost.

**Design:**
- `_send_once` -> `SmtpSession` (connect, EHLO, read the **RFC 1870 SIZE** limit, send). Still ONE
  transport (`core.notify.send_email`).
- Before sending: measure the encoded message (base64 ~4/3) against
  min(server SIZE, `alerts.attachments_max_total_kb`) and pick the richest rung that fits:
  1. support report + logs zip
  2. support report + tail-only zip
  3. support report only
  4. no attachments + "Logs could not be attached; they are on the server at <path>"
- **Any** permanent rejection (5xx) of a message that has attachments (size 552, or a content
  filter refusing .zip with 550/554), or a disconnect during DATA -> one rung down and resend at
  once (a separate step, not a retry of the same message). The last rung (no attachments) gets the
  normal handling. Report emails (CSV attachment) use the same ladder (rev 2, G-4).
- The `notifications` row records the rung delivered; the "For support" block says so.

**Tasks:**
- [x] **B1** (e63ad59, as `_smtp_session`) `SmtpSession` refactor (behaviour unchanged) - existing transport tests green.
- [x] **B2** (e63ad59, a7480ae; rungs = full, report-only, none built by `notify.attachment_ladder`; no logs-tail rung) Size measurement + rung choice (`core/support_bundle.py` builds each rung).
- [x] **B3** (e63ad59) Rejection / disconnect fallback; record the rung (schema: column or extended field -
  the column comes from SCH).
- [x] **B4** (e63ad59, a7480ae) Tests with the real SMTP test server (`test_support/smtp_server.py`): small SIZE
  advertised; 552 reply; hang-up mid-DATA; SIZE 0 (no limit) -> each delivers exactly one email.

**Done when:** no attachment problem can stop an alert from being delivered.

---

## C - Reports: one model, several renderers (topic 3, D-4)

**Findings (`core/report.py`):**
- **P1** A period with no runs sends **no report** (`build_summary_email` returns None) - the week
  nothing ran is the week nobody hears.
- **P2** Weekly = rolling 7x24 h before sending, labelled as calendar days.
- **P3** "Resolved" only if a good run falls inside the same period (a failure on the 31st fixed on
  the 1st still reads "action needed").
- **P4** Totals count rollover rows as backups; "Files Backed Up" adds cloud and NAS together.
- **P5** Integrity section shows both legs even when one is disabled.
- `_serve_report` text "No runs found in the last N days" is wrong for calendar periods.

**Design:**
- `ReportPeriod` value object: start / end (IST, stored UTC), label, kind. Constructors:
  `previous_week()` (Mon 00:00 - Mon 00:00 IST), `previous_month()` (calendar),
  `last_days(n, now)` (rolling, up to now).
- Scheduled emails: `previous_week()` / `previous_month()`. Dashboard downloads + dashboard
  "send email" buttons: `last_days(7)` / `last_days(30)`.
- `ManifestDB.get_runs_between(start, end)` replaces the rolling `get_runs_since(days)` for reports.
- `ReportModel` built once: per-leg backup totals (backup modes only), last good copy, problems
  with "resolved" checked against the full history, latest audit per enabled leg, open decisions,
  data copied per leg, headline.
- Renderers: email HTML, download HTML, CSV, subject - all from the model only.
- An empty period still produces a report ("action needed: no backup ran"); the download says
  "No backups in <period>".

**Tasks:**
- [x] **C1** (619d69b) `ReportPeriod` + tests (IST boundaries, month lengths incl. February / 31-day months,
  DST-free IST, runs over midnight, a run in progress).
- [x] **C2** (619d69b) `get_runs_between` + tests.
- [x] **C3** (d7a5b1b) `ReportModel` (fix P3, P4, P5) + tests.
- [x] **C4** (d7a5b1b) Renderers on the model; empty period still reports (P1); subject from the model.
- [x] **C5** (d7a5b1b) Wire: scheduled flows (calendar), `ui.py` download + send buttons (last 7 / 30 days),
  `_serve_report` text.
- [x] **C6** (d7a5b1b) Consistency test: report = CSV = dashboard counts for the same period.
- [ ] **C7** (needs the trial box: do at step 10, with the deploy) Proof: copy the box DB read-only, generate weekly + monthly + last-30-days reports,
  email them to the owner.
- [x] **C8** (d7a5b1b) (rev 2, G-3) Scheduled report emails are recorded in `notifications` (kind REPORT,
  sent / failed, attempts); the dashboard shows a failed report delivery like a failed alert.

Rev 2 (G-9): `previous_week()` / `previous_month()` take the flow run's **scheduled** time, so a
report sent late (agent was down) still covers the right period.

**Done when:** owner has reviewed the real reports from the box DB.

---

## E - Passwords: dashboard + Advanced, both managed in Advanced (D-6)

**Today:** dashboard sign-in = `dashboard.api_key` (plain text in config.yaml, changed only on the
server); Advanced password = that key until changed, then a PBKDF2 hash
(`dashboard.admin_password_hash`); no recovery for a forgotten Advanced password.

**Credentials map (D-13):**

| Credential | Who | Stored | Changed | Forgotten |
|---|---|---|---|---|
| Dashboard password | everyone who opens the dashboard | `dashboard.login_password_hash` (PBKDF2 hash only) | Advanced -> Passwords | `deploy/11_reset_dashboard_passwords.ps1` on the server |
| Advanced password | owner / IT | `dashboard.admin_password_hash` (PBKDF2 hash only) | Advanced -> Passwords | same script |

Removed: `dashboard.api_key` (removed from the model; D-16 fresh install, no deprecation entry) and the `X-API-Key`
header login. `/health` stays unauthenticated (monitoring, `launch.py` probe). First install: the
installer asks for both passwords at the server console and writes the hashes. Until both hashes
exist the dashboard shows only a "not set up - run the installer / reset script on the server"
page (fail closed).

**Design:**
- Credential store in config.yaml via K's `config_transaction` (lock, atomic write, backup, settings history):
  `dashboard.login_password_hash` (**new**) and `dashboard.admin_password_hash`; both
  PBKDF2-SHA256, 600,000 iterations (OWASP; stdlib `hashlib`).
- Advanced -> "Passwords" card: **Change dashboard password**, **Change Advanced password**.
  Both require re-entering the current Advanced password (re-authentication), the new password
  twice, the minimum length (`dashboard.admin_password_min_length`); the two passwords must differ.
- Sessions carry a credential version: after a change every OTHER session signed in with the old
  password ends (OWASP); the person who changed it stays signed in.
- INFO email to all recipients: "Dashboard password changed by <who> at <time>" (no password).
- Forgotten password: `deploy/11_reset_dashboard_passwords.ps1` (run as administrator on the
  server) asks for a new dashboard and / or Advanced password (typed twice, same rules), writes
  the hashes through the Python config store, signs every session out (agent picks up the new
  credential version), sends the INFO email; documented in `docs/DEPLOY_RUNBOOK.md`.

**Tasks:**
- [x] **E1** (E commits) Config key `dashboard.login_password_hash` (validator like the admin hash; hidden in
  settings; lock-out key list) + `core/admin_auth.py` login verification.
- [x] **E2** (E commits) Session credential versions (dashboard + Advanced) + invalidation tests.
- [x] **E3** (E commits) API endpoints: change dashboard password / change Advanced password (re-auth, rules,
  rate limit) + tests.
- [x] **E4** (E commits) Not-set-up state (no hashes -> setup page only, fail closed) + installer prompt for
  both passwords (`06_install_services.ps1`) + tests; `dashboard.api_key` removed from the model.
- [x] **E5** (E commits) Advanced "Passwords" card in the settings UI.
- [x] **E6** (E commits) INFO email on change (fault-free INFO kind; sample for G).
- [x] **E7** (E commits) `deploy/11_reset_dashboard_passwords.ps1` + runbook section; test the script logic
  (Python helper it calls) in the suite.
- [x] **E8** (E commits) E2E on a real uvicorn server: change both, old sessions signed out, new ones work.
- [x] **E10** (E commits) (rev 5, F-2) Sign-in always on: `dashboard.auth_enabled` removed from the model; every
  "auth disabled" branch in `ui.py` removed; tests use the session helper.
- [x] **E9** (E commits) (rev 2, G-2) Remove the `X-API-Key` header login (`ui._check_api_key_header`):
  it is a master key that would bypass both passwords; nothing in production uses it. Tests move
  to a session-login helper (`test_support`), not a header.

Note (D-12): no HTTPS in this plan - passwords travel unencrypted on the office LAN (accepted
risk for now; post-MVP).

---

## D - Dashboard link resolved dynamically (topic 2, D-5)

**Design:**
- `dashboard.public_url` empty = automatic. New `core/netinfo.py`: the local IPv4 of the
  **default-route interface** (the office network), else the one that routes to the NAS
  (`wol.server_ip` / the LAN destination host) - rev 2 (G-5): on a server with a separate NAS
  NIC the NAS route is not the office address. UDP socket connect + `getsockname`; no packet is
  sent; stdlib only. Readiness lists every IPv4 of the server so the owner can confirm.
- The chosen address must be one the dashboard listens on (`bind_address` 0.0.0.0 or equal),
  otherwise the link problem is reported (amber), never a dead link.
- Resolved each time an email is built (short in-process cache); emails show
  `http://<ip>:<port> (or http://<hostname>:<port>)`.
- A manually set `public_url` that differs from the detected address -> readiness check
  (`04_check_readiness.ps1`) + watchdog daily check -> dashboard amber.
- Firewall rule stays LocalSubnet (independent of the IP).

**Tasks:**
- [ ] **D1** `core/netinfo.py` + tests (route chosen, no network -> hostname fallback, never
  127.0.0.1 / 0.0.0.0 in a link).
- [ ] **D2** `notify.dashboard_url` / `dashboard_link_problem` use it; email link text.
- [ ] **D3** Readiness + watchdog mismatch check -> amber.

---

## T - HTTPS for the dashboard - REMOVED FROM THIS PLAN (owner, rev 4; post-MVP record only)

> **Not to be built now.** Kept only so a later HTTPS decision does not start from zero.

> **Rev 3 (D-15): nothing can be installed on the office PCs, so option 2 below (private CA) is
> ruled out.** New proposal = a publicly trusted certificate (Let's Encrypt, DNS-01), see
> "Design (rev 3)" at the end of this section. Option 2 text kept for the record.

**Why:** the two dashboard passwords and session cookies travel unencrypted on the office LAN.

**Options considered:**
1. Self-signed certificate - a browser warning on every visit forever; staff learn to click
   through warnings. Not proper. Rejected.
2. **Private office CA (recommended).** One-time: the installer creates "<firm> Backup Dashboard
   CA"; each office PC trusts it once (double-click the .cer -> Local Machine -> Trusted Root,
   ~1 minute per PC). The server certificate is then renewed automatically for years with no
   further PC visits. Works with the IP link (D-5), offline, no domain needed.
3. Public certificate (Let's Encrypt DNS-01) - no per-PC step, but needs a domain the client
   owns + a DNS-provider API key on the server + internet; public CAs never issue for a private
   IP (conflicts with the IP link); certificates live 90 days (shrinking further) -> a renewal
   that can break. Rejected for this office.

**Design (option 2):**
- `core/tls.py` with `cryptography` (already installed via Prefect, v48 -> made a direct
  dependency): CA key + certificate (10 years) with a critical **Name Constraints** extension
  limited to the office subnet + the server's names, so even a stolen CA key cannot sign for any
  other site; server certificate (SAN = server IPv4(s) + hostname + FQDN, validity 825 days -
  the limit iPhones/Macs accept for private CAs), renewed automatically 30 days before expiry
  and whenever the server's IP / name is no longer in the SAN (D-5).
- Keys in the runtime folder, ACL: Administrators + the service account only.
- uvicorn serves HTTPS on `dashboard.port`; optional plain-HTTP listener
  (`dashboard.http_redirect_port`) that only redirects to HTTPS and offers
  `/office-certificate` (the public CA .cer + install steps) for first-time PCs.
- Cookies `Secure` (already scheme-based); emails link `https://<ip>:<port>`; `launch.py`
  health probe and audit lab use HTTPS verified against the CA.
- Readiness: certificate valid, SAN matches the detected address, days to expiry; watchdog:
  email if an automatic renewal fails (WARNING -> ACTION_REQUIRED 14 days before expiry).
- Config keys: `dashboard.tls_enabled` (default true for new installs), `dashboard.tls_dir`,
  `dashboard.tls_cert_days` (bounds <= 825), `dashboard.tls_renew_before_days`,
  `dashboard.http_redirect_port` (0 = off).

**Tasks:**
- **T1** `core/tls.py`: create CA (name-constrained), issue / renew server cert, inspect
  (expiry, SAN) + tests (chain verifies, constraint rejects a foreign name, renewal keeps the CA).
- **T2** Config keys + uvicorn HTTPS in `launch.py`; health probe over HTTPS.
- **T3** Redirect listener + `/office-certificate` page.
- **T4** Daily renewal check (agent) + watchdog email on renewal failure / near expiry.
- **T5** Installer: create CA + cert, export the .cer; readiness checks; runbook "trust the
  office certificate on each PC" (Windows; Firefox uses the Windows store; phones optional).
- **T6** E2E: real uvicorn over TLS, browser-equivalent client trusting only the CA;
  login cookies Secure; IP change -> re-issued certificate.

### Design (rev 3) - public certificate, nothing installed on the PCs

- A name per client under a domain we control, e.g. `backup-<client>.<our-domain>`, with a
  public DNS **A record pointing to the server's office IP** (private address; works because
  Let's Encrypt **DNS-01** validation never connects to the server - it only checks a TXT
  record we create through the DNS provider's API).
- ACME client: **`lego`** (single pinned Windows binary in `deploy/bin`, like rclone; run through
  `run_tool`; supports the common DNS providers). Certificate + key in the runtime folder (ACL:
  Administrators + service account); the DNS API token encrypted with DPAPI (S).
- Renewal: daily check, renew 30 days before expiry (public certificates are 90 days today and
  shrinking by CA/Browser Forum rules, so renewal must be fully automatic); uvicorn reloads the
  new certificate (agent restart of the dashboard thread); renewal failure -> WARNING email,
  ACTION_REQUIRED 14 days before expiry.
- IP change (D-5): netinfo detects it -> the DNS A record is updated through the same API ->
  the link name never changes. Emails link `https://backup-<client>.<our-domain>:<port>`.
- Fallback: if the office router blocks public names that resolve to private addresses ("DNS
  rebinding protection"), the readiness check detects it (resolve the name from the server
  through the office DNS) -> amber + runbook (allow the name on the router, or use the hosts
  entry on the server only); the dashboard keeps working over the IP until fixed.
- Config keys: `dashboard.tls_enabled`, `dashboard.tls_hostname`, `dashboard.tls_dns_provider`,
  `dashboard.tls_renew_before_days`, `dashboard.http_redirect_port` (0 = off).

**Tasks (rev 3, replace T1-T6):**
- **T1** Pin `lego` in `deploy/bin` (version + checksum); `core/tls.py`: issue / renew / inspect
  through `run_tool` + tests with a fake lego (real-format output).
- **T2** DNS A-record update (provider API through lego or the provider's REST API) on IP change.
- **T3** uvicorn HTTPS + certificate reload; HTTP redirect listener; health probe over HTTPS.
- **T4** Daily renewal check + emails; readiness checks (cert valid, name resolves to this
  server from the office DNS, days to expiry).
- **T5** Installer + runbook (domain, DNS token, first issue); E2E with the Let's Encrypt
  **staging** server before production.

---

## S - Secrets encrypted with Windows DPAPI (D-14)

**Why:** `notifications.smtp_password` is plain text in config.yaml and the Google Cloud service
account key is a plain JSON file; both are protected only by file permissions, and leak with any
copy of the file (backup, support zip, a mistaken commit - the GitHub repo is public).

**Design:**
- `core/secrets.py`: Windows DPAPI via `ctypes` (`CryptProtectData` / `CryptUnprotectData`, stdlib,
  no new dependency) with **machine scope** + an application-specific entropy. Machine scope
  because three accounts need the secrets: the agent (`.\aambackup`), the watchdog (LocalSystem,
  sends emails) and the installer (administrator). Encrypted values are bound to THIS server:
  a copied config.yaml / key file is useless elsewhere.
- config.yaml stores `smtp_password: "dpapi:<base64>"`; the pydantic model decrypts on load and
  keeps the plain value only in memory; the Settings page encrypts on save (through K).
  The installer encrypts the values it is given (fresh install, D-16); a plain value still loads
  (dev / tests) but shows amber.
- Google Cloud key: stored encrypted (`gcs_key.json.dpapi`); never written to disk in plain text:
  rclone gets it in memory through its environment variable
  (`RCLONE_GCS_SERVICE_ACCOUNT_CREDENTIALS`) on the child process only. The FY archive step
  (gcloud needs a key FILE) moves to `rclone settier ARCHIVE` if the real rclone v1.74.3 supports
  it for GCS (verify first, S4); otherwise a temp file with an owner-only ACL, deleted in
  `finally`.
- Support bundle redaction keeps working (it redacts the decrypted values too).
- Disaster recovery: a new server cannot decrypt the old config -> runbook: re-enter the SMTP
  password and re-download the Google key (keep the original key file offline, outside the
  server); `09_restore_from_gcs.bat` documented accordingly.

**Tasks:**
- [ ] **S1** `core/secrets.py` (protect / unprotect / is_protected) + tests (round trip, wrong
  entropy fails, non-Windows guard for CI).
- [ ] **S2** Model + Settings: `smtp_password` encrypted at rest; a plain value loads with amber
  (no migration, D-16); tests.
- [ ] **S3** Google key encrypted; rclone via environment variable (verify with the real binary
  that `RCLONE_GCS_SERVICE_ACCOUNT_CREDENTIALS` works for sync / check / lsjson / size).
- [ ] **S4** FY archive step: verify `rclone settier` on GCS with the real binary; switch to it
  (removes the gcloud dependency) or temp-file fallback.
- [ ] **S5** Installer encrypts the key file + SMTP password; readiness checks both decrypt as
  the service account AND as LocalSystem; runbook DR section.

---

## F - Date and time in log names (topic 6)

**Design:** run folder `run_logs/<day>/2026-10-18_2100_lan_<short id>/`, files
`2026-10-18_2100_lan_robocopy-sync.log`. Built only in `core/run_artefacts.RunArtefacts`
(started time IST + leg + short run id); the DB run id is unchanged; retention still removes
whole day folders; the support zip keeps the names. (No old-name compatibility needed, D-16.)

**Tasks:**
- [ ] **F1** `RunArtefacts` naming + tests (purge still removes day folders).
- [ ] **F2** Callers pass started time + leg (`RunArtefacts.for_config`); dashboard + support
  bundle read the new names.

---

## H - Older items

- [x] **H1** (8806c45; done in step 0b, rev 5 F-5) `uv audit` (36 advisories, all transitive via Prefect):
  `uv lock --upgrade-package <pkg>` one at a time; full suite + audit lab after the set.
- [ ] **H2** `deploy/03_setup_system.bat`: Active Hours 12:00-06:00 = 18 h is **invalid on
  Windows Server 2016** (max 12 h, Microsoft "waas-restart"). Write a valid 12 h window
  (20:00-08:00); `04_check_readiness.ps1` reports the real Windows Update state (stopped / policy /
  Active Hours) instead of assuming. (Windows Update is stopped on the box now - D-7.)
- [ ] **H3** `04_check_readiness.ps1` runs `uv sync --frozen` without `--no-dev` -> add `--no-dev`.

---

## G - Preview of every email (D-3)

**Design:**
- `core/email_samples.py`: one realistic sample per email kind, per fault-code family, reminder +
  RESOLVED in a thread, weekly + monthly + last-30-days reports, password notice, ROL-04 / ROL-05,
  `*_MISSED`, each attachment rung. Unit test: every fault code in `core/faults.py` has a sample.
- `tools/preview_emails.py --to <address> [--out <dir>]`: renders through the real composer and
  sends through the real transport with `[PREVIEW]` in the subject; no notification rows, no
  cooldown; `--out` writes `.eml` files.

**Tasks:**
- [ ] **G1** Sample catalogue + coverage test.
- [ ] **G2** Preview command + test against the SMTP test server.
- [ ] **G3** Send to the owner; owner reviews in Gmail / Outlook / phone; fix feedback.

---

## Step 10 - Release

- [ ] **10.1** Full unit suite 0 failed; full audit lab 0 red; `/code-review` of the whole diff
  since 781cf01; findings fixed or triaged with the owner.
- [ ] **10.2** Update `docs/ARCHITECTURE.md` (§10 gates, rollover journal, report model,
  credentials, interruption), `docs/EMAIL_CONTENT_SPEC.md`, `config.example.yaml`,
  `docs/DEPLOY_RUNBOOK.md`.
- [ ] **10.3** **Fresh install** on the box (D-16; full control): stop + uninstall the old services,
  keep the old install + runtime folders renamed as reference only (like `C:\AAMBackup_v113`),
  fresh clone, `uv sync`, NEW config.yaml from `config.example.yaml` (old config only as
  reference), new empty runtime / database, `public_url` empty
  (auto) or checked, `bind_address`, run `04_check_readiness.ps1` + `06_install_services.ps1`,
  set both dashboard passwords (E4), encrypt the SMTP password + Google key (S5), check the
  service account can create the lock file beside config.yaml (K), Settings -> "Send test email".
- [ ] **10.4** M5 scenario list on the box (§4a of the handoff) + the new ones: reboot mid-run
  (I4 on the real box), NAS off at rollover (simulated date), password change.
- [ ] **10.5** Seven truthful nights; tune budgets from `extended_metrics.budget.used_hours`.
- [ ] **10.6** Owner changes the `aambackup` password on both machines and re-runs 04 / 06.

## Out of scope for now (post-MVP)

File-list CSV attachment (spec §5.3); separate recipient lists; "test settings before saving";
Postgres for Prefect; LAN leg on rclone; G7 clock approval; **HTTPS for the dashboard** (T record:
needs a domain + DNS API since nothing can be installed on the office PCs).

## Sources (research 2026-10-18)

- Microsoft, Windows Update restart / Active Hours (12 h max on Windows Server 2016):
  https://learn.microsoft.com/ja-jp/windows/deployment/update/waas-restart
- RFC 1870, SMTP SIZE extension: https://www.rfc-editor.org/rfc/rfc1870
- OWASP ASVS v4 ch. 3, session termination on password change:
  https://owasp-aasvs4.readthedocs.io/en/latest/v3.html
- OWASP-cited practice (other sessions removed on password change):
  https://gerrit.openbmc.org/plugins/gitiles/openbmc/bmcweb/+/e518ef320280c4a5e372e15eef86546e483f4f3d
- PBKDF2-HMAC-SHA256 600,000 iterations (OWASP figure, secondary source):
  https://mojoauth.com/security-guides/sha-256-vs-pbkdf2.md
- Saga pattern (persisted state, idempotent steps, compensation):
  https://quality.arc42.org/approaches/saga-pattern ,
  https://www.devx.com/technology/understanding-the-saga-pattern-for-distributed-transactions/
