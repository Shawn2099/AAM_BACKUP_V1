# HANDOVER PROMPT — paste this into a new session

Copy everything between the markers and paste it as the first message. It is
written so the new session loads the right files itself and does not redo the
work already done.

---

You are picking up an in-progress **audit of the AAM Backup Automation V1** codebase at `C:\Users\malliath\Desktop\aam_backup` (a Prefect-3 nightly backup system: rclone→GCS for the cloud leg, robocopy /MIR→SMB for the LAN leg).

**Do not start by exploring the repo or re-running everything. Load the handover first:**

1. Read `HANDOVER.md` — the map: what the repo is, what `audit_lab/` is, the wave map, the current findings, the deployment blocker, and the lab bugs I already fixed.
2. Read `docs/audit/LAB_VERIFICATION_REPORT_2026-10-10.md` — the full findings report (§2 = confirmed defects with root cause + fix, §8 = the stress-testing waves).
3. Skim `audit_lab/LEDGER.md` — the evidence ledger (81 distinct experiments, 1 658 evidence rows). This is generated; regenerate it with `.\.venv\Scripts\python.exe audit_lab\make_ledger.py` only after a new run appends evidence.

**Ground rules you must keep:**
- **Never edit production code unless I explicitly ask for a fix.** So far the instruction has been "tests only, no fixes" — findings are recorded, not fixed. If I ask for a fix later, run an impact/b ROLE check before touching any symbol and warn me if the blast radius is HIGH or CRITICAL.
- `audit_lab/` is my addition; it is the test surface. The lab's safety rules live in `audit_lab/labconf.py` (`assert_lab_path` confines all destructive I/O to `%TEMP%\opencode\aam_lab`, `build_config()` never reads the repo `config.yaml`, WoL/shutdown force-disabled). Respect them.
- Long test runs must be started **detached** (`Start-Process … -RedirectStandardOutput … -PassThru -WindowStyle Hidden`) — a foreground shell is killed at 10 minutes.
- The deployed box is currently broken: `AamBackupAgent` and `AamWatchdog` fail with **error 1069 (logon failure)**, so Wave 7 (`audit_lab/test_w7_deployed.py`) cannot pass and no backups run. Don't try to fix that without me; the remedy is `audit_lab\setup_service_account.ps1 -Apply` from an elevated shell.

**Known-good baseline** (waves 0-6 + 8-11, excluding Wave 7): **59 passed / 2 skipped / 0 failing**. The single intentional red is `audit_lab/test_w9_stress.py::test_w9_manifest_db_concurrency_stress`, which fails loudly to report finding **M-6** (concurrent `ManifestDB` opens silently drop run-history rows — root cause is `PRAGMA busy_timeout` being applied after the dedup DELETE at `core/manifest.py:162`). Don't "fix" that test to make it green.

**Where I left off:** I had just finished **Wave 11** (`audit_lab/test_w11_wrapper.py`, 8 tests, all passing) — wrapper-correctness tests, because rclone/robocopy are battle-tested and the real risk is *our* code around them (~30 parsing sites in `integrity.py`, `fy_rollover.py`, `lan_sync.py`, `cloud_verify.py`). Wave 11 covers real-log corpus replay, the audit engine's branches, round-trip parser canaries, and the gate message attribution.

**What I'd like from you next:** propose and write the next wave of tests, in this order of value —
1. Convert the ~10 assert-free load-bearing tests into real assertions (today a green run overstates enforcement; `HANDOVER.md` §8 lists them).
2. A fake-clock wave: FY rollover boundary (1 April 00:00 IST), clock skew handling, `purge_old_runs` retention, and a simulated 7-day nightly cycle.
3. Branch-exhaustive tests for the remaining under-covered wrapper functions listed in the report §8 / the coverage map from Wave 10.

Ask me before implementing anything; I want to review the test plan first.

---
