# Build plan: fixes from the Opus review of `rclone-engine` (2026-11-25)

Branch `rclone-engine`, worktree `C:\Users\malliath\Desktop\aam_rclone`. Binding for the builder: do not re-plan, do not add scope, do not change any interface listed here. If something below is wrong or blocked, STOP and report it; do not improvise.

All facts marked [MEASURED] were reproduced with `deploy\bin\rclone.exe` v1.74.3 during the review. All file references are the current tree.

## 0. Rules for whoever builds this (from CLAUDE.md, binding)

- Tests and code come from DIFFERENT agents. The test agent writes the tests in this plan first, from this document only, and shows each one RED on the current code (for the reason named in the row). The code agent then makes them green and may NOT edit, weaken, skip or xfail them. A test that looks wrong goes back to Opus, not to the code agent.
- One chunk = one commit. Commit with `git commit --only <explicit paths>`; never `git add -A`. Other agents share this worktree. End each message with the trailer CLAUDE.md prescribes.
- Every tunable is a config key with default and bounds in `models/config.py` AND an entry in `core/config_help.py` AND in `config.example.yaml` (same commit as the code that reads it). No literals at call sites.
- No new dependencies. No robocopy. Do not touch the live box, the NAS, or `C:\AAMBackup`. Never run `taskkill /IM rclone.exe` or kill rclone processes you did not start.
- After each chunk compare `git diff --stat` with the "Scope" line; more files or lines than scoped = stop and flag it.
- Commands (repo root):
  - interpreter: `..\aam_backup\.venv\Scripts\python.exe` with env `PYTHONPATH=.` (this worktree has no `.venv`)
  - targeted: `<py> -m pytest <files> -q -p no:cacheprovider -o addopts="" --timeout=600`
  - fast gate: `<py> -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600 -m "not slow"`
  - lab file: `<py> -m pytest audit_lab/<file> -q -s --timeout=1800 -p no:cacheprovider -o addopts=""` (needs a clean `PREFECT_HOME`, see SESSION_HANDOFF)
- Known reds that are NOT yours: `tests/test_review2_driver.py` (2 tests) fails only on a dirty working tree; it passes once everything is committed. `test_v113_round3` needs a `config.yaml` copied from `config.example.yaml`.
- Record every finding or decision you make while building in `SESSION_HANDOFF.md` (top block) and commit it.

## 1. Order and dependencies

| Order | Chunk | Severity | Depends on |
|---|---|---|---|
| 1 | RF-1 versions purge by folder date | HIGH | none |
| 2 | RF-2 stall watchdog: real progress signal, off for silent tools | HIGH | none |
| 3 | RF-3 copy-only mail tells the truth (both legs) | MEDIUM | none |
| 4 | RF-4 copy-only free-space counts renamed files | MEDIUM | none |
| 5 | RF-5 rclone version check in the health check | MEDIUM | none |
| 6 | RF-6 doc and wording fixes | LOW | RF-1..RF-5 |
| 7 | RF-7 gate: full fast loop, slow file, lab files, Opus mutation review | - | all |

Not built (decisions, see section 9): case-only renames, nightly hash cache caveat, arrival-check excuse, free-space double count.

---

## RF-1 Versions purge must use the run-folder date, not file mtime (HIGH)

### Problem [MEASURED]
`core/lan_versions.py` `purge_old_versions` runs `rclone delete <versions_root> --min-age <N>d`. `--min-age` tests each file's modification time. A file moved into `--backup-dir` keeps its ORIGINAL mtime. Reproduced: a file last edited 2025-01-01 was deleted on the source, moved to `ver/<run>/old.txt` (mtime 2025-01-01), and `rclone delete ver --min-age 91d` removed it immediately. The purge runs at the end of the same `LAN_COMPLETE` run (`flow.py` around line 2319), so any deleted/replaced file that was not edited in the last 91 days gets no retention at all. The correct helper `folders_to_purge()` (uses the date in the run-folder name) already exists in the same file and is never called. `tests/test_b6_versions_purge.py::test_v9_purge_old_versions_runs_rclone_delete_min_age_and_rmdirs` pins the wrong behaviour and the lab test `audit_lab/test_w26_versions_lab.py` only uses fresh files.

