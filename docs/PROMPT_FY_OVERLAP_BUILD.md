# Build prompt - FY overlap (for a new Claude Code session, main model Sonnet)

Paste into the new session: **"Read docs/PROMPT_FY_OVERLAP_BUILD.md and follow it exactly. This is the owner's go."**

---

## 0. Who you are and what this is

You are the main builder (Sonnet) for the AAM backup automation repo at `C:\Users\malliath\Desktop\aam_backup`,
branch `mvp-hardening`, Windows 11 PC (PowerShell primary, Git Bash available). The product runs on a Windows
Server 2016 trial box (`ssh innoviztawinserver2016`, code `C:\AAMBackup`, runtime `C:\BackupAgent`).

The owner has approved a FINAL plan and a FINAL task list. **This prompt is the owner's go to build them end to end.**
Do not re-plan. Do not add scope.

## 1. Read first (in this order, fully)

1. `CLAUDE.md` (project rules, commands, gotchas; item 6 of "Start here").
2. `SESSION_HANDOFF.md` - the top block (2026-11-20).
3. `docs/PLAN_FY_OVERLAP_2026-11-20.md` - the spec (rev 4, FINAL). Decisions D-1..D-17, definitions §3,
   behaviour §4, changes §5, work packages §6, acceptance tests §7, accepted risks §8.
4. `docs/TASKS_FY_OVERLAP_2026-11-20.md` - the build order OV-0.1 .. OV-12.1 and the working rules at its top.
   **Follow it task by task.**
5. `docs/AUDIT_TOOLING_2026-11-20.md` - for WP0 (versions, certifi fix, httpx2, keep Prefect).
6. `docs/ARCHITECTURE.md` - only the sections a task touches.

The requirement in one line: each FY folder is backed up for 15 months - its own 12 plus January-March before it,
when it is backed up next to the previous FY's folder; the existing pipeline is reused once per folder.

## 2. Model policy (owner instruction - follow it)

| Who | Use for | Never for |
|---|---|---|
| **You (Sonnet, main)** | implementing every task, writing tests, commits, deciding | delegating a decision the plan already made |
| **Haiku subagents** (`model: "haiku"`) | read-only chores: finding call sites / text search across many files, summarising long files or logs, running the unit suite or a test file and reporting only the failures, reading audit-lab `REPORT.md`, checking PyPI versions, listing what a diff touches | editing tracked files, design decisions, anything on the trial box |
| **Sonnet subagents** (`model: "sonnet"`) | clearing well-defined, isolated work in parallel when it does not touch files you are editing: e.g. fixing a batch of test fallout after the WP0 upgrade, updating test fixtures for a renamed DB method, drafting a doc section | the same file another agent or you are editing; anything high-risk listed below |
| **Opus subagents** (`model: "opus"`) | **verification only**: (a) a hostile review of the diff at the END of every WP before its last commit (`git diff <wp start>..HEAD`), (b) a review BEFORE committing the high-risk tasks OV-1.1 (schema), OV-1.3 (run history scoping), OV-3.1 + OV-3.4 (slot + NAS power), OV-4.3 (approvals, the R-1 blocker), OV-5.1 (guards), OV-11.4 (trial-box deploy steps) | writing the code |

Rules for agents: give each a precise brief with file paths and what to return; agents are read-only unless the
brief says otherwise; you verify every claim an agent makes against the code before acting on it (agent output is
not proof); never have two agents edit the same file; prefer one agent call with a full brief over many small ones.

## 3. Working rules (from CLAUDE.md + the task list - non-negotiable)

- Reliability over speed; no guesses. Fail closed. Ask the owner when an action could cause a wrong result or data loss.
- **GitNexus before editing a symbol:** `node .gitnexus/run.cjs impact "<symbol>" --direction upstream --repo .`;
  HIGH / CRITICAL / UNKNOWN goes into the commit message; confirm UNKNOWN with a text search. Re-index when stale:
  `node .gitnexus/run.cjs analyze --index-only`.
- **Test first:** each task's regression test fails on the old code, then passes.
- **Every tunable is a config key** (pydantic default + bounds, `config_help.py` label) in the same commit.
- Shared building blocks only (`core/statuses.py`, `core/tool_runner.run_tool`, `core/run_artefacts.py`,
  `ManifestDB.from_config`, `core/notify.py`); do not re-implement them.
- Do not monkeypatch a Prefect `@task` with a plain function: patch `task.fn`, or a `MagicMock` whose `.with_options`
  returns the fake. Tests never use the real `config.yaml`.
