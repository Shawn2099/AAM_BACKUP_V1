# Plan: fixes after the Opus code review of FY-1..8 (2026-10-22, rev 1)

Status: DRAFT for Opus review. No code before the review is answered. Source: Opus code review (agent ac3d7db0990c2ef9e)
of commits 64564e2..9906026. Owner (2026-10-22): "we may have situations where cloud only, LAN only and both, so all shall work".

## 1. The blocker (bug C is not closed for cloud-only) and the rule for the three modes
Facts (read in the code):
- `paths.lan_destination` is REQUIRED and must be a UNC path even when `lan.enabled` is false (a cloud-only install keeps a
  placeholder, often the example `\\NAS_IP\share\FY25-26`). `paths.source_drive` is always required.
- `fy_rollover.configured_fy(source, lan)` = source leaf, else NAS leaf. `update_config_yaml` rewrites BOTH paths to the new FY
  (`_child_path(_parent_path(path), fy)`). The cloud prefix is `effective_fy_prefix(source)` = the source leaf, else the calendar FY.
- FY-1 (`models/config.py`) checks "both paths end in an FY folder or neither" only when `lan.enabled`. Cloud-only config with
  source `D:\DATA` + example NAS `...\FY25-26`: validation passes, `configured_fy` = FY25-26 (from the NAS leaf), and on 1 April the
  rollover writes `source_drive = D:/FY26-27` (an empty folder): the real data stops being backed up. Same with LAN off in the
  mismatch check (`src_fy != lan_fy` applies even when the NAS is unused).

Rule (one definition for every mode; the SOURCE path is the authority, the NAS path only matters while the NAS leg is on):
| mode | source leaf | NAS path | result |
|---|---|---|---|
| any | FY folder (`FY26-27`) | - | FY mode ON: `configured_fy` = the source leaf |
| any | no FY folder | - | FY mode OFF (no rollover, nothing is rewritten) |
| LAN on (LAN-only or both) | FY | must end in the SAME FY | else config refused (existing mismatch rule) |
| LAN on | none | must have NO FY leaf | else refused (the one-sided rule, FY-1) |
| LAN off (cloud-only) | any | ignored for FY purposes: any leaf or none, no mismatch check, never read by `configured_fy` | |
Consequences in code:
1. `fy_rollover.configured_fy(source, lan)` returns the SOURCE leaf only (the NAS argument is kept for the signature and ignored).
   `detect_rollover` follows. This alone removes the destructive case (a NAS-only leaf can no longer switch FY mode on).
2. `models/config.py`: the one-sided and mismatch checks apply only `if self.lan.enabled`; the 4-digit trap check stays for both paths.
3. `update_config_yaml`: compare-and-set uses the source leaf; `lan_destination` is rewritten ONLY when its current leaf is an FY
   folder (then to the new FY, so a re-enabled NAS later still matches); a placeholder without an FY leaf is left untouched.
4. Re-enabling the NAS later on a cloud-only install: the validator then demands the matching leaf (message tells what to fix); the
   NAS path edit is allowed by `_refuse_fy_change` because `configured_fy` (source leaf) does not change.
5. Steps that already skip when the NAS is off stay (`nas_folder`, `nas_shutdown`); the final backups run only enabled legs.
Tests (red first): cloud-only with source `D:\DATA` + NAS `...\FY25-26` -> no FY mode, `detect_rollover` False, rollover is a no-op,
`update_config_yaml` never called; cloud-only with source FY + NAS placeholder without leaf -> rollover switches the source only and
leaves the NAS path byte-identical; cloud-only with source FY + NAS different FY -> loads (NAS ignored); LAN-only/both: one-sided
refused, mismatch refused, both-same OK; LAN enabled later -> refused with the plain message; `configured_fy` table test.

## 2. Should-fix items
- **S2 RESOLVED closes unrelated rollover incidents.** Incidents are per `mode` (`last_open_incident`, `sent_in_mode_after`). Give the
  ROL-06 alert and its RESOLVED their own mode `rollover_files` (labels in `email_compose` fall back safely; add a LEG label/name
  entry if the subject needs one). Test: an open ROL-05 incident stays open after the ROL-06 RESOLVED; the next ROL-05 is still a
  follow-up in the same thread. The RESOLVED goes out the same day: also run `_resolve_unprotected_new_fy` after the switch step
  finished in the rollover run (so it is not a day late) - only when the switch is done.