### Required behaviour (exact)
`purge_old_versions(lan_destination, versions_folder, retention_days, *, artefacts_dir=None, timeout_s=None, today=None) -> dict`:
1. `versions_root = resolve_versions_root(lan_destination, versions_folder)` (unchanged). If it is not a directory: return `{"ok": True, "versions_root": ..., "skipped": "versions folder absent", "purged": [], "failed": []}`.
2. `today` = the argument if given (a `datetime.date`), else today's date in IST (`core.time_utils.IST`, same zone `backup_dir_for_run` uses).
3. `due = folders_to_purge(versions_root, retention_days, today)`. Only immediate child directories whose name matches `_RUN_FOLDER_DATE_RE` and whose age in days is `>= retention_days` are due. Anything else (other names, files, the root itself) is NEVER touched.
4. For each due folder run exactly one command: `rclone purge "<folder>"` (step name `rclone-versions-purge`). Before running, assert the folder's resolved parent equals the resolved `versions_root` (else skip it and list it in `failed` with reason `outside versions root`). One folder failing (exit != 0, timeout, spawn error) does not stop the others.
5. No `rclone delete`, no `--min-age`, no `rmdirs` any more.
6. Return `{"ok": <no failures>, "versions_root": str, "purged": [folder names], "failed": [{"folder": name, "error": text}]}`.
7. `timeout_s` default = new config key `lan.versions_purge_timeout_seconds` (default 1800, bounds 60..14400; `config_help` label "Time allowed for purging old NAS versions", text "Each old versions folder is removed within this time; a slower purge is reported and retried next night."). `flow.py` passes `config.lan.versions_purge_timeout_seconds`. The literal `1800` in the function signature is removed.
8. A purge failure must never change the run status (`flow.py` already wraps the call in try/except; keep that, and log the returned `failed` list at WARNING).

### Files and scope
`core/lan_versions.py` (~50 lines), `models/config.py` (+1 field), `core/config_help.py` (+1 entry), `config.example.yaml` (+1 key), `flow.py` (pass the new timeout, ~3 lines). Nothing else.

### Tests (test agent, red first)
File `tests/test_rf1_versions_purge.py` (new) and one new test in `audit_lab/test_w26_versions_lab.py`. The existing `test_b6` test that asserts `--min-age`/`rmdirs` is REPLACED by the test agent under Opus (it pins the bug); the other tests in that file stay.

| ID | Scenario | Expected |
|---|---|---|
| RF1-1 | versions root with folders `2026-08-01_2101_FY26-27` (116 days old), `2026-09-02_2101_FY26-27` (exactly 91 days old), `2026-11-20_2101_FY26-27` (5 days old), `not_a_run_folder`, and a stray file `notes.txt`; `today=2026-12-02`; fake `run_tool` | `purge` command issued for exactly the first two folders; none for the others; result `purged` lists the two names; `ok` True |
| RF1-2 | any call | argv never contains `delete`, `--min-age`, `rmdirs` |
| RF1-3 | one purge command exits 1, the second exits 0 | second folder still purged; result `ok` False; `failed` has one entry with the folder name |
| RF1-4 | `retention_days=91`, folder exactly 90 days old | NOT purged (boundary) |
| RF1-5 | versions root absent | `skipped == "versions folder absent"`, no command |
| RF1-6 | a due folder name that resolves outside the root (inject via a monkeypatched `folders_to_purge`) | skipped, listed in `failed`, no command for it |
| RF1-7 | config | `lan.versions_purge_timeout_seconds` default 1800, 59 rejected, 60 and 14400 accepted, 14401 rejected; `config_help` has the key; `config.example.yaml` has it |
| RF1-8 | `flow.py` wiring | with `versions_enabled` true and a `LAN_COMPLETE` run, `purge_old_versions` is called with `timeout_s == config.lan.versions_purge_timeout_seconds`; a raised exception does not change the status |
| RF1-L1 (lab, REAL rclone) | on the loopback share: source file `old.txt` with mtime 2025-01-01, mirror it, delete it on the source, run the real LAN mirror with `backup_dir` = today's run folder, then call `purge_old_versions(..., retention_days=91)` | `old.txt` STILL exists in the versions folder with its content (md5 equal to the original) |
| RF1-L2 (lab, REAL rclone) | same share, additionally create a folder named for a date 100 days ago containing a file, then purge | that folder is gone, today's folder is intact |

