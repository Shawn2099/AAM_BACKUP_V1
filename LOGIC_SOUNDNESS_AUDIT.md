# Logic-Soundness Audit — `core` package

**Date:** 2026-10-13
**Scope:** `core/*.py` (4,834 LOC) — decision/classification logic, verdict
gates, path algebra, lock handling, config guards.
**Rule of engagement:** *no production code was changed.* This audit only
runs tools and adds tests. Every claim below is pinned by a test in
[`tests/test_logic_soundness_audit.py`](tests/test_logic_soundness_audit.py)
(177 tests, all passing) that asserts the **actual** behavior, so a future
change to that behavior fails the suite.

---

## 1. Method

| Tool | Purpose | Result |
|---|---|---|
| `ruff` (B, SIM, PIE, RET, C4, PLR, PLW, TRY, ARG) | logic/ambiguity lint | 1 unused import, 1 unused lambda arg, plus style (too-many-args/branches) |
| `vulture` | dead code | `lan_preflight.py:34` unused `retry_state`; `lan_sync.py:327` unused import `_Path` |
| `radon cc` | complexity hotspots | 18 blocks > threshold; worst `integrity._run_rclone_check` **F(50)**, `report.generate_report_html` **E(33)**, `cloud_reporter.get_cloud_diff` **D(21)** |
| `mypy` | type soundness / ambiguity | 11 errors (see §4) |
| `bandit -ll` | security/logic | 7× `B608` (interpolated SQL in `manifest.py`) — all allow-list guarded (pinned) |
| `pytest` + `hypothesis` | behavioral verification | 177 new tests (incl. 18 property tests) |

Full-suite cross-check: **111 failed / 1043 passed / 19 errors** with the new
file vs **111 failed / 883 passed / 19 errors** without it — the same 111
pre-existing failures, so the audit adds **no new failures and masks none**.

---

## 2. Findings (no fixes applied — behavior is only *characterized*)

Severity: **High** = fail-open on a safety gate; **Med** = ambiguity an
operator/reader cannot resolve from source; **Low** = hygiene/readability.

### H-1 · Source-drive low-space gate silently skipped — *Medium*
`core/health.py::check_source_drive` — when `shutil.disk_usage()` raises
`OSError`, the exception is swallowed, only a warning is logged, and the
function still returns `(True, "")`. A drive that is nearly full but whose
usage cannot be read **passes the health gate**.
→ `TestCheckSourceDrive::test_disk_usage_oserror_is_swallowed`

### H-2 · Clock-skew check reports "healthy" when Google is unreachable — *Medium*
`core/health.py::check_clock_skew` — network/parse failures return
`(True, "")` ("skipping"). A genuinely skewed clock combined with an
unreachable `googleapis.com` is therefore treated as fine, and the later
GCS JWT auth is the only thing that will fail.
→ `TestCheckClockSkew::test_network_error_reports_healthy`,
`::test_unparsable_date_reports_healthy`

### H-3 · Redundant guard after a fail-closed raise — *Low*
`core/health.py::pre_backup_health` — `if not gcs_key_path: raise` is
immediately followed by `if gcs_key_path:`. The second condition is always
`True`; `check_gcs_key` can never be skipped in cloud mode. Misleading, and
suggests an intended "optional key" path that does not exist.
→ `TestPreBackupHealth::test_cloud_always_validates_gcs_key`

### L-1 / F-2 · Dead success disjunct in FY-rollover LAN result — *Low (latent)*
`core/fy_rollover.py::run_final_backup` grants LAN success when
`lan_status == "LAN_PARTIAL" and not (exit_code & 8)`. But **every** producer
of `LAN_PARTIAL` (`decide_lan_result`, `classify_exit_code`) sets bit 3
(`code & 8`), so the `and` clause is *always false*. The branch is
unreachable; the guard is redundant. Proven by an exhaustive + property
invariant.
→ `TestClassifyExitCode::test_partial_implies_bit3`,
`TestDecideLanResult::test_partial_implies_bit3_here_too`,
`test_run_final_backup_lan_partial_guard_is_unreachable`

### L-2 · Job-summary row silently truncated past 6 columns — *Low*
`core/lan_sync.py::summarize_log_counts` builds its dict with
`dict(zip(_SUMMARY_COLUMN_NAMES, values))`. With no `strict=True`, a row of
7+ numeric columns keeps the first 6 and **discards the rest without
error** (rule flagged by `ruff B905`).
→ `TestSummaryFilesRow::test_extra_columns_silently_truncated`

### L-3 · Bit-4 verdict evaluated before log completeness — *Low (documented)*
`decide_lan_result` returns `LAN_FAILED` for a bit-4 code even when the log
is truncated; truncation only yields `LAN_SUSPECT` when bit 4 is clear.
Intentional, but the ordering is a precedence subtlety worth pinning.
→ `TestDecideLanResult::test_bit4_wins_over_truncated_log`

### M-1 · Unreadable LAN share can report an empty inventory — *Medium*
`core/lan_manifest.py::walk_lan_destination` decides "root failed" via
`OSError.filename in (unc_path, str(Path(unc_path)))` — an exact string
match. If the error's `filename` differs by even a trailing separator, the
"must be loud" contract fails open and the function returns `[]`, which is
indistinguishable from a genuinely empty destination.
→ `TestWalkLanDestination::test_root_error_raises`,
`::test_root_error_with_odd_filename_does_not_raise`

### I-1 · Unknown status char in `--combined` diff is silently dropped — *Low (fail-open)*
`core/integrity.py::_parse_combined_file` classifies only `+ - * = !`.
Any other leading status character is ignored entirely — neither a mismatch
nor an error — so an unrecognized rclone status cannot surface as a failure.
→ `TestParseCombinedFile::test_all_prefixes`

