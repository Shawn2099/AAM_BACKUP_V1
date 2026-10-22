# Plan: install on a clean Windows Server 2016 from scratch (rev 2, 2026-10-23: rev 1 + Opus review, section 5 is binding where it differs)

Owner goal: "I can install on a Windows Server 2016 from scratch." Source: Opus audit of the install path
(2026-10-23). Process: this plan -> Opus review -> implement test-first / script-checked -> Opus re-review.
The trial box is NOT touched by this work (no deploy of installer changes until a clean-VM run passes).

## 0. Principles
* One written path, in order, for a person at the server: `docs/INSTALL_FRESH.md` (new; replaces DEPLOY_RUNBOOK
  sections 1-6 for a first install; DEPLOY_RUNBOOK stays as the UPGRADE runbook and says so at the top).
* Every external dependency is tested BEFORE services are installed (bucket + key, mail login, NAS folder, WoL
  target) with a message that names the fix. Nothing is first discovered at 02:00.
* No script prints SUCCESS / INSTALLATION COMPLETE unless the thing it claims was verified.
* Safe defaults for a first install: `lan.shutdown_after_backup: false` until the owner has done the shutdown
  test at the NAS (owner rule: real NAS shutdown only with the owner present).
* Nothing is hard-coded to the trial box or this PC.