### Acceptance
- RF1-1..RF1-8 green; RF1-L1 and RF1-L2 green on the real binary; all of them RED on the pre-change code (RF1-L1 red because `old.txt` is deleted).
- `git grep -n "min-age\|rmdirs" core flow.py` returns nothing for versions code.
- `tests/test_b6_versions_purge.py`, `tests/test_b7_versions_space.py`, `tests/test_b2_versions_config.py`, `audit_lab/test_w26_versions_lab.py` all green.
- Opus mutation check (must turn a test red): (a) re-introduce `--min-age`; (b) change `>=` to `>` in the age comparison; (c) purge a folder that does not match the name pattern; (d) drop the parent-equals-root assertion.

---

## RF-2 Stall watchdog (S6): use rclone's own progress, and do not run on silent tools (HIGH)

### Problems [MEASURED]
A. rclone writes a stats block every `--stats` interval (default 1 min) even when nothing moves. With a throttled copy, `bytes` stayed at 32768 across 5 stats entries in 12 s while the log file kept growing. `core/tool_runner.py` `_wait_polling` treats log growth as progress (`_watch_bytes` = file sizes), so a rclone hung on an SMB read never trips the watchdog.
B. `rclone check` is silent until it ends: with `--stats 1s` and no `-v`, nothing was written for the whole 11 s run except the final two NOTICE lines. `run_tool` applies the stall rule to EVERY tool (`stall_timeout_s` default None -> `tools.stall_timeout_seconds`, 1800 in both `config.yaml` and `config.example.yaml`; no caller passes it). Any `rclone check` that runs longer than 30 minutes with no output is killed as "stalled": the weekly hash audit (`core/integrity.py`, one checker, hours), the cloud verify (`core/cloud_verify.py`), the LAN arrival and hash checks (`core/lan_sync.py`), `rclone size`/`lsjson` reports. The weekly audit would be reported incomplete every week on a large tree.

### Required behaviour (exact, in `core/tool_runner.py`)
1. Stall detection runs ONLY when `run_tool` was given a `log_path` (the tool writes its own JSON progress log: cloud sync, cloud dry-run, LAN sync, LAN plan). When `log_path is None` and `stall_timeout_s is None`, stall detection is OFF for that call (only the hard `timeout_s` applies). An explicit `stall_timeout_s > 0` with no `log_path` keeps the old behaviour (watch stdout/stderr byte growth), so callers and tests can still force it.
2. For a call with `log_path`, progress is decided from the log, not from its size. Each poll reads only the bytes appended since the last poll (keep a byte offset and carry a partial last line to the next poll) and splits them into lines. A line is parsed as JSON; unparsable lines are ignored. Then:
   - a line with a `stats` object updates the "stats fingerprint" = the tuple of these keys of that object, missing keys as `None`: `bytes`, `transfers`, `checks`, `deletes`, `deletedDirs`, `renames`, `listed`, `errors`, `serverSideCopies`, `serverSideMoves`. If the new fingerprint differs from the previous one, that is progress.
   - a line with `level == "info"` that is not a stats line and has a non-empty string `object` field (a per-file event such as "Copied (new)", "Deleted", "Moved into backup dir") is progress.
   - everything else (NOTICE/WARNING/ERROR lines, repeated identical stats, lines without `object`) is NOT progress.
