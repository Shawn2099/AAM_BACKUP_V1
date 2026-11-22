# Release break-it session - 2026-11-22 (READ THIS FIRST in a new session)

Owner asked (hours before release): commit tested work, then break the product end to end on THIS PC (Windows 11 Home,
DESKTOP-N7II2M6, no trial box), real credentials allowed, install the services, LAN = loopback, cloud = real GCS,
deep special checks of the FY overlap (12 + 3 month) feature, plus Windows Server 2016 / 2019 / Win10 / Win11 compatibility.
Everything is **uncommitted state is listed explicitly below**; this file is the single source of progress.

## 0. Owner's working rules for this session (follow them)
- **Model split / token efficiency:** Opus ONLY for test design/high-level planning and final verification; **Sonnet = orchestrator,
  reviewer, bug fixer**; **Haiku = mechanical test writing, running, research**. Ask Opus only when Sonnet has doubts.
  Spawn subagents only when they save effort. (Memory: `model-split-token-efficiency`.)
- Fixes are written by Sonnet in a git **worktree**, reviewed by the orchestrator, then merged.
- Owner pre-authorised: elevated shell, installing services here, real GCS creds, pausing/stopping the other product AAMMerger.
- Do not edit product code in the MAIN checkout while the audit lab is running (its Prefect children import fresh code).

