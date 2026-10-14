"""Generate audit_lab/LEDGER.md from the raw evidence + the analysis text below.

Usage:  .venv\\Scripts\\python.exe audit_lab\\make_ledger.py
Re-runnable: every experiment appends to %TEMP%\\opencode\\evidence.jsonl.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

EVID = Path(os.environ.get("TEMP", ".")) / "opencode" / "evidence.jsonl"
OUT = Path(__file__).resolve().parent / "LEDGER.md"

ANALYSIS = r"""
---

## V. What the lab PROVED (my audit hypotheses confirmed)

| # | Hypothesis before the lab | Empirical result | Severity |
|---|---|---|---|
| **D1** | An empty / dirs-only source wipes the mirror and reports success | **CONFIRMED on BOTH legs.** robocopy `/MIR` → 12 destination files → **0**, exit 2 → `LAN_COMPLETE`; rclone `sync` → all objects deleted, exit 9 → **`CLOUD_NO_CHANGES_COMPLETE`** (also counted as a *success* by `last_successful_run()` and the reports). The only reason this wasn't already happening: robocopy dies earlier on `/ZB` (see below). `check_source_drive()` returns **OK** because it only checks that the source root has ≥1 entry. | **CRITICAL** |
| **ZB** | *(not in the original audit)* `/ZB` needs `SeBackupPrivilege` | **CONFIRMED as a deployment blocker.** With `/ZB`, a completely ordinary, fully-readable copy fails with `ERROR : You do not have the Backup and Restore Files user rights.` and **exit 16** before a single byte is copied. `whoami /priv` on this account shows no `SeBackupPrivilege` at all. Same command **without** `/ZB`: exit 1 (success). This also breaks 5 tests in the repo's own suite and is the reason the LAN leg looked "protected" in audit pass 1. | **CRITICAL — day 1** |
| **B** | The daily `--size-only` gate cannot see same-size content changes | **CONFIRMED.** A cloud copy with the *same size and same mtime* but different bytes: daily gate → `verified=True`; weekly hash audit → `VERIFICATION_FAILED`, `content-changed=1 [Clients/ledger.txt]`. The daily gate is an *accounting* check, not an integrity check — and the dashboard labels it "Verification Passed". | **HIGH** |
| **BUG-1** | `watchdog._alert_wedged_lock` is dead code | **CONFIRMED.** `find_spec('core.notifications') = None`; the call logs `No module named 'core.notifications'` at DEBUG and returns `None`. The wedged-lock alert is *never* delivered. | **HIGH** |
| **BUG-2** | A failed run is recorded with a success status | **CONFIRMED (narrower than I thought).** Cloud variant is already handled (`flow.py:661` rewrites `CLOUD_VERIFY_FAILED`). LAN variant confirmed: a **post-sync** exception (e.g. the DB write) leaves `run_history.status = LAN_COMPLETE` while the run raises, and `last_successful_run()` returns it as a success. The existing suite never sees this because it patches `flow._record_run`. | **MEDIUM** |
| **ALERT-2** | *(found by WAVE 6)* one failed backup = one alert email | **REFUTED — a verify-gate failure PAGES TWICE.** `_run_cloud_pipeline()` sends the specific gate alert (`Cloud integrity verification FAILED after sync…`) and *then* raises a plain `RuntimeError`, so `backup()`'s "Backup completed with errors" generic alert fires as well (only `PartialRun` suppresses it). Two emails per event. | **MEDIUM** (alert fatigue) |
| **R5** | Manifest/snapshot memory blows up at 1M+ files | **CONFIRMED and quantified.** 200k objects: +108 MB RSS, 2.2 s, 24 MB pickled per task result. At 1M objects the cloud leg holds **~1.05 GB of live Python objects** and writes **~102 MB into the Prefect result store per night** (2.5M → ~2.6 GB). Times are fine (lsjson 1.9 s @200k → ~10 s @1M, far inside the 900 s timeout). | **MEDIUM** (boxes with <8 GB) |
| **F-08/LAN** | A killed robocopy with exit code 0 is caught | **CONFIRMED.** `TerminateProcess(robocopy, 0)` at 1207/4000 files → `LAN_SUSPECT`, `log_complete=False`, forensic log retained. The LAN fail-closed contract holds. | none |
| **F-08/CLOUD** | A killed rclone with exit code 0 is caught by the *gate* | **CONFIRMED live.** `run_cloud_sync` itself returns `CLOUD_COMPLETE` (it cannot tell) — but the full pipeline: `CLOUD_VERIFY_FAILED`, no false success, **1 alert sent**. The design is sound, but it depends *entirely* on the gate. | none |
| **D2** | An unreadable source subtree is purged from the destination | **REFUTED (both legs protected).** rclone: `failed to open directory "B": Access is denied` + `not deleting files as there were IO errors` → exit 1, objects survived. robocopy: exit 9 → `LAN_PARTIAL`, subtree survived 5/5. | — |
| **CORRUPT** | Corrupt/lock-protected lock files crash the reader | **REFUTED.** 13 hostile payloads (negative PID, `1e9:1e9`, `999999999999999999999`, `1:1:2`, empty) → no exceptions; PID-reuse correctly detected; `acquire_lock` refuses a live foreign owner and collects a stale one. Concurrent DB *inserts* serialise 50/50. | — |
| **ORCH** | *(found by WAVE 8)* the orchestration layer behaves like production | **CONFIRMED end-to-end.** `serve.deployments()` registers all 6 deployments on a real Prefect server with the config's cron+timezone; `arun_deployment("aam-backup/backup-lan")` (the dashboard's exact call) creates a run that the duplicate-click guard then *refuses to duplicate* (400 `already_running`); with the Prefect API unreachable the same endpoint fails **closed** (503) while `/status` degrades to `run_state=unknown`; `launch._cancel_orphaned_runs()` reaps a crashed agent's orphaned PENDING/RUNNING runs but **spares a RUNNING one while its backup lock is live**; two simultaneous backup runs serialise on the real `aam-backup` global limit (queue wait measured, mirror windows never overlap); `weekly-report`/`monthly-report` complete as real flow runs and **contain** an SMTP outage. | — |
| **W3** | The committed suite is green as the guide claims | **REFUTED.** On a clean, unprivileged box: **34 failed / 1632 passed / 199 skipped / 40 errors**, of which **19 failures are code-level and machine-independent** (`test_f2_trigger_remediation.py` references `ui._trigger_result`, `ui._trigger_timeout`, `ui.TRIGGER_WAIT_SECONDS` — none exist in `ui.py`). The rest are environmental: a missing `\\127.0.0.1\lan_backup` share, real-GCS tests, chaos tooling, and `/ZB`. Running their suite also **appends to `docs/SCENARIO_TEST_REPORT.md` and creates a `MagicMock\…` tree in the repo root**. | **MEDIUM** |
| **MINOR** | — | `_clean_path()` is not idempotent (`"0 /"` → `"0 "` → `"0"`). Concurrent `ManifestDB` **initialisation** can raise `sqlite3.OperationalError: database is locked` even with `busy_timeout_ms=8000`, because the legacy dedup DELETE runs *before* `PRAGMA busy_timeout` is set (`core/manifest.py:150-162`). A cloud preflight killed with exit 0 passes preflight. | LOW |

