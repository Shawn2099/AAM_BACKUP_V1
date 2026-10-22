# Plan: FY folders - early new folder, safe source picker, rollover bug fixes (2026-10-22, rev 2)

Status: rev 2 after Opus review (3 blockers fixed). Source: owner discussion 2026-10-22 + Opus reviews
(agent ac3d7db0990c2ef9e). Owner approved all recommended answers below.

## Owner decisions (binding)
1. Create the NEW local FY folder early (source side + placeholder), only when the rollover is exactly the
   next FY (FORWARD) on a PROVEN clock (never ROL-03, CLK-03 or JUMP), together with an alarm (mail + red
   dashboard card) "N files in <to_fy> are NOT backed up until the rollover is approved".
2. Config key `rollover.create_source_folder_early` (default true).
3. No interim sync of the new folder (post-MVP; if ever: cloud-only `rclone copy`, no deletes).
4. Settings can NEVER change the FY. Only the rollover does. (Includes FY -> no-FY and no-FY -> FY: first
   adoption of FY folders is a support edit of config.yaml; the refusal text says so.)
5. No toggle for the source/NAS FY match check. A mismatch stays a hard error at config load.
6. `paths.source_drive` and `paths.lan_destination` move from BASIC to Advanced; a parent-only picker lets the
   owner choose the drive/parent folder (the FY leaf stays = the configured FY). The picker never creates
   folders.
7. OPEN (not built here): catch-up backup of the OLD FY folder after the switch (edits to last year's files
   are never backed up again). Flagged in the handoff; owner decides later.
8. OPEN (ask owner, default = accepted risk): detector for "FY changed by a hand edit of config.yaml without a
   rollover" (configured FY differs from the FY of each leg's last successful run and no journal has it as
   to_fy -> ACTION_REQUIRED).

## Bugs found by Opus (fix first, independent of the features)
- **C (one-sided FY leaf)**: validator compares FY leaves only when BOTH paths have one, but `configured_fy`
  accepts either. Source `D:\DATA` + NAS `...\FY26-27` -> rollover rewrites the source to an empty
  `D:/FY27-28`. Fix: both paths end in an FY leaf, or neither (rollover off). Config load refuses the mixed case.
- **A (completed journal re-entered)**: `rollover_open` returns the existing completed journal; if config is put
  back to `from_fy` nothing resets and the run records ROLLOVER_COMPLETE every morning.
- **B/D (hand switch invisible, Settings not rollover-aware)**: switching both leaves to the next FY in Settings
  with no open journal is classified NONE: no final backup, no archive, no alert.

## Steps (test first, one commit each, full unit suite green before the next)
1. **Bug C**: validator + regression test (mixed leaf refused; both/neither fine; plain-words message). About 60
   test / lab files set FY paths: run the full suite in this commit and fix one-sided configs.
2. **Bug A**: in `_rollover_resume`, after classify and BEFORE the clock check: if `db.rollover_get(from_fy,
   to_fy)` exists, is complete, and config is at `from_fy` -> ROLLOVER_BLOCKED + ACTION_REQUIRED, no COMPLETE
   row, no switch. Message: "set the paths to <to_fy> again; re-running a finished rollover is a support
   action". `/api/rollover` and `manual_run_state` show the same reason instead of "Due".
3. **Bugs B/D**: one shared check function (used by config_edit AND the picker): refuse any change of
   `configured_fy` (including FY<->no-FY) with "use Run FY rollover" / "support edit of config.yaml". Parent-only
   path changes stay allowed even while a journal is open (otherwise a dead source disk deadlocks a blocked
   rollover), with a warning that G4 (AAM-NAS-08) will ask for approval on the next run. No "while due" rule.
4. Move the two path keys to Advanced (risky list, not BASIC); catalog test; help text.
5. Wording: ROL-02 text in `guards.g6_fy_rollover` + email + dashboard card: "staff cannot use <to_fy> until you
   approve here". Check `test_m2_rollover` and approval-card tests together with the new snapshot.
6. **Alarm first** (useful alone, staff can create the folder by hand): pure
   `new_fy_unprotected(source_parent, to_fy, placeholder)` (reuse `first_regular_file` with
   `SOURCE_IGNORE_NAMES | {placeholder}`), valid only while CONFIG_SWITCH is not done. Called at the TOP of
   `rollover_check_flow` (after log retention), in its own try, because the blocked / clock-problem days raise
   early. Read-only, so it may run on an unproven clock. `to_fy` = the open journal's to_fy, else the calendar FY
   only when classify is FORWARD. ACTION alert `rollover:unprotected:<to_fy>` (prefix `rollover:` already
   registered), explicitly resolved once the switch is done (incident closure), reminders per the alert-controls
   plan. Red dashboard card with the count. Tests: placeholder only -> none; real file -> alert; resolved after
   the switch; fires on a G6-blocked day and a ROL-03 day; scan test green.
7. **Early folder**: key `rollover.create_source_folder_early` (model + bounds, config_edit, help, example yaml
   in the same commit). Explicit call in `_rollover_resume` after `rollover_open`, `_record_running` and the D-17
   reset, before the FINAL_BACKUPS block; no backup slot needed. Condition: `kind == FORWARD`, switch pending,
   key on, step not finished. Do NOT reorder `rj.STEPS`, do NOT add `REQUIRES[CONFIG_SWITCH]` (it would mark the
   switch skipped if SOURCE_FOLDER were ever skipped), never mark SOURCE_FOLDER skipped. SOURCE_FOLDER stays
   BLOCKING inside `run_steps`, so a failed early folder is retried and stops the switch. Tests: created when the
   final backup fails; not for JUMP / CLK-03 / ROL-03; idempotent on resume and D-17; failed early folder is
   retried as blocking and stops the switch; existing done journal still readable; key off -> folder appears
   only after the final backups (today); early folder + approved ROL-02 rerun -> not created twice, placeholder
   kept.
8. **Source picker** (Advanced): endpoint lists candidate PARENT folders; the picker never creates folders.
   Server validates with the step-3 shared check: exists, directory (not junction), not a drive root, leaf =
   configured FY, contains a real file (G1), NAS side same leaf (folder + `.AAM_TARGET_MOUNTED`; validated only
   when reachable, else refuse "wake the NAS first", never start WoL from Settings), backup lock free. Shows
   the resulting cloud target `gs://bucket/<fy>`, the G4 warning and that G2 will block a parent with fewer
   files. Typed phrase as for risky keys. One test per refusal.
9. Docs: ARCHITECTURE 5.2/10.7, SESSION_HANDOFF, CLAUDE pointer, EMAIL_CONTENT_SPEC row for the new alert.
10. Full unit suite, audit lab (clean PREFECT_HOME), Opus code review, deploy.

## Risks to keep in view
- Early folder = data saved there is unprotected until the switch; moves out of the old folder get mirrored as
  deletes by the old-FY final backup (G2/G3 only catch big ones). The alarm text says this plainly.
- Placeholder is not in `SOURCE_IGNORE_NAMES`; the alarm excludes it explicitly.
- Every new key: model default + bounds + config_edit + help + example yaml in the same commit.