3. The existing `tools.stall_timeout_seconds` key, its bounds (0 or 300..14400), its default (1800) and its meaning "no progress for this long, 0 = off" stay. Update its `config_help` text to: "Stops rclone sync and dry-run if its own progress counters do not change for this long (0 = off, 300-14400). Check, size and report tools are not stopped by this; they have their own time limits." No new key.
4. A stalled tool is killed exactly as today (`_kill_tree`, `stalled=True`, `describe_failure`). `lan_sync.run_lan_mirror` / `cloud_sync` handling of `result.stalled` is unchanged.
5. No caller changes are needed: the silent callers already pass no `log_path`.

### Files and scope
`core/tool_runner.py` (~70 lines), `core/config_help.py` (1 text). Tests only elsewhere.

### Tests (test agent, red first)
File `tests/test_rf2_stall_progress.py` (new). Use small Python child scripts (run with `sys.executable`) as the tool, `stall_timeout_s` passed explicitly as 1.5 and a poll of 0.2 s via the existing poll-seconds mechanism; every test must finish in under 15 s.

| ID | Child behaviour | Call | Expected |
|---|---|---|---|
| RF2-1 | writes the SAME JSON stats line (identical counters) to the log file every 0.2 s for 10 s | `run_tool(..., log_path=..., stall_timeout_s=1.5, timeout_s=30)` | `stalled True`, killed in < 6 s, exit code not used |
| RF2-2 | writes a stats line whose `bytes` grows every 0.5 s for 4 s, then exits 0 | same | `stalled False`, exit 0 |
| RF2-3 | writes only info lines with `object` ("Copied (new)") every 0.5 s for 4 s, no stats | same | `stalled False` |
| RF2-4 | writes only NOTICE/ERROR lines every 0.2 s for 10 s | same | `stalled True` |
| RF2-5 | writes NOTHING for 4 s then exits 0, NO `log_path`, `stall_timeout_s=None` and the configured default patched to 1.5 | `run_tool(..., log_path=None)` | `stalled False`, exit 0 (silent tool is not killed) |
| RF2-6 | writes nothing to stdout/stderr for 4 s, NO `log_path`, explicit `stall_timeout_s=1.5` | | `stalled True` (explicit override kept) |
| RF2-7 | one stats line split across two writes (partial line, then rest) with changing `bytes` | | `stalled False` (partial line carried, parsed once) |
| RF2-8 | log with a garbage (non-JSON) line followed by valid changing stats | | no exception, `stalled False` |
| RF2-9 | `stall_timeout_s=0` with a silent child | | never stalled |
| RF2-10 | fingerprint keys: a stats line that changes only `checks` (listing/checking phase) | | counts as progress |
| RF2-11 | `config_help["tools.stall_timeout_seconds"]` text equals the string in item 3 | | |
| RF2-L (lab, REAL rclone) | `rclone sync` of one 50 MB file with `--bwlimit 1k --stats 1s --use-json-log --log-file`, `stall_timeout_s=5`, hard timeout 60 | | `stalled True` within ~15 s (identical stats); the process tree is gone |

Also keep green: `tests/test_c6_stall_and_c9_c10.py`, `tests/test_a6_tool_runner_interrupt.py`, `tests/test_cloud_reporter_tool_runner.py`, `audit_lab/test_w22_hardkill.py`, `audit_lab/test_w23_cancel.py`.

### Acceptance
- RF2-1..RF2-11 and RF2-L green and red on the old code (RF2-1, RF2-4, RF2-5 red; RF2-L red because the old code never stalls).
- `git grep -n "stall_timeout_s" core flow.py` shows no new call-site literals.
- Opus mutation check: (a) count any appended byte as progress again (RF2-1 must go red); (b) make silent no-`log_path` calls stall again (RF2-5 red); (c) count NOTICE lines as progress (RF2-4 red); (d) drop the partial-line carry (RF2-7 red).
- Shadow-trial note (do not build): during `rclone-lan-plan` with `--track-renames`, rclone may hash dest-only files over SMB with no counter change for a while; step A must record the longest quiet period so `tools.stall_timeout_seconds` is set >= 3x that.