### I-2 · Cloud capability label overstated on the failure path — *Low*
`core/integrity.py::audit_cloud` unconditionally rewrites
`capability` to `HASH_GCS_ADVERTISED` whenever the local label is present —
including the exception path where no GCS hash capability was exercised (the
`detail` string has no label to rewrite). The recorded capability can claim
more than actually ran.
→ `TestAuditCloudCapability::test_relabel_even_when_detail_has_no_label`

### B-1 · `_clean_path` is not idempotent on the first application — *Low*
`core/backup_repository.py::_clean_path` reaches a fixed point only on the
second pass (a trailing separator is dropped on the next call). Callers that
normalize once can hold a value that changes on re-normalization.
→ `TestCleanPath::test_fixed_point_within_two_passes`

### B-2 · Doubled separators normalize inconsistently by position — *Low*
A **leading** doubled separator survives as `//` (`"\\a\\b" → "a//b"`), while
an **internal** doubled separator is collapsed (`"a\\b" → "a/b"`). Same
physical path shape, two different results.
→ `TestCleanPath::test_duplicate_separators_not_collapsed`

### C-1 · `get_cloud_diff` returns a mixed-type mapping — *Low*
`core/cloud_reporter.py::get_cloud_diff` returns `added/removed/modified/
unchanged` as lists but `_partial`/`_error` as `bool`/`str`. Consumers that
iterate values or assume "all values are lists" break on the partial path.
(mypy flags the same heterogeneity.)
→ `TestCloudReporterErrors::test_diff_partial_has_heterogeneous_extra_keys`

### F-1 · Rollover can be driven solely by the LAN path — *Medium*
`detect_rollover` uses `_fy_name(source) or _fy_name(lan)` and
`run_final_backup`/`rollover` use `old_fy = src_fy or lan_fy`. A deployment
whose **source** has no FY suffix but whose **LAN** destination carries an
old FY triggers rollover and fabricates a new source FY folder from a
non-FY root. (Config cross-field validation blocks the *mismatched-both*
case, but not the source-missing case.)
→ `TestDetectRollover::test_source_missing_fy_but_lan_has_old_fy`

### P-1 · Unreadable lock file is assumed live — *Low (documented fail-safe)*
`core/process.py::read_lock_alive` maps `PermissionError` to `(True, -1)`.
Intentional (AV-held files), but it means a lock that can never be read also
can never be reclaimed by this path.
→ `TestReadLockAlive::test_permission_error_treated_as_live`

### MAN-1 · Interpolated SQL identifiers are allow-listed — *Verified sound*
`bandit` reports 7× `B608` in `manifest.py`. Every interpolated identifier
(`mode`, `status_field`) is validated against a fixed set before use, so the
interpolation cannot carry user input. Pinned so a future refactor cannot
drop the guard silently.
→ `TestManifestModeGuards` (17 tests)

---

## 3. Complexity hotspots (`radon`) — candidates for focused review
| Block | Score |
|---|---|
| `integrity._run_rclone_check` | **F (50)** |
| `report.generate_report_html` | **E (33)** |
| `cloud_reporter.get_cloud_diff` | **D (21)** |
| `backup_repository.record_sync_results` | C (18) |
| `fy_rollover.run_final_backup` | C (16) |
| `cloud_verify.decide_cloud_verify_result` | C (15) |
| `zen`/`lan_sync.decide_lan_result`, `lan_sync.run_lan_sync`, `cloud_sync.run_cloud_sync` | C (13) |

`integrity._run_rclone_check` (F = 50) is the single highest-risk block: it
concentrates the VERIFIED/VERIFICATION_FAILED verdict, capability labelling,
canary filtering, and completion-evidence checks. Its branches *are* now
covered (see `TestRunRcloneCheck`), but the function is a refactor candidate.

---

## 4. Type-soundness ambiguities (`mypy`)
| Location | Issue |
|---|---|
| `manifest.py:182-183` | `self._conn` typed `None` but returns `Connection` |
| `backup_repository.py:63` | `set[str \| Any \| None]` passed where `set[str]` expected |
| `cloud_reporter.py:192,221,223` | `diff` inferred `dict[str, list]`, later assigned `bool`/`str` |
| `report.py:35,357,390,399` | `None` default for `list`/`str` params |
| `fy_rollover.py:87` | `PurePosixPath` assigned to a `PureWindowsPath` variable |

None are runtime crashes on the exercised paths; they are genuine
"two types share one name" ambiguities that a reader cannot resolve.

---

## 5. What is NOT covered (honest limitations)
- No **mutation testing** (`mutmut`/`cosmic-ray`) was run: it would modify
  project config and is slow on Windows. Recommended as the next step to
  measure whether the *tests* (not just the code) are strong.
- The 4 collection-broken test files and the 111 pre-existing failures were
  left untouched (outside this request).
- `report.py` email/HTML rendering and `cloud_sync.run_cloud_sync`'s live
  rclone orchestration were read but not exhaustively characterized here.
- Coverage is line-only (no branch coverage flag enabled).

## 6. Reproduction
```
# static
uvx ruff check core/ --select B,SIM,PIE,RET,C4,PLR,PLW,TRY,ARG
uvx vulture core/ --min-confidence 70
uvx radon cc core/ -s -a -nc
uvx mypy core/ --ignore-missing-imports --python-version 3.12
uvx bandit -r core/ -ll -q
# behavior
.venv/Scripts/pytest tests/test_logic_soundness_audit.py
```
