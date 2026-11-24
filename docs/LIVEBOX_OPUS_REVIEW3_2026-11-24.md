# Livebox harness - Opus review 3 (before the first box run) - 2026-11-24

Read-only review. Nothing was edited, nothing was run on the box. Harness: `aam_baseline_overlap/tools/livebox` (untracked).
Product reviewed: worktree HEAD 4730bd7 = dc50265 + one docs file (no code difference to the box).
Paths below are relative to `C:\Users\malliath\Desktop\aam_baseline_overlap\`.

## Verdict
- **`--days 0` preflight on the box is safe to run next**, with the exact arguments below, the owner present, and not within 30 min of a production job.
- Fix **H1** before L12 (the first lane that runs flows). Fix **H2** and **M1** before you call the NAS-off or cloud-only/LAN-only cells covered.
- No finding lets a lane power anything off, reach the real NAS, real GCS, or real SMTP, or change the services or the schedule. One hole (H1) could send Prefect API calls, in rare thread paths, to the live Prefect server on :4200.

## 1. Box safety (ranked)

**H1 (HIGH, fix before L12): `import launch` points os.environ at the production Prefect, and S6 cannot see it.**
- `launch.py:21` sets `os.environ["PREFECT_API_URL"] = "http://127.0.0.1:4200/api"` at import.
- The harness imports launch in `run_lane.py:1082` (`_guard_s6_before_concurrency_limit`), after `prepare_env` removed the variable (`run_lane.py:772`).
- Inside `prefect_test_harness` the lane URL comes from the settings context. That context is a contextvar. A thread that does not copy the context falls back to the environment, which means production :4200.
- Example: `launch._ensure_concurrency_limit` uses a `ThreadPoolExecutor` when a loop is running (`launch.py:117-120`). That thread would upsert `aam-backup` on production.
- S6 is a tautology. It compares `PREFECT_API_URL.value()` with `rig.prefect_api_url`, which was read from the same setting (`rig.py:1084`, `run_lane.py:1313-1314`). It never looks at `os.environ`.
- Dev evidence cannot show this problem. The dev PC has nothing listening on :4200, so a leak fails quietly. On the box the leak would succeed.
- The TestClient paths work: `/status` shows `run_state: idle` on the dev PC, so the context does propagate to the ui. The risk is limited to other threads, but it is real.
- Fix (2 lines):
  - Right after `import launch`, pop `PREFECT_API_URL` from `os.environ`, or set it to the lane URL once the harness is up.
  - In `safety_check` (run_lane.py:1402), add a check that `os.environ.get("PREFECT_API_URL")` is not :4200.

**H2 (HIGH for coverage, safe as it fails): every "NAS unreachable" scenario aborts the lane at S15.**
- `set_nas("192.0.2.1")` makes robocopy target `\\192.0.2.1\C$\...`. `check_argv` refuses any UNC host that is not loopback (`safety.py:338-347`).
- Evidence: `agent_runs/livebox_rv3s/w1_x08/summary.json` lists `argv_violations: S15 ... \\192.0.2.1\...`, and the lane was aborted.
- This affects X08, X09 and X14, which are the NAS-off cases on 31 Dec and 1 Apr.
- Fix: in `check_argv`, accept UNC hosts that the rig allows (192.0.2.0/24 and `*.invalid`, the same rule as `rig.py:831-839`). They cannot reach anything.

**M1 (MEDIUM): `--cloud-only` / `--lan-only` change only the expected calendar, not the product config.**
- `run_lane.py:435` passes the flags to `build_calendar` only. The lane config keeps production `cloud.enabled` / `lan.enabled` = true.
- Result: the leg the calendar skipped is still enabled and becomes overdue, so the watchdog, status and mail oracles turn red.
- X34 and X35 set the config through `_set_flag` (`scenarios/w3.py:22-40`), but their calendar still expects both legs unless the flag is also passed.
- `_set_flag` writes the in-memory `rig.config`, which is stale. On `--resume`, `open()` writes the resumed YAML but leaves `rig.config` as the fresh derive (`run_lane.py:1054-1059`). A setup that runs again after a resume therefore reverts a product FY switch.
- Fix: make the two flags also call `rig.set_mode(...)` at open, add a "both off" option, and let X34/X35 use `set_mode` (`_save_config` reads the file from disk).

**M2 (MEDIUM): a wrong `--cleanup` call can delete harness folders.**
- `cleanup_lane` only requires `<root>\<stamp>\<lane>` (`run_lane.py:279-311`). The stamp regex is applied in `LaneHooks` only.
- With the planned layout `C:\AAMLivebox\harness`, the call `--cleanup tools --stamp harness` passes every check and deletes the harness code.
- Fix (either one): put the lanes under `C:\AAMLivebox\lanes`, or refuse a cleanup unless `config.livebox.yaml` or `evidence\state.json` exists in the lane.
- Also: cleanup does not check whether that lane's process is still running.

**L1 (LOW): defence in depth that is fine as it is.**
- Power-off: four independent layers, re-checked before every event (`run_lane.py:1410`):
  - the `lan_shutdown_task` spy;
  - the `flow.shutdown_from_config` raiser;
  - the `core.shutdown.shutdown_server` raiser;
  - the run_tool guard that refuses `shutdown`, `gcloud` and `wakeonlan` (`run_lane.py:1358-1362`). It also patches `core.tool_runner.run_tool`, so lazy imports are covered.
- Residual risk: `wol.server_ip = 127.0.0.1` (`derive_config.py:96`), so a bypass of all four layers would target the box itself. Keep it: `wol_target` requires the IP to match the NAS-path host.
- GCS: every remote is either an alias to the lane bucket or a GCS remote whose endpoint is 127.0.0.1:9 or `.invalid`, with a throwaway key whose token_uri is also blackholed. The archive step is a seam. All four `temp_rclone_config` users are seamed (flow, cloud_sync, cloud_preflight; `core.rclone_config` itself is not used directly).
- SMTP: S7 requires 127.0.0.1, no auth and `.invalid` recipients. No other recipient key exists.
- NSSM / schedule: no `sc`/`nssm` call. watchdog and ui are imported but their `main()` never runs. No deployments are created.
- Optional hardening:
  - strip `RCLONE_*` and `GOOGLE_APPLICATION_CREDENTIALS` in `prepare_env`;
  - run git with `GIT_OPTIONAL_LOCKS=0`, because `git status` can refresh `C:\AAMBackup\.git\index`;
  - start Python with `-B`, so nothing compiled before `prepare_env` writes `.pyc` into the production venv.

**L2 (LOW, box differs from the dev PC): the clock-proof fallback may behave differently on the box.**
- `health.ntp_server = 127.0.0.1` (`derive_config.py:108`). The product falls back to `w32tm /stripchart /computer:127.0.0.1` when the Google seam reports no internet (`core/health.py:143`).
- On Win11 Home this fails. Server 2016 may answer with an offset near 0, which would make X01 and X02 ("clock unproven") prove the clock on the box.
- Check by hand during L11 (read-only): `w32tm /stripchart /computer:127.0.0.1 /samples:1 /dataonly`. If it answers, set the derived `ntp_server` to `192.0.2.1`.

**L3 (LOW, operational).**
- **Dry run:** `--dry-run` on the planned layout fails with ModuleNotFoundError (models, test_support). It does not call `prepare_env` and does not add `--code-root` to `sys.path` (`run_lane.py:671-683`). It also derives from `dev_prod_raw`, not from the production config (517, 740). Skip it and use `--days 0`, which is the real gate.
- **Loopback share:** `\\127.0.0.1\C$` may be refused in a key-authenticated ssh session (no network credentials; the real NAS share is already access-denied for this user). `--days 0` does not touch the UNC path, so check it by hand once: `Test-Path \\127.0.0.1\C$\AAMLivebox`.
- **Long lanes:** a lane dies if its ssh session closes. Use `--resume`, which works.
- **Stamps:** must match `[a-z0-9-]` (no `_`, `run_lane.py:933`). A preflight stamp cannot be reused for the real run without `--resume`.

**What each S check covers in a real run:**

| Check | When it runs | Note |
|---|---|---|
| S1-S5, S7 | At open, before the seams | |
| S6, S8, S9, S10-S14 | At open, inside the harness | |
| S9, S10, S11 | Before every event | Pause on the real clock |
| S12 | Every 20 events | |
| S15 | On every robocopy / rclone argv | |
| S13, S14 | At open only | Enough: each lane counts the others at its own open; the production slot is only read |
| S10, S11, S14 without production facts | - | Refuse at open on the box (fail closed): `run_lane.py:1329-1342` |

**Prefect slot `aam-backup`:** the lane creates its own slot on its private server and only reads the production one (S14 GET). H1 is the only way it could reach production.

**Planned layout:** it works with `--code-root C:\AAMBackup` (`sys.path` and S5, `run_lane.py:779-781`) and the cwd at `C:\AAMLivebox\harness` (`tools` is a namespace package in C:\AAMBackup; `tools.livebox` is already loaded from the harness).

**The plan text differs from the code:** plan line 67 (`harness\run_lane.py --root C:\AAMLivebox\<stamp>\A`) does not match the code. `--root` must be `C:\AAMLivebox`, and the run must use `-m tools.livebox.run_lane`. A wrong root is refused (S2), so this is not a safety issue.

**Preflight command** (owner present, outside 20:31-21:31, 01:31-02:31, 05:30-06:30 and 07:30-09:00 IST; Sundays, Mondays and the 1st have 08:00/08:30 jobs):
```
cd C:\AAMLivebox\harness
C:\AAMBackup\.venv\Scripts\python.exe -B -m tools.livebox.run_lane --lane p0 --stamp l11-20261125 --days 0 ^
  --root C:\AAMLivebox --code-root C:\AAMBackup --expected-commit dc50265 ^
  --prod-config <the AAM_CONFIG of AamBackupAgent, read from nssm> --prod-api http://127.0.0.1:4200/api ^
  --overlay C:\AAMLivebox\harness\tools\livebox\overlay\time_machine_pkg