---

## RF-3 The "blocked" mail must say what the copy-only run did (MEDIUM, both legs)

### Problem
On a G3-only block the pipeline runs `rclone copy` (copy-only) and then re-raises the block. `flow.py` (LAN ~line 2152-2166, cloud ~line 1751-1768) stores `gb.record["copy_only"]` but nothing reads it, and `_open_block_request` (flow.py ~1187) decides its sentence from `sync_attempted=bool(sync_state.get("attempts"))`, which the copy-only branch never sets. Result: the mail says "Nothing was copied or deleted." although files were copied. Owner rule: DB, reports and mails show the actual truth.

### Required behaviour (exact)
1. In both copy-only branches set `gb.record["copy_only"] = {"attempted": True, "copied": None, "bytes": None, "deletes_held": <planned_deletes>, "error": None}` BEFORE calling `sync(...)`. After success set `copied`/`bytes` from `stats.transfers`/`stats.bytes` (ints, 0 if absent). In the `except` set `error = str(copy_exc)[:300]` and leave `copied`/`bytes` as `None` (unknown).
2. `_open_block_request(...)` computes `sync_attempted = <existing value> or bool(guard_record.get("copy_only", {}).get("attempted"))` and passes it on. Do this inside `_open_block_request` itself (read `guard_record`), so no call site changes except none. The sentence for `sync_attempted` stays `statuses.BLOCKED_AFTER_CHANGES_NOTE`.
3. New pure function `statuses.copy_only_note(copy_only: dict | None) -> str`, returning "" when `copy_only` is falsy or not attempted, else exactly one of:
   - success: `Copy-only run: {copied} file(s) ({mb:.2f} MB) were copied to the backup; no deletions were made, {held} planned deletion(s) are held until you approve.`
   - error: `Copy-only run was attempted but did not finish ({error}); the number of files copied is unknown; no deletions were made.`
   (`mb = bytes / 1048576`; `held` from `deletes_held`, 0 if missing.)
   `_open_block_request` appends it as its own paragraph after the "Some files may already have been changed..." sentence.
4. `files_copied`/`bytes_copied` already reach the run row; do not change that. Do not touch the dashboard text (`statuses.describe`).

### Files and scope
`flow.py` (~25 lines), `core/statuses.py` (+~20 lines). No config.

### Tests (test agent, red first)
File `tests/test_rf3_copy_only_truth.py` (new), built on the existing helpers used by `tests/test_a3_blocked_text_after_changes.py` and `tests/test_b4_copy_only.py`.

| ID | Scenario | Expected |
|---|---|---|
| RF3-1 | LAN, G3-only block, copy returns stats transfers=7, bytes=2097152 | block mail contains `Some files may already have been changed` AND `Copy-only run: 7 file(s) (2.00 MB)`; does NOT contain `Nothing was copied or deleted` |
| RF3-2 | cloud, same | same assertions |
| RF3-3 | LAN, copy raises SyncFailed | mail contains `Copy-only run was attempted but did not finish` and `unknown`; not `Nothing was copied` |
| RF3-4 | G3-only block where the copy is skipped because `is_g3_only_block` is False (e.g. G1 also blocks) | text unchanged from today: `Nothing was copied or deleted.` |
| RF3-5 | `statuses.copy_only_note` unit | returns "" for `None`, `{}`, `{"attempted": False}`; exact strings for the two cases above (golden strings in the test) |
| RF3-6 | run row | `files_copied == 7`, `bytes_copied == 2097152`, status `LAN_BLOCKED` |
| RF3-7 | guard record JSON in `extended_metrics` | contains `copy_only` with `attempted` true |

### Acceptance
RF3-1..RF3-7 green and RF3-1..RF3-3 red on old code; `tests/test_a3_blocked_text_after_changes.py`, `tests/test_b4_copy_only.py`, `tests/test_b5_ladder_and_dashboard.py`, `tests/test_flow_orchestration.py` green. Mutation: remove the `attempted` OR from item 2 (RF3-1 red).

