# Test plan — assertion conversion + Wave 12 (fake clock) + Wave 13 (branches) + Wave 14 (ui/reporting)

**Date:** 2026-10-10 · **Author:** audit session (continuing from Wave 11) · **Status:** APPROVED BY REVIEWER — implementing.

Reviewer decisions recorded 2026-10-10:
1. **No defect-pinning marker. No all-green sanitising.** This is a journey to reveal every break; a red that says where we break is the *product*. Every assertion below is plain, and where the shipped behaviour is defective the assertion encodes the defect (exactly like the intentional M-6 red in `test_w9_stress.py`). Fixing the code later makes the test red until the test is updated — that is the desired signal.
2. **Wave 12 drives everything through the real Prefect flows** — `flow.backup()` for every one of the 8 nights, `flow.rollover_check_flow()` for the 1-April event, `flow.integrity_audit_flow()` for the audit night, each as a real flow run inside `prefect_test_harness()` with the production `aam-backup` concurrency limit created by `launch._ensure_concurrency_limit()`. No direct `_run_*_pipeline` calls in the cycle. (The deployment-API route — `arun_deployment` + a worker subprocess — is already proven by Wave 8's `W8.concurrent_serialise` / `W8.dashboard_trigger`; it is not duplicated here.)
3. **A10 is strict**: `rows == 50 and not errors` for both rounds. It fails whenever concurrent `ManifestDB` opens lose rows — that is the M-6/L-3 defect surfacing, which is the point. Same policy as the W9 stress test.
4. **Wave 13 stays on the wrapper/parsing surface**; **ui.py + reporting become Wave 14**.

Ordered by the value ranking in `HANDOVER.md` §8.

Nothing below edits production code. All new code lives in `audit_lab/` (lab surface) and this doc.

---

## 0. Inventory I actually found

I enumerated every `test_*` function in `audit_lab/` and counted `assert`/`pytest.raises` nodes. **23 tests have zero assertions** — more than the "~10" in HANDOVER §8, because Wave 2/2b (cost measurement) and Wave 5/10 documentation tests also record verdicts without enforcing them.

Split into three tiers:

| Tier | Meaning | Count | Action |
|---|---|---|---|
| **A — load-bearing** | the recorded verdict *is* a fail-closed contract or a defect signature. A green run today means nothing if nobody enforces it. | 15 | convert to real assertions |
| **B — measurement** | W2/W2b cost benchmarks. "Green" means nothing because they have no threshold at all. | 2 | add regression ceilings + correctness assertions |
| **C — deliberate documentation** | blind-spot maps (W10.3 line coverage), design notes (W5c smb probe, L-5). Recording *is* the experiment. | 6 | keep assert-free, but assert the *observation* that makes the note true (e.g. tracer produced data) |

Full list with the exact assertion per test is in §1.

---

## 1. Tier A — assert-free → asserted (15 tests, edited in place)

Each row: the test, the evidence id it records, and the invariant to assert. "Pins defect" = the assertion encodes a current *defect* (M-2, L-1, L-5, C-2, the W5c source-loss case), so it goes red the day someone fixes it. **This is intentional and desirable** — but it needs a marker so you can still get a clean baseline run.

| # | File / test | Evidence | Invariant to assert | Pins defect |
|---|---|---|---|---|
| A1 | `test_w1a_diag.py::test_w1a_diag_robocopy_exit16_reason` | `D1.robocopy_exit16` | `res["status"] in {"LAN_FAILED","LAN_SUSPECT"}` **and** destination file count unchanged (`survivors == 1`) — a `/ZB` abort must never delete | C-2 |
| A2 | `test_w1a_diag.py::test_w1a_d2_partial_readability_both_legs` | `D2.partial.lan` / `.cloud` | both legs: `B survivors == 5/5`, `A == 2/2`, LAN status != `LAN_COMPLETE`, cloud status != `CLOUD_COMPLETE` | — |
| A3 | `test_w1a_purge.py::test_w1a_d2_unreadable_subtree_purges` | `D2[maxpath].result` | deep/unreadable subtree survives (5/5) and `status != LAN_COMPLETE` | — |
| A4 | `test_w1c_controls.py::test_w1c_c3_partial_source_without_zb_purges` | `C3.denied_subtree_no_zb` | `b_left == 5` and `decide_lan_result(...)["status"] in {"LAN_PARTIAL","LAN_FAILED"}` | — |
| A5 | `test_w1c_controls.py::test_w1c_c4_denied_file_with_production_command` | `C4.denied_file` | destination copy exists and still holds `b"OLD COPY"` (exit-8 path retains the previous good copy) | — |
| A6 | `test_w1bd.py::test_w1d_kill_lan_with_exit_code_0` | `D.kill_exit0.lan` | `res["status"] == "LAN_SUSPECT"` and `log_complete is False` and the forensic log was retained | — |
| A7 | `test_w1bd.py::test_w1d_kill_cloud_with_exit_code_0` | `D.kill_exit0.cloud` | transfer-level contract: `run_cloud_sync` returns a *non*-`CLOUD_COMPLETE`-implying verdict **or** the landed object count < source count (kill actually happened). Assert the kill really happened first: `killed is True and landed < 3000`, otherwise the test is vacuous | — |
| A8 | `test_w1d2.py::test_w1d2_kill_lan_exit0_without_zb` | `D2.kill_exit0.lan` | `res["status"] == "LAN_SUSPECT"`, `termination == "abnormal"`, `copied < 4000` (proof the kill was mid-transfer) | — |
| A9 | `test_w1e_bugs.py::test_w1e_bug2_lan_post_phase_exception_records_complete` | `BUG-2.lan_status` | `exc is not None` **and** `last_successful_run("lan") is not None` and its status == `"LAN_COMPLETE"` — the M-2 misreport signature | M-2 |
| A10 | `test_w1e_bugs.py::test_w1e_two_processes_concurrent_db_writes` | `LOCK.db_concurrency` / `.inserts_only` | `rows == 50 and not errors` (both rounds). Today the losing case *can* legitimately produce `errors`, so this test needs the same treatment as the M-6 stress test: keep it loud, assert the **root-cause signature** (`errors` non-empty ⇒ traceback contains `manifest.py` DDL `executescript`), so the red is always the M-6/L-3 shape and never an unrelated flake | M-6 / L-3 |
| A11 | `test_w5a_paths.py::test_w5a_path_variants_through_lan_sync` | `W5a.path_variants` / `.cloud` | `missing == []` **and** `missing_cloud == []` — zero silent file loss for hostile-but-legal names, on both legs | — |
| A12 | `test_w5b_scenarios.py::test_w5b_junction_in_source` | `W5b.junction.lan` / `.cloud` | `nested == 0`, `copied >= 20`, `via_junction == 0` | — |
| A13 | `test_w5b_scenarios.py::test_w5b_crash_then_recovery` | `W5b.crash_recovery` | `out["status"] == "LAN_COMPLETE"`, `on_nas == 4000`, and no stale lock left (`read_lock_alive(...) == (False, …)` or the lock was collected) | — |
| A14 | `test_w5b_scenarios.py::test_w5b_double_trigger` | `W5b.double_trigger` | `len(runs) <= 2` and destination file count == 400 (no interleaving, no double-copy) | — |
| A15 | `test_w5c_ops.py::test_w5c_source_disappears_mid_run` | `W5c.source_disappears` | **pins the issue**: `res["status"] == "LAN_COMPLETE"` while the source was gone and the destination kept 3000/3000. Assert exactly that, with a comment that this is the finding to re-open when fixed | L-5 / M-2 family |

Two more that are already partly asserted but whose *recorded* finding is not enforced:

| # | File / test | Evidence | Invariant to assert | Pins defect |
|---|---|---|---|---|
| A16 | `test_w5c_ops.py::test_w5c_missing_canary_fails_and_alerts` | `W5c.missing_canary` | `exc is not None`, `len(alerts) == 1`, `rows[0]["status"] == "LAN_FAILED"`, destination files == 0 | — |
| A17 | `test_w5c_ops.py::test_w5c_locked_file` | `W5c.locked_file` | **pins L-1**: `kept is False and dest files == 200/200` (the locked file was purged, not retained) | L-1 |
| A18 | `test_w6_flow.py::test_w6_integrity_audit_flow` | `W6.integrity_audit_flow` | `audits[0] is not None`, its `status == "VERIFICATION_FAILED"`, and `len(alerts) == 1` | — |

### Latent lab bug to fix while converting (lab files only)

`test_w1bd.py::test_w1d_kill_lan_with_exit_code_0` is **currently vacuous on this box**: it builds the production command *with* `/ZB`, so robocopy exits 16 before copying, `pid` stays `None`, and the "kill" never happens. It also calls `_terminate_with_code_0(pid)` — a name that does not exist (`_terminate_exit_0` is the real one), so the first time a PID *is* found the test raises `NameError` instead of testing anything. The recorded `D.kill_exit0.lan` verdict ("FAIL-CLOSED BROKEN: status=LAN_FAILED exit=16") is therefore an artefact of C-2, not of the kill contract.

Fix (lab-only): strip `/ZB` via the existing `L.strip_zb` seam (as `test_w1d2` already does) and correct the helper name. This is exactly the class of defect HANDOVER §7 warns about — a green wave that tests nothing.

---

## 2. Tier B — measurement tests get ceilings (2 tests)

W2/W2b measure but never fail, so "80 tests pass" says nothing about M-3/R5.

| Test | New assertions (regression ceilings, generous but real) |
|---|---|
| `test_w2_scale.py::test_w2_scale_manifest_and_snapshots` | `len(manifest) == N`; per-object RSS growth `< 1.0 KB/object`; `lsjson` wall `< 60 s` at 200k (config `manifest_timeout_seconds=900` ⇒ huge headroom, so this only fires on a real regression); pickled manifest `< 30 MB` at 200k (⇒ the ~102 MB/night M-3 number stays a fact, not a drift) |
| `test_w2b_costs.py::test_w2b_lan_and_db_costs` | `len(before_dict) == N`; insert `< 60 s` at 100k; read `< 30 s`; per-object RSS `< 1.5 KB` |

## 3. Tier C — documentation tests stay assert-light but prove they ran (6)

| Test | Asserts |
|---|---|
| `test_w10_frameworks.py::test_w10_production_line_coverage_during_real_run` | the tracer recorded data for every mapped module (`len(hit) > 0` for each of the 9 files) — otherwise a silent tracer failure looks like a 0% blind spot. Plus a **regression floor**: each module ≥ 50% of executable lines, and `core/integrity.py` ≥ 0 with a note that Wave 13 will raise it |
| `test_w5c_ops.py::test_w5c_smb_false_positive` | `_smb_port_open(decoy_port) is True` (the false positive is real) and the pipeline did **not** report success |
| `test_w5c_ops.py::test_w5c_smb_false_positive` sibling — keep `L.record` verdict as DESIGN NOTE | — |
| `test_w6_flow.py::test_w6_backup_flow_failure_alert_policy` | already asserts M-1 (2 alerts) — keep, add a comment that it pins M-1 |

---

## 4. Wave 12 — fake clock (`audit_lab/test_w12_clock.py`, new)

### 4.1 The seam (no production edits)

`pendulum` is imported as a *module object* in exactly two production modules that read the clock: `core/time_utils.py` (`pendulum.now(IST)`) and `core/health.py` (`pendulum.now("UTC")`). Everything else reaches time through `now_iso()` / `get_fy_prefix()` / `cutoff_iso()`, whose bodies resolve `pendulum` from `core.time_utils`' globals at call time. So one seam covers the whole system:

```python
# audit_lab/labclock.py  (new; labconf.py's safety rules stay untouched)
@contextlib.contextmanager
def fake_clock(at, *, modules=("core.time_utils", "core.health")):
    """Replace the `pendulum` module object in the listed production modules
    with a shim that returns a fixed instant. .timezone/.parse/.datetime and
    every other attribute are delegated to the real pendulum, so only the
    clock moves."""
```

The shim returns a real `pendulum.DateTime`, so `.subtract()`, `.isoformat()`, `.format()`, `.in_timezone()` all behave normally. Prefect's own pendulum reference is untouched (different module object), so orchestration timing is unaffected.

### 4.2 Tests

| Id | Test | What it proves |
|---|---|---|
| `W12.fy_boundary` | `get_fy_prefix()` across the IST boundary: 2027-03-31 23:59:59.999+05:30 → `FY26-27`; 2027-04-01 00:00:00+05:30 → `FY27-28`. Plus the **UTC trap**: the same instant expressed as 2027-03-31 18:29:59Z → `FY26-27` and 2027-03-31 18:30:00Z → `FY27-28` (proves the code is IST-anchored, not UTC-anchored — a 5 h 30 m early-rollover if anyone ever changes it) | FY routing, deterministic forever |
| `W12.detect_rollover` | `detect_rollover(src=…/FY26-27, lan=…/FY26-27)` → `False` at 31 Mar 23:59 IST, `True` at 1 Apr 00:00 IST. Plus the opt-out path (no FY in either path → `False` with the warning) and the source/LAN FY disagreement | rollover trigger + G2 opt-out |
| `W12.rollover_flow` | rig config at `FY26-27`, fake clock at **2027-04-01 00:30 IST**, run the real `flow.rollover_check_flow(config_path=…)`: final backup runs, new folders created (source dir + LAN dir **with canary**), `config.yaml` atomically rewritten to `FY27-28`. Then a second call with the clock back at 31 Mar → `NO_ROLLOVER_NEEDED` | the actual 1-April deployment event, reproduced deterministically (W6 does this against the *real* clock, which is why it is fragile) |
| `W12.fy_divergence` | **M-4, now reproducible**: at 1 April with the agent down (simulate by *not* running the rollover flow), run one night of both legs and assert the cloud prefix is `FY27-28` while the LAN destination is still `FY26-27` — i.e. the two FY trees split and nothing complains | the exact risk in M-4, as a pinned signature |
| `W12.clock_skew` | `check_clock_skew()` with a monkeypatched `core.health.http.client.HTTPSConnection` returning a controlled `Date` header: +0 s → ok; +599 s → ok; **+601 s → fail-closed** with the "clock skew" reason; −900 s → fail-closed; missing header → fail-closed; unparseable → fail-open; `OSError` → fail-open. Uses the fake clock so `pendulum.now("UTC")` is exact and the test can never flake | branch-exhaustive coverage of `health.py:83-123` (60% today, zero direct tests) + pins the fail-open design note |
| `W12.purge_retention` | `purge_old_runs(retention_days=90)` under the fake clock: rows at −89 d kept, −90 d kept, −91 d purged, and `file_entries` untouched (the docstring's claim). Also the `VACUUM` branch via `vacuum_freelist_threshold=-1`, and `retention_days=0` → everything goes | the DB-retention leg of the fake-clock theme |
| `W12.seven_day_cycle` | see §4.3 | the headline experiment |

### 4.3 The simulated 7-day nightly cycle

One rig, fake clock advancing one day per night, driving the **production pipelines** (`_run_lan_pipeline` / `_run_cloud_pipeline`, `PREFECT_TEST_MODE=1`) plus one `flow.backup(mode="all")` inside `prefect_test_harness()`:

* nights 1–7 (2027-03-26 … 2027-04-01): each night mutate the source (add a file, modify one, delete one) and assert
  * the destination is **byte-identical to the source on both legs** (sha256, not counts),
  * exactly one `run_history` row per leg per night, all success-looking,
  * `last_successful_run("lan")` / `("cloud")` is *this* night,
  * manifest `lan_status` / `cloud_status` row counts track the source,
  * a re-run of the same night is a no-op (convergent).
* night 8 = **2027-04-01 01:00 IST** (after the boundary): assert the FY split from `W12.fy_divergence`, then run `rollover_check_flow` and assert night 9 both legs agree on `FY27-28`.
* one night drives `flow.backup(mode="all")` to cover the real entry point, the maintenance block and `purge_old_runs`.

Two rig details that the earlier waves got wrong and I will get right (HANDOVER §7):
* the rig must create the **calendar-derived** bucket prefix *from the fake clock* (`bucket_root/lab-bucket/<fake FY>`), not the real one, or preflight fails with "directory not found";
* `flow.backup()` calls `cleanup_orphaned_robocopy_logs(max_age_hours=24)`, which deletes real `%TEMP%\robocopy_sync_*.log` files — i.e. it eats the **W11 log corpus** (11 harvested logs, mtime from earlier today). I will monkeypatch that one function in the W12 rig (documented seam) and leave the real cleanup to Wave 14's redirected-tempdir test.

**Runtime estimate:** ~2–4 min (8 nights × 2 legs on small trees).

---

## 5. Wave 13 — branch-exhaustive wrapper tests (`audit_lab/test_w13_branches.py`, new)

The ~30 parsing sites are our silent-breakage surface; Wave 11 covered 7 of them. This wave finishes the map from `W10.line_coverage` (lan_sync 62%, manifest 56%, health 60%, lan_preflight 68%, cloud_preflight 65%, cloud_verify 73%, integrity 0-145-in-W10).

| Group | Functions | Branches to cover |
|---|---|---|
| **lan_sync** | `_validate_required_flags` | `/NC` and `-NC` raise `ValueError`; the production flag set passes |
| | `build_robocopy_command` | flag contract: `/MIR`, `/XJ`, `/XF .AAM_TARGET_MOUNTED`, `/R:`, `/W:`, `/MT:<mt_threads>`, `/NJH`, `/NP`, and `/ZB` present (pins C-2 as observable) |
| | `decide_lan_result` | **full matrix**: exit ∈ {None, −1, 0…7, 8, 15, 16, 17, 999} × log ∈ {complete summary, no summary, contradictory FAILED markers, summary with failed>0, empty} → the documented status for every cell |
| | `_summary_files_row_values` / `summarize_log_counts` | row absent, short row, non-numeric token, thousands separators `1,234`, CRLF, log cut mid-row → `None`/dict per contract |
| | `_read_log_tail` | file < max, file > max (assert only the tail comes back and it starts on a clean char), missing file, a directory in place of the file → fallback string, never raises |
| | `cleanup_orphaned_robocopy_logs` | `tempfile.gettempdir` redirected to a lab dir: fresh log kept, >24 h log removed, non-matching name untouched, zero files → 0 |
| | `_assert_source_not_empty` | raises on empty source; silent when files exist; silent when the failure reason is *not* "appears empty" (documents that it only guards the empty case) |
| **cloud_sync / cloud_verify** | `classify_rclone_exit` | full code table incl. `9 → CLOUD_NO_CHANGES_COMPLETE` (the C-1 trap) and negatives |
| | `scan_rclone_log_for_errors` | real rclone stderr shapes: critical line, "Failed to delete", pure INFO/bandwidth noise, empty |
| | `resolve_max_duration_seconds` | `None`, `0`, negative, `> timeout`, `< timeout` |
| | `build_rclone_sync_command` | flag contract (sync, `--transfers`, `--checkers`, `--bwlimit`, `--buffer-size`, `--config`, `--log-file`) |
| | `decide_cloud_verify_result`, `_build_error_message` | full exit-code table (extends W11's 5 codes to all of them) |
| | `verify_cloud_integrity` failure paths | rclone missing (patch `resolve_binary`), timeout=1 s, bad config path → fails closed with `verified=False` |
| **integrity** | `audit_lan` | **the W10 blind spot** — clean / missing-from-dest / extra-in-dest / same-size corruption / canary filtered / unreadable source, against the real loopback share (mirrors W11's `audit_cloud` test) |
| | `_parse_combined_file`, `_is_canary`, `_norm_rel`, `new_audit_id` | branch table for the 6-tuple + the `partial` flag; backslash/`./`/leading-slash/case normalisation; canary matching; id format + uniqueness |
| **health** | `check_source_drive` | missing path, dirs-only (C-1), file-only, `PermissionError` on iterdir, `min_free_gb` absurd → exact reason strings |
| | `check_gcs_key`, `pre_backup_health` | missing / empty / valid key; invalid mode; mode=cloud with rclone "missing" → `HealthError` |
| **fy_rollover** | `_fy_name`, `_parent_path`, `_child_path` | table: UNC paths, forward slashes, trailing separators, non-FY names, the 4-digit FY trap, lowercase `fy26-27` |
| | `create_new_fy_folders` | happy path (source + LAN + canary) and the **LAN-offline** path (force the LAN `mkdir` to raise) → must not raise and must return only `source` |
| | `update_config_yaml` | comments preserved (ruamel round-trip), atomic replace, and the **failure** path (unwritable target) → raises and leaves the original file byte-identical with no temp file left behind |
| | `run_archive_transition` | fake `gcloud` shims on PATH: exit 0 → `True`; exit 1 → `False`; sleeping shim with `archive_timeout=1` → `False`; no gcloud anywhere → `False` |
| **manifest / repository** | `insert_run` dedup, `last_run` / `last_successful_run` mode scoping, `wal_checkpoint` | duplicate `run_id` keeps `MIN(id)`; mode-scoped queries never cross legs; WAL truncated |
| | `record_run_history` | returns `False` (not raises) when the DB write fails, and logs at **ERROR** — the M-6 "silent drop" shape, pinned so the log-level half of the fix is caught too |
| | `_migrate_legacy_schema` | a legacy DB (user_version 0, no `extended_metrics`) opens and is stamped; a locked DB raises `ManifestSchemaError` (L-3 root cause, pinned) |
| **lan_manifest / cloud_reporter / report** | `walk_lan_destination`, `snapshot_to_dict`, `diff_snapshots` | UNC + deep paths + canary; added/modified/removed partition |
| | `get_cloud_manifest`, `get_cloud_diff`, `get_cloud_size` | real rclone against the local backend, incl. **empty prefix** (`count == 0`, no division by zero) and a nonzero exit → `CloudReporterError` (never `[]`) |
| | `_csv_safe`, `_generate_csv_data` | CSV-injection prefixes (`=`, `+`, `-`, `@`, tab, CR) neutralised; numeric columns untouched |

**Runtime estimate:** ~3–5 min (mostly real subprocesses on tiny trees; `decide_lan_result` and the parsers are pure).

---

## 6. Conventions I will follow

* **No production edits.** New files: `audit_lab/labclock.py`, `audit_lab/test_w12_clock.py`, `audit_lab/test_w14_branches.py`. Edited: the Tier A/B lab test files, `audit_lab/conftest.py` (export the new helpers + register a `known_defect` marker).
* **Safety rules intact** — every path confined by `labconf.assert_lab_path` to `%TEMP%\opencode\aam_lab`; `build_config()` still never reads the repo `config.yaml`; WoL/shutdown stay force-disabled; `/ZB` still stripped through `labconf.strip_zb`.
* **Evidence**: every new experiment calls `L.record(...)` with a new id (`W12.*`, `W13.*`, `W14.*`) so `make_ledger.py` renders them after the next run. I will **not** regenerate `LEDGER.md` until a full run has appended fresh evidence.
* **The intentional red stays red**: `test_w9_manifest_db_concurrency_stress` is untouched, and A10's replacement asserts the M-6 *signature*, not a green outcome.
* **Known-good baseline**: 59 passed / 2 skipped / 0 failing must be reproduced after Tier A/B (modulo tests converted to pin a defect, which I will list explicitly before running).
* **Long runs detached**, per the ground rules:

```powershell
$p = Start-Process ".venv\Scripts\python.exe" -ArgumentList "-m","pytest","audit_lab","--ignore=audit_lab/test_w7_deployed.py","-q","-s","--timeout=1800","-p","no:cacheprovider" `
     -RedirectStandardOutput audit_lab\raw\run.log -RedirectStandardError audit_lab\raw\run.err -PassThru -WindowStyle Hidden
```

* Wave 7 stays untouched (1069 blocker).

---

## 7. Wave 14 — `ui.py` + reporting (`audit_lab/test_w14_ui.py`, new)

Requested by the reviewer after reviewing the plan. Dashboard and report surface:

| Group | Functions / endpoints | Branches |
|---|---|---|
| **ui** | `_is_running(mode)` | in-flight run seen; no run seen; Prefect API unreachable → `False`/fail-open display (the Wave 8 contract, now unit-level) |
| | trigger endpoints `POST /trigger/lan`, `/trigger/cloud` | 200 queued; 400 `already_running` (duplicate click); **503 fail-closed** when the API is unreachable |
| | `GET /status` | `run_state=unknown`, `running=false` when the API is down (never claims idle) |
| | dashboard render | empty DB (no runs), a `…_FAILED` row, an `integrity_audits` row, an in-sync manifest |
| | `get_db()` lifecycle | holds a `ManifestDB` for the dashboard's lifetime — the M-6 exposure path (open + write while another writer initialises) |
| **report** | `_csv_safe`, `_generate_csv_data` | CSV-injection prefixes (`=`, `+`, `-`, `@`, tab, CR) neutralised; numeric columns untouched; empty run list → header-only CSV |
| | `generate_report_html` | 0 runs, all-failed, success-heavy, audit rows present, `days=1` vs `days=30` |
| | `send_weekly_report` / `send_monthly_report` | SMTP outage contained (Wave 8 already proves the flow level — here the function level: no raise, `False`/logged) |
| | `send_failure_alert` | no recipients → no send; recipients configured with a refusing SMTP → retries then contained |

**Runtime estimate:** ~2–3 min (mostly pure/in-process; the SMTP paths use a local refusing socket).

---

## 8. Decisions taken (from review)

1. Defect-pinning → plain assertions, no marker, no all-green sanitising.
2. Wave 12 cycle → every night through the real Prefect flows.
3. A10 → strict `rows == 50 and not errors` (both rounds).
4. Wave 13 = wrapper/parsing surface; Wave 14 = ui + reporting (new).

## 9. Outcome (implemented)

**Tier A/B/C conversions — done, 21 tests edited in place.** New `W13.exit_code_bool` finding
below. One lab defect fixed on the way (`test_w1bd.py` was vacuous — see §1).

**Wave 12 — done, 7/7 passing**, `audit_lab/labclock.py` + `audit_lab/test_w12_clock.py`:

| Evidence id | Result |
|---|---|
| `W12.fy_boundary` | OK — the flip is anchored to 1 April 00:00 **IST**, and the same instant expressed as UTC (31 Mar 18:30) flips too, so a future UTC refactor fails loudly |
| `W12.detect_rollover` | OK — boundary, the G2 no-FY opt-out, and source/LAN label disagreement |
| `W12.rollover_flow` | OK — `flow.rollover_check_flow()` at 2027-04-01 00:30 completes: final backup, new folders, LAN canary, `config.yaml` → `FY27-28` (deterministic, unlike W6's real-clock version) |
| `W12.fy_divergence` | **M-4 CONFIRMED and reproducible** — agent down across the boundary: cloud wrote `FY27-28` (12 objects), LAN mirrored `FY26-27`, both `…_COMPLETE`, no check crossed the two FYs |
| `W12.clock_skew` | OK — fails closed at +601 s / −900 s / −3600 s and on a missing Date header; fails open on an unparseable header and an unreachable network (the documented trade-off). `health.py:83-123` had zero direct tests before |
| `W12.purge_retention` | OK — 89/90-day rows kept, 91/365 purged, today kept, `file_entries` untouched, `retention_days=0` clears, VACUUM branch does not raise |
| `W12.seven_day_cycle` | OK — 7 nights (26 Mar → 1 Apr) + boundary + converging night 8, **every night through `flow.backup()`**, `integrity_audit_flow()` mid-week (both legs `VERIFIED`), `rollover_check_flow()` at the boundary. Byte-identical on both legs every night, one success row per leg per night, `last_successful_run()` tracks the current night, the re-run is a no-op, 0 alerts, `cleanup_orphaned_robocopy_logs()` called on every night |

**Wave 13 — done, 18/19 passing**, `audit_lab/test_w13_branches.py`.

### New finding from Wave 13

**`W13.exit_code_bool` — `decide_lan_result()` accepts a boolean exit code as success.**
`bool` is an `int` subclass, so `isinstance(code, int)` is not enough. `decide_lan_result(True, log)`
evaluates `True & 16 == 0` and `0 <= True <= 7` → **`LAN_COMPLETE`**, i.e. a bool is classified as
exit code 1. `core/cloud_verify.py::decide_cloud_verify_result()` guards exactly this with an
explicit `isinstance(exit_code, bool)` check; `core/lan_sync.py` does not — the two deciders
disagree on the same input class. Nothing in the shipped code passes a bool today, so this is a
latent robustness hole, not a live failure. The test is **red by design** until the LAN decider
aligns with the cloud one (~1 line).

**Bonus observation from the Tier-A conversion of `test_w5c_missing_canary_fails_and_alerts`:**
`flow.py:947-957` — the LAN pipeline's `except` handler covers `phase` `"sync"` and `"pre"`, but
only *alerts* for `LAN_SUSPECT`. A pre-flight failure therefore raises `LAN_FAILED` with **no
alert of its own**; the operator is paged only by `backup()`'s generic path (`flow.py:1336`).
A direct/manual `_run_lan_pipeline()` call is silent. Recorded as
`W5c.canary_alert_delegation`.

### Final suite result

`97 passed / 2 skipped / 3 failed` (16 min) — the 3 failures are **all intended reds**:

| Failing test | Why |
|---|---|
| `test_w9_manifest_db_concurrency_stress` | pre-existing intentional red — **M-6** (12 of 72 rows lost in 2 of 12 rounds this run) |
| `test_w1e_two_processes_concurrent_db_writes` | **A10, now enforced** — the M-6 root cause surfaced with the traceback at `core/manifest.py:163 executescript(DDL)` |
| `test_w13_bool_exit_code_is_rejected_by_both_deciders` | **new finding** `W13.exit_code_bool` |

Everything else passes: all 7 Wave-12 tests, 18 of 19 Wave-13 tests, all 21 conversions.
(Known cosmetic: Prefect's harness logs `UNIQUE constraint failed: task_run_state` under the
frozen clock — a harness artefact; the flows still COMPLETE, and `labclock.tick` reduces it.)
