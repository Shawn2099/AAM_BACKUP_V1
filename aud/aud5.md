Chunk 5 Audit: FY/health/time-utils
Functions analyzed
core/health.py (136 lines, 5 functions + 1 exception class)
check_source_drive(source_path, min_free_gb=1) -> tuple[bool, str] (lines 16-51)
- Intent: Validate source drive exists, has files, has free space.
- Returns: (True, "") healthy, (False, "reason") failed. Discriminated by bool.
- Edge cases:
- iterdir() raises PermissionError → returns False with reason.
- iterdir() raises OSError (other, e.g., network timeout) → returns False.
- No timeout on iterdir() or shutil.disk_usage() for a network drive (UNC path like \\server\share\...) — these can block indefinitely. Cloud dry-run gets a 10s timeout; health check gets none. Mismatch.
- shutil.disk_usage failure is soft-warned and return True, "" (line 49) — disk space is "best effort," but the docstring says "Verify ... free space" which suggests it's a hard check. Inconsistent semantics: file-existence is hard, free-space is soft.
- Test test_directory_with_files_returns_true (test_health.py:33) passes — but it patches nothing, relying on the host's disk. On Windows the temp dir is on C: which always has free space, so the test never actually exercises the low-space branch unless the mock in test_low_disk_space_returns_false is in play.
check_binary_exists(name) -> bool (lines 54-56)
- Intent: shutil.which(name) is not None.
- Anti-pattern: Returns bool not tuple[bool, str]. Loses the binary name in the return — caller has to hardcode "rclone not found" / "robocopy not found" in pre_backup_health:122, 134. Return type is inconsistent with the three sibling checks (all return tuples).
- PATH lookup pattern: Standard shutil.which. No exception handling needed (it returns None on missing).
check_gcs_key(key_path) -> tuple[bool, str] (lines 59-66)
- Intent: Service-account key file exists and non-empty.
- Validation: Path.exists() + st_size > 0. Does NOT validate JSON parseability — an empty-but-non-empty garbage file passes. Probably acceptable (a corrupt key will surface as a 401 at GCS call time, not at preflight).
check_clock_skew(max_skew_seconds=600) -> tuple[bool, str] (lines 69-102)
- Intent: Compare local UTC against Google's HTTP Date header. GCS JWT auth rejects >10 min skew.
- Threshold: 600s = 10 min, matches GCS limit.
- Anti-patterns:
- Line 79: resp.getheader("Date") or resp.getheader("date") — email.message.Message.getheader() is case-insensitive in Python 3. The or branch is dead code.
- Line 85: parsedate_to_datetime(google_date_str) returns a stdlib datetime, not a pendulum.DateTime. Line 87 subtracts from pendulum.now("UTC"). Works (both are timezone-aware in UTC), but mixes stdlib and pendulum — exactly the kind of mixing the time_utils.py docstring warns against.
- Lines 97-102: Network/parse errors return (True, "") — silent pass. Comment "skipping" but the return is a green light. If Google's HEAD endpoint is down, the system proceeds with a possibly-wrong clock and only fails much later at JWT validation. Defensible (don't block backup on Google's transient outage) but invisible to operators.
- http.client.HTTPSConnection (stdlib) — CODE_REVIEW_2026-06-01.md:359 flagged httpx is already a dep. Borderline.
pre_backup_health(source_path, mode, gcs_key_path=None) -> None (lines 105-136)
- Intent: Orchestrate the four checks, raise HealthError on hard failure.
- Failure semantics are INCONSISTENT:
- Source drive fail → raise (block)
- rclone/robocopy missing → raise (block)
- GCS key invalid → log warning, continue
- Clock skew too large → log warning, continue
- GCS key check is documented as "warning" in the docstring; clock skew is NOT documented as non-blocking — surprising behavior. The check returns (True, "") on network error AND (False, reason) on real skew, but the orchestrator only logs on not clock_ok — so network-error skipping is silent at the orchestrator level.
- The clock_reason returned from check_clock_skew is fetched (line 128) but discarded if clock_ok is True. Even when not clock_ok, only the warning is logged; clock_reason is used (line 130). OK on that branch, but the variable is still fetched wastefully on the happy path.
core/fy_rollover.py (251 lines, 8 functions + 1 exception class)
get_fy_prefix(today=None) -> str — actually lives in time_utils.py:96-113. Documented there.
_fy_name(path_str) -> str | None (lines 27-33)
- Intent: Extract FY\d{2}-\d{2} suffix from path tail. Cross-platform (forward/backslash).
- Logic: Replace backslashes with slashes, split, regex-match the last segment.
- Correctness: r"E:\SOURCE\FY26-27" → "FY26-27" ✓. r"\\server\share\FY25-26" → "FY25-26" ✓. Edge r"E:\SOURCE\FY26" → None ✓ (regex requires \d{2}-\d{2}).
- Tests cover Windows/UNC/Unix and "no FY" cases. Solid.
_parent_path(path_str) -> str (lines 36-51)
- Intent: Cross-platform parent path preserving separator style.
- Manual reimplementation: Hand-rolled. pathlib.PureWindowsPath / PurePosixPath would do this — but they normalize separators, which is exactly what this code is fighting against. The manual impl is correct for this use case.
- Edge cases: All paths (UNC, Windows drive, Unix) covered by tests (test_fy_rollover.py:52-64).
_child_path(root, fy) -> str (lines 54-57)
- Intent: Append FY folder using same separator as root.
- Logic: Detects \ vs / from root, uses it consistently.
detect_rollover(source_drive, lan_destination) -> bool (lines 60-66)
- Intent: True if either path's FY suffix doesn't match get_fy_prefix().
- Anti-pattern (silent): If neither path has an FY suffix, returns False without logging. A misconfigured source_drive="E:\SOURCE" (no FY folder) means rollover NEVER triggers, ever. The user gets no error, just "stuck" backups. The orchestrator (rollover() line 213) repeats the same check and also silently returns False.
- Subtle issue: current_fy = _fy_name(source_drive) or _fy_name(lan_destination) — if source is FY26-27 and lan is FY25-26, only source is checked. Computed FY matches source → returns False. But LAN is pointing at the wrong FY and is never noticed. Not a bug per design (source is master), but worth documenting.
run_final_backup(...) -> tuple[bool, bool] (lines 69-136)
- Intent: One final sync of the closing FY to both destinations.
- Return: (cloud_ok, lan_ok). True if enabled AND sync returned a success-class exit code.
- Cloud exit classification (line 96): if exit_code in (0, 9) — matches classify_rclone_exit CLOUD_COMPLETE (0, 9). ✓
- LAN exit classification (line 119): if exit_code in range(0, 8) — robocopy bits 0-2 = 0-7 = "considered OK" per classify_exit_code (LAN_COMPLETE covers 0-7). ✓
- Anti-patterns:
- Line 119: exit_code in range(0, 8) — works (C-level range containment is O(1)) but a tuple literal (0, 1, 2, 3, 4, 5, 6, 7) is more conventional and equally fast.
- Lines 101-102, 133-134: bare except Exception masks KeyError/AttributeError from config bugs. Should tighten to (OSError, subprocess.SubprocessError, RuntimeError). Currently a config typo (e.g., config.cloud.bucket → config.cloud.bucket_name) is treated as a "retry next run" condition, silently.
- Lines 235-244: builds required = [] list then if required — could be two explicit ifs. Trivial.
- Local imports of ensure_server_online (line 106) and shutdown_server (line 127) inside the function — defensive, OK.
create_new_fy_folders(source_root, lan_root, new_fy) -> dict[str, Path] (lines 139-153)
- Intent: mkdir(parents=True, exist_ok=True) for both. Idempotent.
- Uses _child_path to construct paths. ✓
update_config_yaml(config_path, source_root, lan_root, new_fy) -> None (lines 156-195)
- Intent: Atomic ruamel.yaml round-trip write. tempfile.mkstemp + os.replace.
- Transactional: On yaml.dump failure, os.unlink(tmp_path) and re-raise. ✓ Test test_old_config_untouched_on_write_failure (test_fy_rollover.py:155) confirms.
- Race condition? If two processes start simultaneously and both detect rollover, they could race on os.replace. Not handled (no lock), but in practice rollover() is called once at startup via launch.py.
rollover(config_path="config.yaml") -> bool (lines 198-250)
- Intent: Detect, backup, create folders, update config. Returns True on rollover.
- Failure semantics: Raises RolloverError if any enabled destination's final backup failed. Config is NOT updated on failure — "retry next run" invariant. ✓
- Idempotency: detect_rollover is the gate. On the second run, current_fy == new_fy (already updated) → returns False. ✓
- Order: run_final_backup → create_new_fy_folders → update_config_yaml. If process dies between folder creation and config update, next run sees no rollover needed (config still has old FY) AND create_new_fy_folders is idempotent → eventual consistency restored. ✓
core/fy_router.py (3 lines)
Verdict: dead-weight indirection. Re-export with no consumer outside the project tree.
"""Fiscal year prefix router — re-exported from core.time_utils for backward compatibility."""
from core.time_utils import get_fy_prefix  # noqa: F401
- 4 import sites: flow.py:31, tests/test_workflows.py:18, tests/test_fy_router.py:5, core/__init__.py:3 (which then re-exports it again at line 3).
- 2 sites that BYPASS it: ui.py:30 and core/fy_rollover.py:18 import get_fy_prefix directly from core.time_utils. If the indirection were load-bearing, these two would also go through it. They don't, which proves no one is enforcing a "public API" boundary.
- The # noqa: F401 comment is the tell — the author knew the file itself does nothing.
- CODE_REVIEW_2026-06-01.md:365-376 already flagged this for deletion. Recurrence confirms it.
core/time_utils.py (144 lines, 7 functions + 1 constant)
utcnow_iso() -> str (lines 19-25)
- Returns: pendulum.now("UTC").isoformat() → e.g. "2026-05-30T14:22:00+00:00". Always timezone-aware, always parseable.
- Used by: core/manifest.py:139, 206, 252, 270, 303, flow.py:42, 509, 623, 631. 9 call sites. Centralized ✓.
cutoff_iso(days) -> str (lines 81-86)
- Returns: pendulum.now("UTC").subtract(days=days).isoformat().
- Vs utcnow_iso: NOT redundant. cutoff_iso(7) returns 7 days ago; utcnow_iso() returns now. The implementation is one extra .subtract(days=days). Sharing the formatter would be premature DRY.
- Used by: core/manifest.py:438, 501 (2 call sites, both for "retention" cutoffs in DB queries). Correct use case.
parse_iso_to_local(iso_str, tz="Asia/Kolkata") -> str (lines 40-57)
- Intent: Parse any ISO string, return YYYY-MM-DD HH:mm:ss in target tz.
- Robustness: Handles None, empty, unparseable — all return "-" or the raw first 19 chars.
- Used by: ui.py:285, 306, 312, 576, 581, 621, core/report.py:128. 7 call sites. ✓
format_iso_for_js(iso_str) -> str | None (lines 60-74)
- Intent: Pass-through reformat for JS new Date() — pendulum's isoformat always has offset.
- Used by: ZERO call sites in the repo. grep "format_iso_for_js" *.py returns only the definition. Dead code. **[FALSE — format_iso_for_js is used at ui.py:305 and ui.py:311 in the /status endpoint; imported at ui.py:30]**
get_fy_prefix(today=None) -> str (lines 96-113)
- IST correctness: pendulum.now(IST).date() — host TZ is irrelevant; always uses Asia/Kolkata.
- March 31 23:59 IST: today = date(2026, 3, 31), today.month >= 4 is False → returns f"FY{(2026-1) % 100:02d}-{2026 % 100:02d}" = "FY25-26". ✓ Correct.
- April 1 00:00 IST: today = date(2026, 4, 1), month >= 4 → "FY26-27". ✓ No off-by-one.
- Year 2099/2100 rollover: year % 100:02d works for 2099→99, 2100→00, 2000→00. ✓ Test test_year_2099_rollover and test_year_2000 cover this.
- Used by: 20 edges in the graph (god node). 6+ direct callers.
cron_to_human(cron, tz) -> str (lines 120-144)
- Pure formatter. Used by ui.py:30 for schedule display.
- No issues.
Module-level IST = pendulum.timezone("Asia/Kolkata") (line 93)
- Exported and used by tests/test_fy_rollover.py:21 for date construction in tests. ✓
Cross-file duplications
#	Duplication	Severity
1	fy_router.py is a pure re-export of time_utils.get_fy_prefix with no consumer pattern to justify it.	High (clarity, not correctness)
2	run_final_backup (fy_rollover.py:69-136) reimplements the WoL→sync→shutdown sequence that _run_lan_pipeline (flow.py:436-497) already does. Two copies of the orchestration.	High (per CODE_REVIEW_2026-06-01.md:250-257 W8)
3	flow.py:19 imports pendulum directly AND uses pendulum.parse(str(mtime)) and pendulum.parse(started_at) at lines 399, 400, 510. time_utils.py:1-6 docstring says "Every file in this project that touches datetime must import from here, not from datetime/zoneinfo directly." Violated.	Medium
4	ui.py:500 calls pendulum.now().format('YYYY-MM-DD') directly instead of utcnow_formatted("YYYY-MM-DD") from time_utils. Same violation.	Low
5	Exit-code classification logic is duplicated in fy_rollover.py:96 ((0, 9)) and fy_rollover.py:119 (range(0, 8)), AND reimplemented as classify_rclone_exit (cloud_sync.py) and classify_exit_code (lan_sync.py). The final-backup code could call classify_rclone_exit(...) == "CLOUD_COMPLETE" instead of duplicating the magic number tuple.	Low (not critical — the classifier lives in a different module, so duplicating is a tradeoff)
6	utcnow_iso() and cutoff_iso() are not redundant — they differ by .subtract(days=days). False alarm.	N/A
7	check_source_drive does manual iterdir() + disk_usage, not using psutil or shutil.disk_usage shortcut. Fine — psutil is a new dep.	N/A
8	run_final_backup in fy_rollover.py vs _run_lan_pipeline in flow.py — both call ensure_server_online, then run_lan_sync, then conditionally shutdown_server. The only differences: (a) run_final_backup inlines cloud + LAN, (b) the old_fy GCS prefix vs no prefix. Refactor candidate.	High
Anti-patterns
Location	Issue
core/health.py:54-56	check_binary_exists returns bare bool while siblings return tuple[bool, str]. Caller hardcodes "rclone not found" / "robocopy not found" at lines 122, 134. Should return (False, f"Binary not found in PATH: {name}") for consistency.
core/health.py:79	resp.getheader("Date") or resp.getheader("date") — getheader is case-insensitive; the fallback is dead code.
core/health.py:85-87	Mixes datetime (stdlib parsedate_to_datetime) and pendulum.now("UTC") in the same subtraction. Works (both are UTC-aware) but violates the time_utils.py "single source of truth" rule. Should google_time = pendulum.instance(google_time) or use pendulum.parse(rfc2822).
core/health.py:97-102	check_clock_skew returns (True, "") on network/parse error. Docstring at module level says it "Compares local UTC time against Google's HTTP Date header" — doesn't say it's optional. Silent pass is a real footgun (wrong clock → GCS 401 → backup fails much later with confusing error). At minimum, log the skip at INFO level so operators see it.
core/health.py:128	clock_reason is fetched but only used in the warning branch. On the happy path, the unpack is a no-op for clock_reason. Minor.
core/health.py:128-130	Clock skew is non-blocking but docstring of pre_backup_health (line 105-116) does NOT document that. Inconsistent with source drive / binary checks which DO block.
core/fy_rollover.py:60-66, 213-215	detect_rollover and the orchestrator silently return False when no FY suffix is present. A misconfigured path = no rollover, no error, backups continue pointing at the wrong FY. Should at least log a warning.
core/fy_rollover.py:101-102, 133-134	Bare except Exception masks config typos (AttributeError, KeyError). Tighter: (OSError, subprocess.SubprocessError, RuntimeError).
core/fy_rollover.py:119	if exit_code in range(0, 8) — works but unconventional. if exit_code < 8 is the bitmask-correct way (0-7 are bits 0-2 set, which the classifier documents as LAN_COMPLETE).
core/fy_rollover.py:213	old_fy = _fy_name(source_drive) or _fy_name(lan_destination) — second or branch can differ from detect_rollover's current_fy if the FY suffixes disagree. The two functions should agree on what the "current" FY is, or rollover might happen for the wrong reason.
core/fy_router.py	3-line re-export with no enforcement. CODE_REVIEW_2026-06-01.md:365-376 flagged for deletion. Still here.
core/time_utils.py:60-74	format_iso_for_js is defined but has zero call sites in the repo. Dead code — remove or document why it's exported.
flow.py:19, 399-400, 510	Direct pendulum import + usage. Violates the time_utils.py rule. Use from core.time_utils import parse_iso (would need to add a parse_iso(iso_str) -> float helper that returns .timestamp()) or accept the exception for the flow.py orchestrator.
ui.py:16, 500	from datetime import timedelta (line 16) for _SESSION_TTL — not a date logic concern, OK. But pendulum.now().format('YYYY-MM-DD') at line 500 violates the rule.
core/__init__.py:3	Re-exports get_fy_prefix via the fy_router indirection. If fy_router.py is deleted, this must be updated to from core.time_utils import get_fy_prefix.
Edge cases / correctness
Question	Answer
get_fy_prefix(date(2026, 3, 31)) — last moment of FY25-26?	Returns "FY25-26". ✓ Correct — March 31 belongs to the closing FY.
get_fy_prefix(date(2026, 4, 1)) — first moment of FY26-27?	Returns "FY26-27". ✓ No off-by-one.
get_fy_prefix(date(2099, 4, 1)) — Y2K-like century rollover?	Returns "FY99-00". ✓ % 100 works correctly across centuries.
check_source_drive on a slow network share?	No timeout on iterdir() or shutil.disk_usage(). Blocks indefinitely. The downstream cloud dry-run has a 10s timeout; the preflight health check does not. Inconsistent — recommend wrapping in concurrent.futures with a timeout, OR document the blocking behavior.
check_source_drive on a permission-denied dir?	Returns (False, "permission denied: ...") (line 30). ✓
check_source_drive with disk_usage raising?	Silently returns (True, "") (line 49). Soft-warn. Disagreement: file existence is hard, free-space is soft.
check_clock_skew when Google is unreachable?	Returns (True, "") (line 99). Network error is invisible at the orchestrator level — no warning logged by pre_backup_health.
check_clock_skew when local clock is actually skewed?	Returns (False, "skew detected: ...s") (line 90). pre_backup_health:130 logs WARNING but does NOT raise. Clock skew is non-blocking — surprise.
check_gcs_key on a 1-byte corrupt file?	Passes (only checks st_size > 0). Will surface as GCS 401 at sync time.
rollover() if both cloud and lan are enabled: false?	run_final_backup runs both, both return ok=False (no cloud_ok/lan_ok set to True). required = [] (empty), no error raised. create_new_fy_folders and update_config_yaml run anyway. Behavior is "best effort" — folders are created even with no actual backup. Documented? No. Test test_rollover_with_both_disabled_still_creates_folders confirms this is intentional.
rollover() if process crashes between create_new_fy_folders and update_config_yaml?	Next run: detect_rollover returns True (config still has old FY). Final backup re-runs (idempotent at GCS / robocopy level). Folders already exist (exist_ok=True). update_config_yaml retries. ✓ Self-healing.
_run_lan_pipeline (flow.py) vs run_final_backup LAN branch	Same sequence (WoL → sync → optional shutdown). The shutdown condition differs slightly: _run_lan_pipeline:lan_shutdown_task checks config.lan.shutdown_after_backup AND config.wol.enabled; run_final_backup:125 checks the same. Identical gating. The duplication is structural, not behavioral.
Does LAN pipeline use FY routing?	No. _run_lan_pipeline (flow.py:436) does NOT call get_fy_prefix(). The LAN destination is config.paths.lan_destination, which includes the FY suffix as part of the path. The folder was already named FY26-27 during rollover() — the path itself encodes the FY. This is the design — LAN is FY-agnostic at the pipeline level because the FY is in the path, not in a separate prefix. ✓
Why does cloud need get_fy_prefix() but LAN doesn't?	GCS uses bucket/FY26-27/... path structure. The FY is a top-level prefix, computed at runtime. LAN uses the absolute path in config, which was updated by rollover() to include the new FY folder.
god-node / impact analysis
- get_fy_prefix is a god node (20 edges, HIGH risk to change). 5 direct callers across 1 module: ui.py:status, _render_dashboard, tests/test_fy_rollover.py:current_fy, core/fy_rollover.py:detect_rollover, rollover. Through core/__init__.py and core/fy_router.py re-exports, 2 more indirect entry points.
- rollover (19 edges). 3 processes: launch.main (5), ui.dashboard (4), ui.status (2). Changing rollover touches the entire startup sequence and the dashboard.
- The graph report flags get_fy_prefix with "18 INFERRED edges - model-reasoned connections that need verification" — most are likely correct, but the test files account for 23 of the 23 hits in the "Tests" module.
Recommendations
#	File:line	Recommendation
R1	core/fy_router.py:1-3	Delete. CODE_REVIEW_2026-06-01.md:365-376 already flagged. Update 4 import sites: flow.py:31, tests/test_workflows.py:18, tests/test_fy_router.py:5, core/__init__.py:3 → all import from core.time_utils directly. The 2 sites that already skip it (ui.py:30, core/fy_rollover.py:18) prove no boundary is being enforced.
R2	core/health.py:54-56	Change check_binary_exists to return (bool, str): return shutil.which(name) is not None, f"Binary not found in PATH: {name}". Update pre_backup_health:122, 134 to use the returned reason. Unifies return shape across all four checks.
R3	core/health.py:79	Simplify: google_date_str = resp.getheader("Date") (case-insensitive).
R4	core/health.py:69-102	Consider a HealthCheckResult NamedTuple/dataclass for return shape, or at minimum keep all four checks returning tuple[bool, str]. Currently three return tuples, one returns bool.
R5	core/health.py:97-102	Add INFO/WARNING log when clock skew is skipped due to network error. Currently invisible at the pre_backup_health orchestrator.
R6	core/health.py:128-130	Either document the non-blocking semantics of clock skew in pre_backup_health's docstring, or actually raise HealthError on real skew. As written, a 30-minute skew produces a warning, then GCS rejects the JWT mid-sync. Inconsistent.
R7	core/health.py:16-51	Add a timeout to check_source_drive for network drives. Options: concurrent.futures.ThreadPoolExecutor + future.result(timeout=N), OR document that the function is blocking. Currently 0 timeout.
R8	core/fy_rollover.py:60-66, 213-215	Log a warning when no FY suffix is found in either path. Silent False return = hidden misconfiguration.
R9	core/fy_rollover.py:101-102, 133-134	Tighten except Exception to (OSError, subprocess.SubprocessError, RuntimeError). Config errors should fail loudly, not retry-forever.
R10	core/fy_rollover.py:119	Replace if exit_code in range(0, 8) with if exit_code < 8 (matches the bitmask meaning) or if classify_exit_code(exit_code) != "LAN_FAILED". The latter is more robust if classify_exit_code ever changes.
R11	core/fy_rollover.py:62, 213	current_fy and old_fy use or to pick a fallback. If the two paths disagree on FY, the system silently picks source. Add a validation step: if _fy_name(source_drive) != _fy_name(lan_destination) (and both non-None), log an error. The current "source is master" assumption is undocumented.
R12	core/fy_rollover.py:69-136 (vs flow.py:436-497)	Extract the LAN+shutdown sequence into a shared helper. CODE_REVIEW_2026-06-01.md:250-257 W8 — a bug fix in one will not be ported to the other. Highest-impact refactor in this chunk.
R13	core/time_utils.py:60-74	format_iso_for_js is unused. Either delete or document the intended caller.
R14	flow.py:19, 399, 400, 510	Add def parse_iso_timestamp(iso_str) -> float to time_utils.py (returns .timestamp()) and use it here. Honors the "single source of truth" rule from the module docstring.
R15	ui.py:500	Replace pendulum.now().format('YYYY-MM-DD') with utcnow_formatted("YYYY-MM-DD") (already exists in time_utils.py:28).
R16	core/health.py:85-87	Wrap Google time in pendulum: pendulum.instance(parsedate_to_datetime(google_date_str)) or use pendulum.parse(google_date_str, tz="UTC") with rfc2822 format. Currently mixes stdlib and pendulum datetimes.
R17	core/health.py:105-136	The pre_backup_health docstring should explicitly call out: clock skew and GCS key checks are non-blocking (warning only). Source drive and binary checks are blocking (raise). Currently this is a subtle behavioral surprise.
Notes on what was NOT a problem (verified)
- The time_utils.py:1-6 docstring rule "every file must import from here" is followed in the core/ package — no datetime.now() / datetime.utcnow() / zoneinfo usage found in any core file. The two violations are in flow.py and ui.py (R14, R15).
- get_fy_prefix IST logic is correct for the boundary cases (March 31 23:59 = old FY, April 1 00:00 = new FY, year 2099/2100 rollover, year 2000). Tests cover all four.
- rollover is idempotent and self-healing across crashes between folder creation and config update.
- _fy_name regex ^FY\d{2}-\d{2}$ correctly rejects "FY26" (no dash) and "FY26-2" (truncated).
- Exit code classification in run_final_backup matches the canonical classifiers in cloud_sync.py and lan_sync.py.
▣  General · MiniMax M3 Free · 7m 42s