---

## RF-4 Copy-only free-space check must count renamed files (MEDIUM, LAN)

### Problem [MEASURED]
The dry-run runs with `--track-renames`, so a renamed file is planned as `"skipped":"move to <new>"` with `"size":N` and is NOT counted in `planned_bytes` (sum of `skipped == "copy"` sizes). The copy-only run is `rclone copy`, which has no `--track-renames` (builder: `mode == "sync"` only), so every renamed file is copied in full. After a big folder rename, `lan_access_task`'s free-space check under-counts and the NAS can fill up mid-copy.

### Required behaviour (exact)
1. `core/cloud_sync.py`: new pure function `planned_move_bytes(log_text) -> int | None`: sum of `size` over entries whose `skipped` starts with `"move to "`; `None` if any such entry lacks an int `size` (same fail-closed rule as `planned_copy_bytes`); `0` when there are none.
2. `lan_sync.plan_lan_mirror` result gains `planned_move_bytes` (same value, `None` allowed).
3. `flow.lan_access_task(config, plan, run_id, copy_only: bool = False)`: when `copy_only` is true, `bytes_needed = planned_bytes + planned_move_bytes`; if either is `None` then `bytes_needed = None` (the existing fail-closed path of `check_destination_free_space`). When false, unchanged.
4. The copy-only call site in `_run_lan_pipeline` passes `copy_only=True` (only that call; the normal and `remeasure` calls do not).

### Files and scope
`core/cloud_sync.py` (+~15), `core/lan_sync.py` (+2), `flow.py` (~6). No config.

### Tests (test agent, red first)
File `tests/test_rf4_copy_only_space.py` (new) plus one real-rclone row in `audit_lab/test_w25_rclone_lan.py`.

| ID | Scenario | Expected |
|---|---|---|
| RF4-1 | `planned_move_bytes` on a log with two `move to` entries (size 20 and 30) and one `copy` entry (size 5) | 50 |
| RF4-2 | one `move to` entry without `size` | `None` |
| RF4-3 | no move entries | 0 |
| RF4-4 | `plan_lan_mirror` with a fake dry-run log containing moves | result has `planned_move_bytes` equal to the sum |
| RF4-5 | `lan_access_task(..., copy_only=True)` with `planned_bytes=100`, `planned_move_bytes=900` | `check_destination_free_space` called with `bytes_needed == 1000` |
| RF4-6 | same with `copy_only=False` | `bytes_needed == 100` |
| RF4-7 | `copy_only=True` and `planned_move_bytes=None` | `bytes_needed is None` |
| RF4-8 | copy-only branch of `_run_lan_pipeline` | `lan_access_task` received `copy_only=True`; normal path did not |
| RF4-L (lab, REAL rclone) | mirror a folder, rename it on the source, run the real dry-run | `plan["planned_bytes"] == 0` and `plan["planned_move_bytes"] == <total bytes of the folder>` |

### Acceptance
All green, RF4-1, 4, 5, 7, 8 red on old code; `tests/test_p1_*` LAN plan/guard tests and `audit_lab/test_w25_rclone_lan.py` green.

---

## RF-5 Check the rclone version before trusting its output (MEDIUM)

### Problem
Parsers depend on rclone 1.74.x text and JSON ("Copied (", "partial file rename failed", "N matching files", "N differences found", stats keys). `core/health.py` only checks that a binary exists. A different rclone could silently mis-parse into a wrong COMPLETE or a wrong fail.