- **Git Bash heredocs mangle backslashes** - write code / commit messages containing `\` with the Write tool or a file
  (`git commit -F <file>`); check diffs for stray `\n`.
- **Never commit the owner's unrelated uncommitted work**: `core/restore.py`, `core/upload.py`, `restore.py`,
  `upload_fy.py`, `tests/test_restore_engine.py`, `tests/test_upload_engine.py`, `docs/RESTORE_CLOUD.md`,
  `tools/server_health/README.md` and untracked files you did not create. Stage files by name, never `git add -A` /
  `git add .`. If a task (OV-10.1) must touch those files, stop and ask the owner first.

## 4. Commands

```
# full unit suite (~3 min; run in the background, have a Haiku agent summarise failures if long)
.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600
# one file / one test
.venv\Scripts\python.exe -m pytest tests/test_x.py -q -p no:cacheprovider -o addopts=""
# audit lab (~20-60 min, background; needs a CLEAN PREFECT_HOME or it hangs)
powershell -ExecutionPolicy Bypass -File tools\run_audit_lab.ps1      # writes agent_runs\audit_lab_<stamp>\REPORT.md
# lint (backlog exists; no new findings in changed lines)
.venv\Scripts\ruff.exe check <changed files>
# graph check before every commit
node .gitnexus/run.cjs detect-changes --scope all --repo .
```

Known intentional audit-lab red: `test_w16_schedule_collisions_across_year`. `audit_lab/test_w7_deployed.py` is
excluded. While the audit lab runs, do not edit modules it imports.

## 5. How to work through the tasks

For each task in `docs/TASKS_FY_OVERLAP_2026-11-20.md`, in order:
1. Re-read the task and the plan section it cites; read the real code (line numbers in the plan are evidence and may
   have drifted).
2. GitNexus impact for each symbol you will change.
3. Write the failing test(s); run them; see them fail for the right reason.
4. Implement the smallest change that satisfies the plan.
5. Run the task's tests + the WP's test files; for high-risk tasks, the Opus review (section 2) before committing.
6. `detect-changes`; commit (task IDs + plan items in the message, ending with
   `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`); tick the task's box in the task list in the same commit.
7. At the end of each WP: full unit suite green, Opus hostile review of the WP diff, fix what it confirms, then push
   (section 6). After WP0, WP3 and WP11 also run the audit lab.

Anything the plan does not cover: write it under "Found while building" in the task list, finish what you can
without it, and **ask the owner** - do not build it on your own judgment.

## 6. Pushing (the GitHub repo `Shawn2099/AAM_BACKUP_V1` is PUBLIC)

Push only `mvp-hardening` to `origin`, at the end of each green WP, never force-push. **Before every push** run a
secrets check over everything that would be pushed (`git log -p origin/mvp-hardening..HEAD`): passwords, SMTP
credentials, GCS key JSON, tokens, `config.yaml` with real values, IP + account + password combinations. If anything
is found, do NOT push; tell the owner. (A NAS/service-account password is already in the pushed history of
`SESSION_HANDOFF.md`; the owner has been told to rotate it - do not copy it anywhere.)

## 7. Trial box (owner: full control for this work)

- Allowed: deploy, restart services, the folder-creation test (OV-11.3), renaming `manifest.db` + `-wal` / `-shm`
  for the fresh v6 DB (OV-11.4), the rehearsal (OV-11.5).
- Before anything that stops a backup or touches backup data: check no backup is running (Prefect slot `aam-backup`
  = 0, no `backup.lock`), tell the owner in your message what you are about to do, then do it.
- Deploy = git bundle piped over ssh (recipe in `SESSION_HANDOFF.md`); remote PowerShell uses `;` not `&&`,
  `[Console]::OutputEncoding=[Text.Encoding]::UTF8`; no scp - pipe scripts in.
- Service order: stop watchdog → agent; start agent → watchdog. Copy the DB before any change. Rollback =
  `git reset --keep <previous sha>` + the same restart order.
- Box schedules: LAN 21:01, cloud 02:01, audit Sunday 08:00, rollover check 06:00 IST. Deploy outside those windows.

## 8. Done means

- Every task in the list ticked; plan §7 acceptance tests 1-9 pass; full unit suite green; audit lab green except
  the known red; Opus review of the last WP has no open confirmed finding.
- Trial box runs the new code with the fresh v6 DB; the next LAN and cloud runs finish green; the rehearsal proved
  two cards, the INFO mail and both folders in the cloud and on the NAS, and was cleaned up.
- `SESSION_HANDOFF.md` top block updated (what was built, deployed sha, what is open); `docs/ARCHITECTURE.md` updated.
- Final message to the owner: what was built per WP, test / lab results, deployed sha, anything in "Found while
  building", and the owner actions left (e.g. OV-12.1 lifecycle review).

At the end of every session (even mid-way): update the `SESSION_HANDOFF.md` top block with the last ticked task and
the next one, so the following session can continue from the task list.