## 1. Work items (ID = Opus finding no.)
| ID | Item | Where |
|---|---|---|
| I-1 (1) | New `docs/INSTALL_FRESH.md`: prerequisites (accounts, NAS share + `FYnn-nn` folder + `.AAM_TARGET_MOUNTED`, GCS bucket/IAM/key, source year folder), getting the code (git bundle/zip of HEAD, NOT stale origin), then the script order. DEPLOY_RUNBOOK gets a banner "upgrade only"; its step 9 rollback says trial box only. | docs |
| I-2 (2) | Readiness works on a fresh machine: 04 creates `runtime_dir` if missing; mail-password "not encrypted" is a WARN with the fix before 06 (FAIL only after 06); account must have batch-logon: 06/04 grant via `secedit`/`ntrights` equivalent or the doc adds it to *Backup Operators* (gives both rights) - pick what works on 2016 (check in the clean-VM run); `bind_address` check explains the fix and the wizard asks for the dashboard address. | deploy/04, 06, core/secret_setup.py |
| I-3 (3) | New pre-install checks in 04 (run as the service account where the credentials matter): (a) `gcs_key_path` from the CONFIG exists, is valid JSON service-account key; (b) `rclone lsd/size` on `aam_gcs:{bucket}` and a write+delete of a probe object under `_aam_preflight/` (the existing preflight call, reused); (c) SMTP: connect + STARTTLS + login (no mail sent) using the decrypted password; (d) with `-SendTest` a real test mail to both lists. Missing key = FAIL (not WARN). | deploy/04, new `deploy/probe_cloud_mail.py` |
| I-4 (4) | `wol.server_ip` must be the host of `lan_destination` (validator warns/fails when the NAS leg is on and WoL is on and the hosts differ; hostnames compared case-insensitively; skipped if either is a name that cannot be resolved offline - then WARN in 04). Wizard writes `lan.shutdown_after_backup: false` and says why; the doc has a "enable after the NAS shutdown test" step. Example placeholders (`AA:BB:..`, `192.168.1.100`) rejected when WoL is on. | models/config.py, wizard, doc |
| I-5 (5) | Doc/step: first LAN run is LAN_BLOCKED by guard G5 on an empty NAS year folder unless `.AAM_TARGET_MOUNTED` exists; doc tells to create it (10_recreate_canary.bat) BEFORE the first run, or approve on the dashboard. | doc, 10_recreate_canary.bat |
| I-6 (6) | 04 sets `UV_PYTHON_INSTALL_DIR` before any `uv run`; if `.venv` exists with a Python under a user profile, 04 says "delete .venv and re-run 03". | deploy/04 |
| I-7 (7) | 06: `INSTALLATION COMPLETE` only if all three services report Running after start and the dashboard answers; otherwise "INSTALL FAILED: <service> not running" and exit 1. 04: summary line says "N warnings open" and is not "SUCCESS" while a WARN for a required thing is open. Dead gcloud check removed. | deploy/04, 06 |
| I-8 (8) | 06 line ~475 config check: `Push-Location $RepoRoot` and use the venv python with `PYTHONPATH`; failure happens BEFORE service install, not after. | deploy/06 |
| I-9 (9) | Replace `02_collect_config.ps1` (trial-box values, invalid YAML, unknown keys) by `deploy/02_make_config.py` (+ ps1 wrapper): interactive wizard that writes a valid `config.yaml` from `config.example.yaml` using only real keys: source (year folder mandatory, validated live: exists, ends in FYnn-nn, matches today's FY +/- warning), NAS path (same year leaf, reachable), bucket/project/location/key path, WoL mac+ip (default off unless given), SMTP + two recipient lists, dashboard bind/public_url, cron times, timezone (default Asia/Kolkata, asked). The result is loaded through `AppConfig` before saving. Backslashes handled in Python (no shell quoting). | deploy |
| I-10 (10) | Fold the GCS bucket / service account / IAM (`roles/storage.objectAdmin`) / versioning / soft-delete / lifecycle / uniform bucket-level access steps into INSTALL_FRESH.md with copy-paste `gcloud` commands; mark `DEPLOYMENT_GUIDE.md` "SUPERSEDED - do not use" at the top. Verify rclone flag names with `rclone help flags gcs` on the bundled rclone. | docs |
| I-11 (11) | `.gitattributes`: `*.bat`, `*.ps1`, `*.cmd` `eol=crlf`; `*.py`, `*.md`, `*.yaml` `eol=lf`. Renormalise the deploy scripts (single commit, no content change). | repo root |
| I-12 (12) | 03: extract gcloud itself with the bundled `7za.exe` (or PowerShell `Expand-Archive`) from `deploy\bin\*.zip` if the zip is present; verify `gcloud.cmd` exists after; clear message otherwise. | deploy/03 |
| I-13 (13) | `core/permissions.py` write-probe message distinguishes folder missing / path unreachable / NAS asleep from missing modify rights. 04 notes the NAS must be on. | core/permissions.py |
| I-14 (14) | Example config year; wizard computes the current FY from the date (Indian FY: Apr-Mar); 04 FAILs if the configured source FY is not the current FY (an install in the wrong year would immediately start a rollover). | config.example.yaml, wizard, 04 |
| I-15 | Nits: next-step text consistent with the runbook order; two scripts numbered 07 (rename gcloud one); `echo |` escape in 07_restart; `RUN_GCLOUD_LOGIN.bat` hard-coded path; uninstall must kill only processes whose path is under the install/runtime dir; remove committed `server_discovery_report.*`; SESSION_HANDOFF line 17/32 fix. | misc |
| I-16 | Old-year bulk upload tool (separate plan, `docs/PLAN_BULK_UPLOAD_*.md`): `rclone copy` (never sync), one prefix per old folder, never into the current FY prefix, ARCHIVE class, bandwidth limit, ends with `rclone check --one-way`; invoked by the doc as a final step. | tools |

## 2. Order
1. Plan -> Opus review (this doc). 2. I-11 (line endings), I-1/I-10 docs skeleton. 3. I-9 wizard + tests
(the wizard's pure functions are unit tested: path -> FY leaf, YAML produced loads through AppConfig, WoL host rule).
4. I-3 probes (+ unit tests with fake_run_tool / smtp_server). 5. I-2, I-6, I-7, I-8, I-12 script fixes (checked by
PowerShell parse test + a Pester-less smoke on the clean VM). 6. I-4, I-13, I-14 (config/validator, TDD).
7. I-5, I-15. 8. I-16 own plan. 9. Opus re-review. 10. CLEAN-MACHINE DRY RUN: a fresh Windows Server 2016 VM
(or fresh Windows Sandbox/VM) following only INSTALL_FRESH.md; every deviation is a bug. Cannot be done without
a clean machine - the owner supplies one (or a snapshot of the trial box before install).

## 3. Not done by this plan / limits
* Proxy/AV behaviour at the client, NAS BIOS WoL support, remote shutdown rights: only visible on the real site.
* Real NAS shutdown stays owner-at-NAS only.
* Lab is running while this is written: no edits to modules the lab imports until its REPORT.md is in; docs and
  deploy/ scripts are not imported by the lab. models/config.py (I-4) and core/permissions.py (I-13) wait.

## 4. Decisions for the owner
1. Push `mvp-hardening` to origin (it is 68 commits behind) so the client machine can `git clone`? Or ship a
   zip / git bundle on a USB stick (no GitHub needed)? Recommendation: bundle/zip; pushing publishes the repo.
2. Rights for the service account: "add to Backup Operators" (simple, documented, gives backup + batch logon) -
   recommended - versus fine-grained policy edits by script.
3. A clean Server 2016 VM or snapshot for the dry run (without it the installer is untested end to end).

## 5. Rev 2 - Opus review changes (binding; they override sections 1-4 where they differ)
Blockers added:
* R-1 Windows timezone: a clean 2016 is Pacific. 03/doc: `tzutil /s "India Standard Time"` + `w32tm /resync`; 04 FAILs
  if `tzutil /g` does not match `schedule.timezone`. Update Active Hours (03) use Windows local time.
* R-2 04 checks rights that only 06 grants: move the icacls grants (06:228-242) into a shared step run by 04 before
  the probe; 04 also creates `runtime_dir`.
* R-3 04 sets `UV_PYTHON_INSTALL_DIR` / `UV_CACHE_DIR` as its first lines; drop the `uv run python --version` check.
Should-fix added:
* R-4 No self-elevation (output vanishes): scripts refuse with "open PowerShell as Administrator".
* R-5 Doc: always `powershell -ExecutionPolicy Bypass -File`; zip delivery needs `Unblock-File`.
* R-6 Server has only IE11 (dashboard JS needs Chrome/Edge): doc says open the dashboard from an office PC; the
  06 "dashboard answers" check polls `/api/health` (not a fixed 15 s wait).
* R-7 Lock the code folder in 03: `icacls C:\AAMBackup /inheritance:r` + Administrators:F, SYSTEM:F, Users:RX (services
  run as SYSTEM from it). Config in its own folder via `AAM_CONFIG` is an owner option (decision 4 below).
* R-8 Source year folder must contain at least one file (probe_source_read, G1): doc requirement.
* R-9 Canary: create `.AAM_TARGET_MOUNTED` on the NAS together with the year folder (admin has no NAS credentials).
* R-10 Mail password: the config template leaves it empty; 06 asks (hidden) and writes `dpapi:` immediately; 04 rule
  becomes "empty = FAIL after 06". Must be entered on the server itself.
* R-11 Verify on the clean VM: Backup Operators + UAC-filtered token vs probe `-RunLevel Limited`; record the result.
* R-12 I-3 simplified: ONE mail check = a real test mail to both lists from 04 (no `-SendTest`, no login-only mode);
  cloud probe runs as the service account and reuses the `_aam_preflight` write+delete probe.
Design changes:
* I-9 wizard DROPPED. Use `deploy/config.install.yaml` (about 20 site keys, paths in single-quoted YAML, WoL and
  shutdown_after_backup false by default, smtp_password empty). 02_collect_config.ps1 deleted. 04 validates it through
  AppConfig and runs the live checks, and prints the detected server IPs and the current FY.
* I-4 is NOT in AppConfig load validation (would crash-loop existing sites). It lives in 04 and the Settings save path
  (core/config_edit.py): FAIL only if lan on, WoL on, UNC host is an IPv4 literal and differs from wol.server_ip;
  WARN if the UNC host is a name. No DNS in validators. Example placeholders: reject MAC AA:BB:CC:DD:EE:FF; change the
  example IP to 192.0.2.10 and reject that one (192.168.1.100 is a plausible real address).
* I-14 lives in 04 only: reuse `get_fy_prefix` + `classify_rollover`; FAIL on JUMP / CLOCK_BEHIND; FORWARD = FAIL only
  when there is no run history (fresh install), else WARN. Trial box (E:\FY26-27 today) passes.
* Small separate fix: `notify.py:420` and `email_compose.py:120` use `wol.server_ip` even when WoL is off - use the
  lan_destination host when WoL is off.
* Set `PREFECT_HOME=<runtime>\.prefect` in 04/06 for consistency.
Revised order: 1 I-11 line endings; 2 INSTALL_FRESH skeleton with the prerequisites (timezone, Backup Operators,
NAS account + year folder + canary, a file in the source year folder, Bypass/Unblock, dashboard from an office PC);
3 script fixes (03 ACL lock + I-12; 04 env first, runtime folder + grants, tz check, no self-elevation; 06 health poll,
I-8); 4 config template, delete 02, hidden mail prompt; 5 probes (cloud as account, real test mail); 6 I-4/I-14 (04 +
Settings save) and I-13 - AFTER the audit lab finishes; 7 I-5, nits; 8 Opus re-review; 9 clean-VM dry run (fresh 2016
VM, NOT a trial-box snapshot); 10 I-16 own plan.
Owner decisions (recommended): 1 code delivery = git bundle on USB + Git for Windows on the server (same tool for
install and upgrade; fallback zip from `git archive` + VERSION + Unblock-File); 2 Backup Operators via one
`net localgroup` command (also lock the code folder, R-7); 3 clean VM mandatory; 4 config.yaml in its own folder
(optional).