### Required behaviour (exact)
1. Two new keys in `ToolsConfig`: `rclone_expected_version: str = "1.74"` (a `major.minor` prefix, validated by regex `^\d+\.\d+$`) and `rclone_version_policy: Literal["block", "warn"] = "block"`. `config_help` labels: "rclone version the backup was tested with" / "The backup program only trusts rclone output of this major.minor version (for example 1.74)." and "What to do if rclone is a different version" / "block = do not run the backup; warn = run but send a warning." Add both to `config.example.yaml`.
2. `core/health.py`: new `rclone_version_fault()` helper that runs `rclone version` through `run_tool` (step `rclone-version`, `timeout_s=30`, `stall_timeout_s=0`), parses the first line with regex `^rclone v(\d+\.\d+)\.\d+`, and compares the `major.minor` to `tools.rclone_expected_version`. The result is cached per resolved binary path for the process lifetime. Patch level differences are allowed.
3. `health_check_task` (both the cloud and the lan branch, right after `check_binary_exists("rclone")`): if the version cannot be read or does not match: policy `block` raises `coded(HealthError("rclone version <found> is not the tested <expected>.x - ..."), "AAM-SYS-04")`; policy `warn` logs a WARNING and calls `notify` once per day with an `AlertEvent` kind `WARNING`, key `rclone_version`, fault code `AAM-SYS-04`. Use the existing notify cooldown; do not invent a new alert path.
4. `rclone version` must not contact the network (it is run without `--check`).
5. The bundled `deploy\bin\rclone.exe` is 1.74.3, so the default passes on the box.

### Files and scope
`core/health.py` (~40), `models/config.py` (+2 fields), `core/config_help.py`, `config.example.yaml`. Also update `deploy/04_check_readiness.ps1` ONLY if it already prints the rclone version (then print the check result); otherwise do not touch deploy scripts.

### Tests (test agent, red first)
File `tests/test_rf5_rclone_version.py` (new); fake `run_tool`.

| ID | Scenario | Expected |
|---|---|---|
| RF5-1 | output `rclone v1.74.3` / expected `1.74` | passes |
| RF5-2 | `rclone v1.75.0` / `1.74`, policy block | `HealthError`, fault `AAM-SYS-04`, message names found and expected |
| RF5-3 | same, policy warn | no raise; one WARNING alert recorded; second call in the same day does not alert again |
| RF5-4 | garbage output / exit 1 / timeout | treated as mismatch (block raises, warn warns) |
| RF5-5 | cache | two checks of the same binary run `run_tool` once |
| RF5-6 | config | `rclone_expected_version` rejects `"1"`, `"1.74.3"`, `""`; accepts `"1.74"`; policy rejects `"ignore"`; both keys in `config_help` and `config.example.yaml` |
| RF5-7 | `health_check_task` | version check runs for modes `cloud`, `lan`, `all`; not for an invalid mode |

### Acceptance
RF5-1..RF5-7 green; RF5-2, 3, 4, 7 red on old code; `tests/test_health*.py`, `tests/test_m4_config_example*`, `tests/test_config_validation.py`, `tests/test_flow_orchestration.py` green (adjust nothing in them; if they need the new keys in a fixture, that is a test-agent edit under Opus).

---

## RF-6 Doc and wording fixes (LOW)

Doc-only, one commit, no code. Each edit must be exactly this:
1. `core/lan_sync.py` docstring of `check_lan_transfers_hash`: add the sentence "Caveat: the read-back may be served from the NAS cache or from this server's own SMB client cache, so it proves the SMB path, not the NAS disk surface; the weekly `integrity.audit_lan` stays the proof." (docstring only).
2. `docs/ARCHITECTURE.md` section on the nightly hash check and `docs/PLAN_P1_RCLONE_LAN_2026-11-25.md` section 2.1 caveat: same sentence.
3. `docs/PLAN_P1_RCLONE_LAN_2026-11-25.md`: add one line under "Deferred" saying the versions purge now uses the run-folder date (RF-1) and S6 uses rclone stats (RF-2).
4. `SESSION_HANDOFF.md`: new top block listing RF-1..RF-7 with commit ids and the decisions of section 9.

Acceptance: `git diff --stat` shows only the files above; the code diff of `core/lan_sync.py` is docstring-only (`git diff -w` shows no non-comment line change).

---

## RF-7 Gate (Opus, after RF-1..RF-6)