- **S3 cost and junctions.** Dashboard (`compute_attention`) needs presence only: new `fy_rollover.new_fy_has_files(source, to_fy,
  placeholder)` using `guards.first_regular_file(root, SOURCE_IGNORE_NAMES | {placeholder.lower()})` (skips junctions, stops at the first
  file). The exact count is computed only in the daily alarm, by a junction-safe walk (scandir, `follow_symlinks=False`, skip
  `os.path.isjunction`). `unprotected_new_fy(config, db, count=False)`; message has a variant without a number. Tests: a junction
  inside the new folder is not counted/found; presence-only does not walk (patch `os.walk` to fail).
- **S4 picker path handling.** `list_folders`: a drive root (`D:\`) has `parent=None`; refuse UNC (`\\...`) in browse and check (the
  source is local); `check_source_parent` refuses a parent that is not absolute with a drive root (`ntpath.splitdrive` + `isabs`),
  normalises with `os.path.normpath`, and detects links on ANY component (`os.path.realpath(p)` differs from the normalised `abspath`).
  Tests per refusal (`D:` drive-relative, `\\host\share`, relative path, a parent below a junction).
- **S5 messages.** `fy_rollover.reentered_message` and the D7 (`rollover:switched_by_hand`) message must say "ask support to change
  source_drive and lan_destination in config.yaml on the server" (Settings refuses FY changes). Update the tests that assert the old words.
- **S6 early-step failure is silent.** When the early SOURCE_FOLDER step fails: WARNING alert key `rollover:<to_fy>:source_folder`
  (fault AAM-ROL-04, class failure) saying the new folder could not be created and why, once per day via the normal cooldown.
  Test: failure -> one alert event; success -> none.
- **Nit** `browseFolders`/`useFolder`: try/catch around fetch + json, show "The server did not answer properly" instead of a stuck
  "Loading...". A JS source test asserts the try/catch exists.
- **Nit** deploy note: an existing production config with a one-sided leaf and the NAS on now refuses to load - add to
  `docs/DEPLOY_RUNBOOK.md` and the readiness script's text; the trial box has both leaves (checked).

## 3. Order (each its own commit, test first, full unit suite green before the next)
1 rule + `configured_fy` + validator + `update_config_yaml` (+ tests above, fallout fixes)  2 S5 messages  3 S4 picker  4 S3 presence/count
5 S2 mode + same-day RESOLVED  6 S6 warning  7 JS nit + docs  8 full suite, Opus re-review of the diff, deploy (box idle), verify.

## 4. Questions for the reviewer
Is "source leaf is the authority, NAS path ignored when the NAS leg is off" complete - any caller of `configured_fy`/`_fy_name` on the
lan path that still misbehaves (`create_new_fy_folders`, `_complete_pending_rollover_nas_folder`, `ui` rollover panel, guards G5/G6,
reports, audit, installer/readiness scripts)? Is leaving a non-FY NAS placeholder untouched on the switch safe? Does a separate mode
`rollover_files` break the alert-class scan test, the incident closure (`_incident_closed` falls to quiet-days for unknown modes), the
email subject/labels? Anything missing for LAN-only (cloud disabled, `gcs_key_path` empty)?

## 5. Rev 2 - changes after the Opus review of rev 1
1. (BLOCKER) S2 same-day RESOLVED: `_resolve_unprotected_new_fy` takes `to_fy` explicitly (the run's config object is the pre-switch one) and is
   called after the switch step finished; test: switch in the run -> RESOLVED in the same run.
2. S6 warning uses its own key `rollover:<to_fy>:source_folder_early` (the escalation `fail()` uses `rollover:<to_fy>:source_folder`).
3. S2 mode `rollover_files`: add a `LEG_LABEL` entry in `email_compose` (subject), and a branch in `notify._incident_closed`: closed once the
   configured FY differs from the FY in the key or `unprotected_new_fy` is None (works when RESOLVED mails are switched off).
4. S4: compare `normcase(realpath(p))` with `normcase(abspath(p))`; test: folder typed in a different case is not "a link".
5. FY mode OFF (source has no FY leaf): `effective_fy_prefix` falls back to the calendar FY, so the cloud prefix and the guard baseline key change
   every 1 April (full re-upload, G2/G4 start from nothing). Existing behaviour, NOT changed here; flagged to the owner (options: fixed prefix when FY
   mode is off, or require FY mode whenever the cloud is on). Recorded in the handoff.
6. `update_config_yaml` decides "NAS path ends in an FY folder" from `txn.doc` inside the lock.
7. The 4-digit trap check is skipped for the NAS path when the NAS is off. 8. `create_new_fy_folders` has no production caller: marked legacy.
9. The one-sided / mismatch messages say "change the NAS folder under Advanced" (the paths are Advanced settings).
11. `new_fy_has_files` / count: a new FY folder that is itself a link/junction does not count.