```
- Pass when: exit 0, and `evidence\safety.json` shows S1-S14 ok (S10, S11, S14 for the first time with real production facts).
- Then run `--cleanup p0 --stamp l11-20261125`, and check that nothing changed outside `C:\AAMLivebox`.

## 2. Real-life coverage

**Leg settings:** B = both legs on (production), C = cloud only, L = LAN only, O = both off. All waves except X34-X36 run in B (production config).

**Status labels:**
- **ok** = runs today.
- **BROKEN** = the lane aborts (H2).
- **NR** = NOT_RUN by decision.
- **gap** = nothing runs it.
- **reuse** = an existing scenario or calendar lane covers it once M1 is fixed, by adding `--cloud-only` / `--lan-only` (and `--outage`).

| Situation | B (both on) | C (cloud only) | L (LAN only) | O (both off) |
|---|---|---|---|---|
| Normal nights incl. weekends | Lane A 728 d (104 Sun, 104 Mon, 24 firsts, leap day): ok | X34 (20 d): ok, plus calendar lane `--cloud-only`: reuse | X35 (20 d): ok, plus `--lan-only`: reuse | gap |
| 31 Dec activation | Lane A (2x), X01, X04, X08, X10, X12, X13: ok (X08 BROKEN) | gap: X34 starts 03-20, so it activates late on 03-20, not on 31 Dec. Reuse: X04 `--cloud-only`, or move X34 to start 12-29 | gap (same for X35) | gap |
| 1 Apr switch | Lane A (2x; 2091-04-01 is a Sunday with the audit, 2092-04-01 a Monday with the weekly report), X02, X03, X05, X07, X15, X16, X18, X19, X29: ok (X09, X14 BROKEN) | X34: ok | X35, X36: ok | gap |
| No internet, 1 night | X02, X03 (switch day): ok. Normal night: gap (minor) | reuse X02 `--cloud-only` | reuse X02 `--lan-only` (the clock proof still gates the switch: worth running) | - |
| No internet, 3 nights | gap (X01 is about 1.5 days: 12-31 to 01-02) | gap | gap | - |
| No internet on 31 Dec / 1 Apr | X01 / X02, X03: ok | reuse | reuse | - |
| Server off 1-3 days | X04 (31 Dec), X05 (1 Apr), X06 (mid-overlap): ok; 1 day: X07 (partly) | reuse X04/X05 `--cloud-only` | reuse X04/X05 `--lan-only` | gap |
| NAS off / unreachable | X08, X09, X14: BROKEN (H2) | n/a | gap (after H2: reuse X09 `--lan-only` = switch with no copy anywhere) | - |
| NAS not ready after boot / WoL fails | gap (knob `seams.wake_result` exists, no scenario uses it; the box had a real case on 23 Nov) | n/a | gap | - |
| Source or new-FY folder missing | X10, X11, X12, X25, X26, X27, X28; G1 wipe X24: ok. Current-FY root folder missing: gap (minor) | gap | gap | - |
| Mass delete / shrink / wipe | X22, X23, X24: ok | gap (low) | gap (low) | - |
| Locked file / long paths | X40, X41: ok | - | - | - |
| Disk full | X42: gated NR | - | - | - |
| SMTP down | X44: ok | - | - | - |
| DNS down | gap: the `set_cloud("dns")` variant exists but no scenario uses it | - | - | - |
| Clock wrong | X13 (+1 h on activation), X02 (unproven): ok. X45 (backward) and X46 (+13 months): NR | - | - | - |
| Double start / reboot mid-backup | X37, X38, X39: NR. Mid-rollover crash X17: NR | NR | NR | - |
| Weekly audit, weekly + monthly reports on special days | Lane A (Sun 31 Dec audit = F-E1, Mon 1 Jan monthly, Sun 1 Apr audit, Mon 2092-04-01 weekly): ok. X47 (reports during an outage): NR, but reuse lane A with `--outage` over a Monday 1st (e.g. 2091-10-01) | Sundays inside X34: ok | Sundays inside X35: ok | gap |
| Retention over 728 days | Lane A (production 90), A730 (`--retention-days 730`): ok. R35 (db 35 / run_logs 7): gap, the CLI has one retention knob only | - | - | - |
| DST | not needed (IST) | | | |

**NOT_RUN by decision:** X17, X37, X38, X39, X45, X46, X47, X48 (drop it); X42 is gated. Of these, only **X39 (kill mid-sync, then LAN_ABANDONED)** is worth wiring. A power cut or reboot during the 21:01 LAN run is a likely real event, and the knobs already exist (`rig.kill_child_after` / `fire_step` / `kill_lane_processes`). X17 is similar but rarer. X37 and X38 are covered by the Prefect slot and the existing tests, so skip them. X47 is covered by reusing `--outage`.

**Real gaps worth closing** (all are reuse or small parameter changes, no new scenario packs):
1. H2 (S15 TEST-NET): unblocks X08, X09, X14.
2. M1 (`--cloud-only` / `--lan-only` set the config, plus a both-off option): gives C, L and O for X02, X04, X05 and calendar lanes over 31 Dec and 1 Apr.
3. A no-internet-for-3-nights variant: X01 with the `blackhole` days extended to 3, and a DNS variant (`_cloud("dns")`).
4. NAS not ready / WoL failure: one entry that sets `seams.wake_result = False` for one night.
5. X39 (owner decides).

**Minimum first set on the box** (at most 3 lanes in parallel):
1. L11 `--days 0`.
2. L12 smoke: a 14-day calendar lane from 2090-12-25 (crosses activation, a Sunday, a Monday, and the 1 Jan monthly report).
3. Lane A, 728 days, both legs.
4. X01, X02, X04, X05 (no internet and server off over 31 Dec and 1 Apr, both legs).
5. X34 and X35 (after M1, re-run with the flag), X09 (after H2), X44, X24, X40.
6. Then A730, and the C/L reuse runs of X04 and X05.

## 3. Dev evidence: wrong or misleading

1. **X02 is PASS while its lane has 51 FAIL checks; X03 has 53** (`agent_runs/livebox_dev51/w1_x02`, `livebox_dev52/w1_x03`). The day-override fix was applied to X01 only. X02 and X03 have no `overrides`, so the calendar expects a normal switch day. The catalog verdict ignores oracle FAILs (`run_lane.py:709-714`), so the exit code is 0. X03's scenario FAIL ("no ROLLOVER_BLOCKED row on 2091-04-01") is unclassified. Diagnose it like X01 before anyone calls it a product finding.
2. **`expected_red` turns any failure into FINDING** (`scenarios/results.py:375-376`). This affects X01, X09, X18 and X36: an unrelated regression would be reported as the known finding. It should only change the failures that name that finding.
3. **Some checks pass on empty evidence.**
   - `created_paths`, `junction_target_entries` and `switch_before_manual_lan` are never filled in (only initialised at `run_lane.py:988-990` and written at 2053-2055).
   - `lan_runs_0600` counts LAN events at 06:00, which the driver never creates (`results.py:296-298`).
   - So parts of X10, X11 and X12, and all of X19 and X36, pass with nothing observed.
4. **`make_report` does not look at whether a lane finished.** It re-judges `verdict_for` on whatever evidence exists (`make_report.py:154-175`). An aborted lane (rv3s X08, S15) or an empty one (dev50: `lane_root "C:Users..."` was mangled by Git Bash, `safety: []`) can show PASS. The report should read `result.json` and the safety refusal, and print ABORTED.
5. **dev54 X01 = FINDING, but its 3 remaining FAILs (12-31 FLOW_STATE, ROWS:AUDIT, MAIL) are the Sunday-audit finding F-E1.** They are still labelled FAIL because `apply_known_findings` maps P1 only (`run_lane.py:450-458`).
6. **"A41 249/249 PASS" says nothing about special days.** It covers 12 days from 15 to 26 Dec: no activation, no switch, no 1st of the month. It ran on the dev PC, where S10, S11 and S14 are skipped (`--allow-dev-path`), :4200 is down (H1 cannot be seen), and w32tm fails (L2). It is not a predictor for the box.
7. **Wake 9 / shutdown 7 in X01** matches F-NEW-WAKE-PER-PASS (one wake per FY pass). That is consistent, not a harness error.