1. Commit everything first (a dirty tree makes `tests/test_review2_driver.py` fail).
2. Fast loop: `3764` passed baseline plus the new tests; zero new failures; the known `test_v113_round3` reds only if `config.yaml` is missing (copy `config.example.yaml` first).
3. Slow calendar file: `-m slow`, 9 passed.
4. Lab files, one run each, in this order: `audit_lab/test_w26_versions_lab.py`, `test_w25_rclone_lan.py`, `test_w22_hardkill.py`, `test_w23_cancel.py`, `test_w4_pipeline.py`, `test_fy_overlap_lab.py`, `test_w6b_rollover_nas_off.py`. Clean `PREFECT_HOME`. Not the full audit lab.
5. Robocopy grep: `git grep -n -i robocopy -- core flow.py models` still shows only the 4 allow-listed hits.
6. Opus reviews `git diff rclone-engine~N..` against the Scope lines, runs the mutation lists of RF-1, RF-2, RF-3, RF-5 and confirms no assertion was removed or weakened and nothing was skipped.
7. `ruff check` on the touched files: no NEW findings (existing backlog is not a gate).

Acceptance of the whole plan: items 2-7 pass, handoff updated, owner informed. Only then does the `rclone-rc1` tag go to the owner for a decision.

---

## 8. Experiments to add to the shadow trial (no code; owner at the NAS; extends `docs/SHADOW_TRIAL_2026-11-25.md`)

| ID | Step | What to do | Pass criterion |
|---|---|---|---|
| E1 | B | Versions with an old file: delete on the source a file whose mtime is > 91 days old, run a night, run the purge | the version is still in `<share>\AAM_SHADOW_TEST_versions\` (needs RF-1) |
| E2 | B | SMB cache: copy a file to the NAS, change one byte of it ON the NAS (ssh), run `rclone check` hash step within 60 s | the hash check reports a difference. If it does not, record that the nightly hash proves only the SMB path and rely on the weekly audit |
| E3 | cable pull | Hold the outage > `tools.stall_timeout_seconds` | S6 kills rclone within threshold + one poll (needs RF-2); also confirm a 40-minute silent `rclone check` (size-only on the real tree) is NOT killed |
| E4 | B | Copy-only on a block that includes a folder rename | mail text matches RF-3 wording; free-space number includes the renamed bytes (RF-4) |
| E5 | B | Case-only rename of a file and of a folder on the source | record whether the NAS name changes; compare `--fix-case`; owner decides (section 9) |
| E6 | B | Run with a different rclone version | the run is refused or warned per `tools.rclone_version_policy` (RF-5) |
| E7 | A | Record the longest quiet period (no change in stats counters) of `rclone-lan-plan` with `--track-renames` | feeds `tools.stall_timeout_seconds` (>= 3x) |

## 9. Decisions recorded, no code now (owner may reopen)

- **Case-only renames (`--ignore-case`) [MEASURED]:** after `Case.txt` -> `CASE.txt` on the source, the NAS keeps `Case.txt`, exit 0, and the arrival check (also `--ignore-case`) stays green. Harmless for accounting data; restore keeps the old casing. Decision after E5.
- **Nightly hash cache caveat:** documented in RF-6; weekly audit is the proof.
- **Arrival check "changed during backup" excuse:** keep. A missing file also touched during the run is excused; failed copies already produce `LAN_PARTIAL` first.
- **Free-space double count with `--backup-dir`:** `lan_access_task` adds `planned_versions_bytes` although a move on the same share needs no new space. Kept as a conservative choice (the versions folder may be on another NAS volume); revisit if false free-space failures appear.
- **Junctions/symlinks in the source** are skipped silently on both legs (exit 0, NOTICE "Can't follow symlink") [MEASURED]. Record any in shadow step A.
- **Cloud nightly verify is `--size-only`:** a same-size content change is caught by the sync's mtime rule and the weekly hash audit, not nightly. Accepted.
- **Only exit 5 is retried at flow level:** an SMB drop mid-sync probably ends `LAN_PARTIAL` (exit 1) and the next night repairs it. Confirm in the cable-pull test; do not change the retry rule without owner go.