---

## VI. What all of this changes in my earlier ranking

1. **`/ZB` is the single most important new finding.** It is a *guaranteed* LAN failure for the deployment pattern the guide prescribes (service Log On = domain user with share write access) — and it accidentally masks D1 on the LAN leg. Verify `SeBackupPrivilege` (or run as LocalSystem, or drop `/ZB`) **before** the first production run.
2. **D1 must be fixed even though the LAN failure looks "safe"**: the cloud leg already deletes everything and reports `CLOUD_NO_CHANGES_COMPLETE`.
3. **R1 was both right and wrong**: preflight does cover destination-side faults and does email; it cannot see a run that never happens (and the LAN leg can't even start without `/ZB` fixed).
4. **R5 is real but bounded**: ~1 GB at 1M objects. Fine on a 24 GB dev box; a genuine constraint on a 2-core/4-8 GB server.

---

## VII. Reproducing

```powershell
# everything is offline; nothing real is touched
.\.venv\Scripts\python.exe -m pytest audit_lab -q -s --timeout=900
.\.venv\Scripts\python.exe audit_lab\make_ledger.py     # regenerate this file

# only the Prefect orchestration wave (deployments, triggers, concurrency,
# crash recovery, report flows) — runs everywhere, no services needed
.\.venv\Scripts\python.exe -m pytest audit_lab/test_w8_prefect.py -q -s --timeout=1200
```

* Harness rules: all writes under `%TEMP%\opencode\aam_lab`, `audit_lab/labconf.py::assert_lab_path` enforces it,
  `build_config()` never reads `config.yaml`, WoL/shutdown are force-disabled *and* monkeypatched to raise,
  and the fake GCS is an rclone **local-backend** remote named `aam_gcs` (the name the production code hardcodes),
  so `run_cloud_sync` / `verify_cloud_integrity` / `get_cloud_manifest` / `get_cloud_diff` / `audit_cloud` all
  execute for real with zero network and zero cost.
* WAVE 8 additionally starts a **real ephemeral Prefect API server** (`prefect_test_harness()`), creates the
  production `aam-backup` global concurrency limit with `launch._ensure_concurrency_limit()`, registers the
  production deployments with `serve.deployments()` + `Deployment.aapply()`, and drives them with
  `arun_deployment()` — the same call `ui.py`'s "Run Backup" endpoint makes.
* WAVE 7 is the only opt-in wave: it needs the deployed box (`audit_lab\bootstrap_deployed.ps1`, run as
  Administrator). With `AamBackupAgent`/`AamWatchdog` stopped it fails at `_require_services()` /
  `_trigger_and_wait()` (no worker → the flow run stays SCHEDULED) — that is an environment state, not a
  code finding.
"""


def main() -> None:
    rows = [json.loads(l) for l in EVID.read_text(encoding="utf-8").splitlines() if l.strip()]
    latest: dict[str, dict] = {}
    for r in rows:
        cur = latest.get(r["id"])
        if cur is None or r["ts"] >= cur["ts"]:
            latest[r["id"]] = r
    out = [
        "# AAM Backup Automation — empirical audit lab (evidence ledger)",
        "",
        "Everything below was produced by executing the shipped production code against a fully isolated rig",
        "(mock source, loopback SMB `\\\\127.0.0.1\\C$` as the NAS, an rclone *local* backend as the fake GCS bucket).",
        "No production module or test was edited; only `audit_lab/` was added.",
        "",
        f"**{len(rows)} experiments recorded ({len(latest)} distinct).** Raw log: `%TEMP%\\opencode\\evidence.jsonl`.",
        "",
        "---",
        "",
        "## Index",
        "",
        "| ID | Verdict (latest run) |",
        "|---|---|",
    ]
    # the index shows the LATEST verdict per experiment (earlier rows may
    # predate a lab fix and would misrepresent the current state)
    for eid in dict.fromkeys(r["id"] for r in rows):
        v = latest[eid]["verdict"].replace("|", "\\|")
        out.append(f"| `{eid}` | {v[:170]} |")
    for eid in dict.fromkeys(r["id"] for r in rows):
        r = latest[eid]
        out += [
            f"### `{r['id']}` — {r['hypothesis']}",
            "",
            f"- **Verdict:** {r['verdict']}",
            f"- **When:** {r['ts']}",
            "",
            "```",
            r["evidence"][:2600],
            "```",
            "",
        ]
    out.append(ANALYSIS.strip() + "\n")
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {OUT} ({len(rows)} evidence rows)")


if __name__ == "__main__":
    main()