## 1. Git state
Branch `fy-overlap` (main checkout `C:\Users\malliath\Desktop\aam_backup`), commits made this session:
- `a3826d5` restore/upload tools (OV-10.1, owner's work) + tests; `17fa758` seed_daily tool + tests + doc
- `36953aa` break-it suites (6 files) + hypothesis properties + config fuzz
- `8ed49a7` secrets scan, rclone auth/throttle log table, report counts, alert 72h bound, folder/argv invariants
Branch **`release-candidate`** = worktree `C:\AAMTest\code` (HEAD `aa561bb`) = fy-overlap + ALL fixes below (merged from worktree
branches `worktree-agent-af3212604f9944d10` (fixes) and `worktree-agent-a523ff8af0ccd36c3` (SILENCE-2)). **Not yet merged back into `fy-overlap`**
(wait until the audit lab finished, then `git merge release-candidate` in the main checkout, run the unit suite, commit).
UNTRACKED in main (tests written by agents after the last commit; commit them when green): `tests/test_break_release_ops.py`,
`tests/test_break_release_approvals.py`, `tests/test_break_release_s1.py` (these three already exist, edited, inside release-candidate - take
release-candidate's versions on merge), plus whatever the overlap test agents wrote: `tests/test_ov_timeline_a.py`, `test_ov_timeline_bc.py`,
`test_ov_timeline_def.py`, `test_ov_review_points.py` (see section 5). Scratch files at repo root (analyze_logs*.py etc.) are the owner's, leave them.

## 2. Test files added (all new, mostly green; suspected bugs kept as xfail(strict=False))
`tests/test_break_{flow,notify,fy_overlap,ui,db_artefacts,restore_upload}.py`, `test_property_release_break.py` (hypothesis),
`test_property_config_fuzz.py` (400 mutated configs), `test_break_release_{ops,secrets,cloudlog,reports,approvals,s1}.py`,
`test_property_release_{alerts,paths}.py`. Combined run before fixes: 321 passed, 3 skipped, 15 xfailed. Design: `docs/TEST_DESIGN_RELEASE_2026-11-22.md` (Opus; 40 cards,
live-stack runbook LS1-LS13, mutation/verification protocol, release gate). Overlap design: `docs/OVERLAP_REVIEW_2026-11-22.md` (Opus; D1-D13 + 37 cards).
(Copies of both are committed under docs/.)

## 3. Bugs found and FIXED (in `release-candidate`, each with a regression test)
1 `suggested_name` zero-pad + `_FY_LOOKING` accepted 1-digit end year ('FY27-8' was never flagged misnamed) | 2 non-object JSON body -> 500 on settings/validate/password (now 400)
| 3 `phrase_matches` non-text -> 500 | 4 cross-site Origin / Sec-Fetch-Site POST refused (middleware, absent header allowed) | 5 alert body ignores `body_max_chars` when no fault code
| 6 purge date regex ASCII-only | 7 restore folder names reject ':' etc. | 8 restore junction check before resolve | 9 restore FY name upper-cased | 11 cancel/Ctrl-C while waiting for the backup slot left a RUNNING row
| 12 `read_lock_alive` crashed on non-UTF-8 lock file | 13 approve of an FY that is not active -> 409 | **SILENCE-2** invalid config.yaml => agent never starts and nobody was mailed: watchdog now sends ONE ACTION_REQUIRED mail from the raw
YAML notifications section (marker-file throttle `watchdog_invalid_config.sent`), `launch.py` logs a one-line FATAL.
Skipped by design: #10 (restore engine default `forbidden_paths=()`; CLI passes the list - owner API decision).

## 4. Findings that are NOT bugs / decisions
- LAN robocopy exit 4-7 with a clean summary = `LAN_COMPLETE` is deliberate (BUG-05 analysis in `lan_sync.decide_lan_result`); weekly MD5 audit is the independent check.
- SILENCE-1 (no dashboard warning while mail fails): NOT present (`alerts_undelivered` red banner + /health 503).
- SILENCE-3 / U10 (old-schema manifest.db): `launch.py _mark_abandoned_runs` is "never blocks startup" by design; backups then fail loudly; whether a mail is sent is checked by live card LS8b (open).
- Purge items (days<=0, 10**400, loose files at root) are unreachable via config bounds (retention 7..3650) / by design.
- Open design item for owner: alert-controls plan steps D-G (follow-up limits) are not in code (see docs/PLAN_ALERT_CONTROLS_2026-10-22.md).
- Config-fuzz test failed once in a full run, not reproducible on 3 seeds: re-run it a few times (`--hypothesis-seed=N`).

## 5. FY overlap deep review (Opus, `docs/OVERLAP_REVIEW_2026-11-22.md`) - verdict: nothing blocks a December deploy
Fix before **1 Jan 2027**: **D3** deferred activation (junction / no create rights) = ONE mail only, no dashboard item/repeat/RESOLVED => possible silent 3-month miss (`fy_overlap.py:239-284`, `:450`);
**D4** owner decision: big first upload of the early folder can use the whole shared budget and leave the current FY `CLOUD_MISSED` for nights (`flow.py:3285-3294`).
Fix before **1 Apr 2027**: **D2** `_close_old_fy` never retried after journal complete (`flow.py:2331`, `:2665`); **D1** a flow that loaded config before the switch syncs the closed FY into the
archived prefix (`flow.py:3388` vs `:3280`). Recommended: D7 (final-backup slot timeout becomes approvable G6 block), D9 (DONE = fix 13). Owner calls / minor: D5 (early NAS folder silently re-created),
D6, D8, D10, D11, D12, D13 (gcs_lifecycle.json `age 365 -> ARCHIVE` unscoped; WP12 / `deploy/gcs_lifecycle.fy_overlap_proposed.json`, not applied).
**Test agents writing the 37 cards** (xfail = DEFECT Dn until fixed): `test_ov_timeline_a.py` (T-a1..a6, P-3, P-4, P-12), `test_ov_timeline_bc.py` (T-b1,b2,T-c1..c5), `test_ov_timeline_def.py`
(T-d1,d2,e1..e3,f1..f3,P-7), `test_ov_review_points.py` (P-1,2,5,6,8..11,13,T-b4). Lab card T-b3 and live card T-f4 NOT written yet.

## 6. Environment on this PC (changed this session - IMPORTANT)
- **AAMMerger (other product) is STOPPED and DISABLED**: services `AAMMerger-PrefectServer`, `AAMMerger-Web`, `AAMMerger-Worker` (were Running/Automatic; list in `C:\AAMTest\merger_was_running.txt`).
  **To restore (do this when testing ends):** `Set-Service <name> -StartupType Automatic` for each, start `AAMMerger-PrefectServer`, then `AAMMerger-Worker`, then `AAMMerger-Web` last. It used ports 4200 and 8000.
- `C:\AAMTest\` sandbox: `code` (git worktree, branch release-candidate), `source\FY26-27` (41 files incl. Malayalam names + 20 MB file), `nas`, `runtime`, `config\config.yaml` (valid; dashboard 127.0.0.1:8080, SMTP 127.0.0.1:2525),
  `secrets\` (aam-gcs-key.json copy, svc_password.txt, dashboard.txt - NEVER commit/print), `smtp_sink.py` (running detached, appends `mails.jsonl`), `INSTALL_REPORT.md` (the install agent's state/commands).
- Real GCS test bucket **`aam-backup-qa-bilcvx`** (asia-south1, project 920173882190), created by this session; the demo bucket `aam-backup-demo-innovizta` was NOT touched. Delete the bucket when done.
- Local user `aamtest` and SMB share `AAMTestNAS` -> `C:\AAMTest\nas` exist. Services (AamPrefectServer / AamBackupAgent / AamWatchdog) are **NOT installed yet** (install agent was working from `C:\AAMTest\code`; check `INSTALL_REPORT.md`).
- Elevated worker queue (may be dead in a new session): drop `NNN_name.ps1` into `<session scratchpad>\elevq\`; worker kills scripts after 600 s.
  Needs a UAC click to (re)start it (`scratchpad\elev_worker2.ps1`). Never use Get-LocalUser/New-LocalUser or unbounded service-stop waits in elevated scripts (a script hung that way before).
- **Gotcha:** `deploy/04_check_readiness.ps1` runs `uv sync --frozen` WITHOUT the dev group: run from the dev checkout it removed pytest/hypothesis from the main `.venv` (restored with `uv sync --frozen`).
  Run deploy scripts only from `C:\AAMTest\code`. Memory: `readiness-script-strips-dev-venv`.
- Readiness gate failures on this test PC (accepted for test): gcloud missing, Windows Update active hours, W32Time stopped (start it), `.\aamtest` account lookup error ("server could not be contacted" - unresolved, see INSTALL_REPORT), public_url 127.0.0.1.
- Base Python lives under `C:\Users\malliath\AppData\Roaming\uv\python` (service account can't read it): use `UV_PYTHON_INSTALL_DIR=C:\ProgramData\uv\python` for the services' venv.
- Audit lab (real robocopy/rclone, ~90+ min) was started in background: output `agent_runs/lab_release.txt` (run with `PREFECT_HOME=...\Temp\ph_lab`). It showed one `F` near 90%: TRIAGE when finished (rerun the failing test alone with a clean PREFECT_HOME; known intentional red: `test_w16_schedule_collisions_across_year`).

## 7. Windows compatibility (2016 / 2019 / 10 / 11)
All 11 deploy/tool `.ps1` scripts parse with the PowerShell 5.1 parser (this PC, no PS7-only syntax). A Haiku research agent was writing `agent_runs/COMPAT_WINDOWS.md` (min-OS facts per dependency, `wmic` removed on Win11 24H2+,
Defender/SMB cmdlets, localized-output parsing, SMB signing defaults). **Cannot be proven here:** actual runs on Server 2016/2019 (needs real machines; checklist is in that file's last section).

## 8. TODO (in order)
1. Wait for / collect: the 4 overlap test agents, the install agent (services), the compat scan, the audit lab; triage the lab `F`.
2. Sonnet fixes in a worktree off `release-candidate`: D3, D2, D1 (+ D7, D6, D8, D12 if cheap); flip the matching xfails to hard tests; D4/D5/D11/D13 = write up for the owner with a recommendation.
3. Merge `release-candidate` -> `fy-overlap` (main checkout, after the lab ended), full unit suite (`.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider -o addopts="" --timeout=600`), commit the untracked tests.
4. Live-stack runbook (TEST_DESIGN section C: LS1 baseline, LS2 kill agent mid-LAN, LS3 NAS share stops, LS4 key revoked (owner OK needed), LS6 patched run date = overlap Jan-Apr timeline, LS8/8b manifest.db rename / old schema, LS9 watchdog killed, LS10 power loss, LS11 SMTP down, LS12 secret scan, LS13 bad config at boot -> now expects the SILENCE-2 mail) + browser check of the dashboard (Origin middleware).
5. Lab card T-b3; mutation check (non-vacuity) of the Haiku-written tests in a worktree.
6. Opus final verification (release gate in TEST_DESIGN D3), then update SESSION_HANDOFF.md, restore AAMMerger, uninstall the test stack, delete the QA bucket, tell the owner what remains unverified (Server 2016/2019 runs).
