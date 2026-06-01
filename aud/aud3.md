Chunk 3 Audit: cloud pipeline + rclone
Functions analyzed
core/rclone_config.py (50 lines)
write_temp_config(gcs_key_path, location, project_number, storage_class) -> str (L12-37)
- Intent: serialize GCS remote config to a temp file.
- Correctness: builds INI-like block with all required keys; key_abs = str(Path(gcs_key_path).resolve()).replace("\\", "/") normalizes path separators (necessary for the INI value on Windows; harmless on Linux).
- Issue: function is exported and re-exported as _write_temp_config alias in flow.py per MANUAL_CODE_REVIEW.md:22 — confusing chain. (Pre-existing, not new.) **[FALSE — grep for _write_temp_config across all .py files returns zero matches; no such alias exists in flow.py or anywhere in the codebase]**
temp_rclone_config(*args, **kwargs) (contextmanager) (L40-49)
- Intent: yield a temp config path with auto-cleanup.
- Correctness: write happens in body before try, so the only cleanup is unlink(). Does not unlink on the write_temp_config failure path — if Path(cfg_path).write_text raises, the file is leaked. Acceptable since mkstemp returns a unique name and OS cleans /tmp eventually, but worth noting.
core/cloud_sync.py (156 lines)
classify_rclone_exit(code: int) -> str (L16-44)
- Intent: map rclone exit codes → CLOUD_COMPLETE | CLOUD_PARTIAL | CLOUD_FAILED.
- Mapping table: correct per official rclone docs (verified via webfetch of rclone.org/docs/).
- Docstring has codes 1↔2 swapped: line 20 says "1 → (uncategorised error)" and line 21 says "2 → (syntax/usage)"; the rclone docs (rclone.org/docs/) say "1 = Syntax or usage error" and "2 = Error not otherwise categorised". The mapping table is functionally correct (both → CLOUD_FAILED), so no runtime impact, but the docstring is wrong.
- Dead-code path: code 9 requires --error-on-no-transfer flag (rclone.org/docs/), but build_rclone_sync_command (L47-79) does not set that flag. Code 9 will never be returned in practice.
- Dead-code path: code 10 requires --max-duration, also not set. Unreachable.
build_rclone_sync_command(source, bucket, fy_prefix, config_path, storage_class, bwlimit, retries, transfers, checkers) -> list[str] (L47-79)
- Intent: construct rclone sync with GCS-optimized flags.
- Correctness: GCS flags look correct (--gcs-no-check-bucket, --gcs-storage-class). --no-traverse is generally paired with --fast-list — that is satisfied. --modify-window 1s is aggressive (typical is 1s-5s for NTFS/SMB) — acceptable.
- --track-renames requires --fast-list (satisfied) but is documented as expensive on large remotes; combined with --no-traverse may be redundant — rclone scans for renames via listing.
- API smell: storage_class is passed in twice — once as cmd[--gcs-storage-class] and once embedded in the config file (temp_rclone_config writes storage_class = X into the INI block). This is redundant; rclone honors both but the cmd-line flag wins. Minor.
run_cloud_sync(...) (dict) (L82-155)
- Intent: execute sync with temp config + temp stderr log, classify, return.
- Lifecycle of temp config: temp_rclone_config(...) context manager handles cleanup correctly (verified via rclone_config.py:40-49).
- Lifecycle of stderr log: tempfile.mkstemp + os.close(fd) + try/finally with Path.unlink(). Correct — matches the Windows-safe pattern from EDGE_CASE_AUDIT.md:33-47 (NamedTemporaryFile would hold the handle and block rclone on Windows). The unlink is in finally so it runs on success, timeout, and OSError.
- Bug: text=True is set (L120) but stdout is subprocess.DEVNULL — text=True is only meaningful for stdout/stderr captures. Harmless but misleading.
- Issue: result is not in scope inside the except blocks for TimeoutExpired/FileNotFoundError/OSError. If subprocess.run raises mid-execution, the local var is unbound and Python will not try to read it (good), but the code does not even log the partial result.returncode if it was assigned before the raise. In practice result is only assigned on the line where subprocess.run returns, so this is fine — but the pattern of result not existing inside except makes the function fragile to refactors.
- No shell=True — safe. Commands passed as list.
core/cloud_preflight.py (80 lines)
run_cloud_dry_run(...) (dict) (L14-80)
- Intent: rclone check --one-way dry-run.
- Correctness: exit-0 = matches, exit-1 = differences (normal). ok = code < 2 is correct. matched = code == 0 is correct.
- Uses temp_rclone_config (L39) — consistent with cloud_sync.
- Asymmetry: signature takes gcs_key_path, project_number, storage_class, location and creates its own temp config; but cloud_verify.py takes a pre-built config_path. The interface is inconsistent — preflight duplicates the GCS config-construction code path (one extra temp file write per preflight call) for no good reason. Could be unified.
- No shell=True — safe.
core/cloud_verify.py (74 lines)
verify_cloud_integrity(source, bucket, fy_prefix, config_path, timeout=600) -> dict (L8-73)
- Intent: post-sync rclone check --one-way — exit 0 = byte-identical.
- Correctness: verified = returncode == 0 — strict (post-sync, anything else is failure). Good.
- No temp file — uses capture_output=True, no log file. This is appropriate because the operation is fast (<10 min) and stderr is small.
- Asymmetry vs preflight: preflight uses ok = code < 2 (lenient), verify uses verified = code == 0 (strict). The asymmetric semantics are intentional and correct: preflight is just "can we talk to GCS", verify is "is everything mirrored". Worth a comment to prevent future "fixes".
- No shell=True — safe.
core/cloud_reporter.py (119 lines)
_base_args(config_path) -> list[str] (L16-17)
- Tiny helper. Used by all 3 reporters. Good.
get_cloud_size(bucket, fy_prefix, config_path) -> dict (L20-35)
- Calls rclone size --json. Returns {count, bytes, sizeless}. 30s timeout.
- Issue: json.loads(result.stdout.strip()) — has .strip(). The sibling get_cloud_manifest (L48) does not strip. Inconsistency flagged in MANUAL_CODE_REVIEW.md:60. If rclone ever emits leading whitespace, manifest query crashes while size handles it.
- Hardcodes timeout=30 — should be a parameter (or a constant at module level) for consistency with manifest (300) and diff (600). Config-driven timeout would match the rest of the cloud_* family.
get_cloud_manifest(bucket, fy_prefix, config_path) -> list[dict] (L38-54)
- Calls rclone lsjson -R, filters out IsDir entries.
- Returns full list — no streaming. For a multi-million-file bucket, this loads everything into RAM. Consistent with lan_manifest.walk_lan_destination (also full-list), but same scalability concern (MANUAL_CODE_REVIEW.md:475).
- Missing .strip() on stdout — see above.
get_cloud_diff(source, bucket, fy_prefix, config_path) -> dict (L57-119)
- Calls rclone check --combined writing to a temp diff file, then parses +/-/*/= prefixes.
- Temp file pattern: tempfile.mkstemp + os.close(fd) + try/finally: unlink(). Matches the Windows-safe pattern. Correct.
- Hardcoded timeout 600s — not configurable.
- Issue: does not use temp_rclone_config. Receives config_path as parameter (same as cloud_verify), so the caller (flow.py) must wrap in temp_rclone_config. This is the same asymmetry as cloud_verify — verify+size+manifest+diff are designed to share one config, but preflight and sync each build their own. The "verify+report" cluster has a clean shared-config API; the "preflight+sync" cluster duplicates config-construction. Could be unified by making all five functions take config_path and centralizing config creation at the flow level.
- _mock_result test fixture is duplicated in tests/test_cloud_reporter.py:10-15 and tests/test_cloud_verify.py:9-14 (identical). Belongs in a shared test helper.
Symmetry check: cloud vs LAN
Function pair (cloud vs LAN)	Symmetric?	Notes
build_rclone_sync_command (cloud_sync.py:47) vs build_robocopy_command (lan_sync.py:46)	✅ Separate is correct	Different binaries, different flag sets
classify_rclone_exit (cloud_sync.py:16) vs classify_exit_code (lan_sync.py:26)	✅ Different models is correct	rclone = fixed codes 0-10; robocopy = bitmask
run_cloud_sync (cloud_sync.py:82) vs run_lan_sync (lan_sync.py:69)	✅ Parallel structure	Both: build cmd, temp log file, subprocess.run, classify, return {status, exit_code, error}
run_cloud_dry_run (cloud_preflight.py:14) vs run_lan_dry_run (lan_preflight.py:12)	✅ Parallel	Both: pre-flight, lenient exit-code (< 2 / < 8), return {ok, exit_code, error}
cloud_verify.verify_cloud_integrity (cloud_verify.py:8)	⚠️ No direct LAN analog	LAN side uses lan_manifest.walk_lan_destination + diff_snapshots to verify post-sync — completely different mechanism (filesystem walk vs rclone check). Both approaches are correct, but they are not a 1-to-1 function pair.
cloud_reporter.get_cloud_diff	❌ No LAN analog	lan_manifest.diff_snapshots does the same job (added/removed/modified/unchanged) but is implemented as in-memory dict diffs over two walk_lan_destination snapshots — different mechanism. Could be unified with a common DiffResult dataclass.
cloud_reporter.get_cloud_size / get_cloud_manifest	❌ No LAN analog	No get_lan_size or get_lan_manifest. The LAN side does its own walk_lan_destination (81 lines) and computes size implicitly via file entries. Cloud has 3 dedicated reporter functions; LAN has 0. Asymmetry — but justified: robocopy doesn't have a "size the destination" subcommand, and walking the share twice is expensive.
rclone_config.temp_rclone_config (rclone_config.py:40)	⚠️ No LAN analog	lan_sync.py:88 does the same mkstemp + os.close + try/finally unlink pattern inline for /LOG:. Extractable to a shared temp_log_file() context manager (or moved into a shared core/tempfiles.py).
Cross-file duplications
1. mkstemp + os.close + Path.unlink in finally appears in 3 places:
- core/rclone_config.py:34-36, 47-49 (config)
- core/cloud_sync.py:112-113, 152-155 (stderr log)
- core/cloud_reporter.py:74-75, 115-118 (diff file)
- core/lan_sync.py:88-90, 130-133 (robocopy log)
- core/fy_rollover.py:181 (used too — already noted)
- Recommendation: extract to core/tempfile_helpers.py:
@contextmanager
def temp_path(suffix, prefix) -> Iterator[str]:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
    os.close(fd)
    try:
        yield path
    finally:
        try: Path(path).unlink()
        except OSError: pass
2. subprocess.run exception-handling boilerplate is repeated 4 times with the same TimeoutExpired/FileNotFoundError/OSError pattern:
- cloud_sync.py:142-150
- cloud_preflight.py:60-68
- cloud_verify.py:66-73
- lan_sync.py:120-128
- Could be extracted to a run_subprocess(cmd, timeout, error_status) helper.
3. _mock_result test fixture duplicated verbatim in tests/test_cloud_reporter.py:10-15 and tests/test_cloud_verify.py:9-14. Belongs in tests/conftest.py or tests/_helpers.py.
4. No same-name duplication between cloud and LAN (build_rclone_sync_command vs build_robocopy_command, etc. — kept separate, which is correct).
5. No different-name-same-functionality bug for rclone check. cloud_preflight.run_cloud_dry_run and cloud_verify.verify_cloud_integrity both wrap rclone check --one-way --fast-list --config ... --gcs-no-check-bucket, but the cmd lists are identical at the binary-flag level — they are correct, intentional copies (preflight lenient, verify strict). A private helper _build_check_command(source, dest, config_path) would consolidate the 4-line cmd.
6. _base_args helper in cloud_reporter.py:16 is good — already deduplicates the --config/--gcs-no-check-bucket/--fast-list triplet for size/manifest/diff.
7. IMPLEMENTATION_PLAN.md:1021 still references from core.cloud_sync import write_temp_rclone_config, ... — write_temp_rclone_config was renamed/removed; current API is temp_rclone_config context manager from core.rclone_config. Documentation drift, not code bug.
Anti-patterns
- core/cloud_sync.py:120 — text=True on subprocess.run with stderr=stderr_file (a real file handle, not PIPE). Useless — only affects PIPE text decoding. Harmless. Info-level, not a bug.
- core/cloud_sync.py:155 / core/cloud_reporter.py:117 — Path.unlink() in finally swallows OSError silently. Acceptable for cleanup, but in cloud_reporter.py this is the only error path; if unlink fails on Windows because the file is held by something else, the user sees nothing. Low.
- core/rclone_config.py:23 — key_abs = str(Path(gcs_key_path).resolve()).replace("\\", "/"). The replace("\\", "/") is dead code on Linux (no backslashes in resolved paths); on Windows it's load-bearing because rclone's INI parser wants forward slashes in the service_account_file value. No fix needed, but worth a comment.
- No shell=True anywhere — confirmed clean. All subprocess.run calls pass cmd as a list. Good.
- No hardcoded rclone binary path — uses bare "rclone" and relies on PATH. Could improve with shutil.which("rclone") validation at module load (would give a clear error if the binary is missing), but not required.
- No stdout-parse-without-timeout — all calls have timeout=.... Good.
- core/cloud_preflight.py + core/cloud_verify.py — both build essentially the same rclone check cmd. The 5 lines ["rclone", "check", source, dest, "--one-way", "--fast-list", "--config", config_path, "--gcs-no-check-bucket"] are duplicated. Extract.
- Inline dest = f"aam_gcs:{bucket}/{fy_prefix}" appears in 4 places: cloud_sync.py:59, cloud_preflight.py:40, cloud_verify.py:31, cloud_reporter.py:25/43/70. Extract to _gcs_dest(bucket, fy_prefix) helper in rclone_config.py (alongside the remote name "aam_gcs" which is the rclone remote key in the INI file).
Verification
- Cloud exit code classification (cloud_sync.py:31-44) is correct per rclone docs. Codes 0, 9 = success; 4, 5, 6, 10 = partial/transient; 1, 2, 3, 7, 8 = failed. However: codes 9 and 10 are unreachable in this codebase because the corresponding flags (--error-on-no-transfer, --max-duration) are not passed. The classification is correct, but the "no files to transfer" case (9) is interesting — the current code treats it as COMPLETE, but if the source drive is empty, the operator may want to know. Currently invisible.
- temp_rclone_config cleanup on exception: yes, the finally block runs even on exception (rclone_config.py:46-49) — verified by reading the code. The only leak path is if write_temp_config itself raises between mkstemp and write_text completing — in that case the unlink is never called. Low risk.
- mkstemp + close pattern: already evaluated per EDGE_CASE_AUDIT.md:33-47 (Windows file handle lock fix). Correct.
Recommendations
 1. core/rclone_config.py:23 — add a comment explaining the replace("\\", "/") is for Windows INI values.
 2. New file core/tempfile_helpers.py — extract a temp_path(suffix, prefix) context manager; replace 5 inline copies (3 cloud + 1 lan + 1 fy_rollover).
 3. New file core/subprocess_runner.py — extract a run_with_exit_handling(cmd, timeout, *, stdin=None, stdout=None, stderr=None, error_status="FAILED") helper to deduplicate the 4 copies of TimeoutExpired/FileNotFoundError/OSError handling.
 4. core/rclone_config.py — add _gcs_dest(bucket, fy_prefix) -> str helper and replace the 6 inline f"aam_gcs:{bucket}/{fy_prefix}" occurrences.
 5. core/cloud_preflight.py + core/cloud_verify.py — extract _build_check_command(source, bucket, fy_prefix, config_path) -> list[str] to deduplicate the 4-line rclone check --one-way cmd.
 6. core/cloud_sync.py:20-21 — fix the docstring: swap "1 → (uncategorised)" and "2 → (syntax/usage)" so they match rclone's official documentation.
 7. core/cloud_reporter.py:48 — add .strip() to json.loads(result.stdout) for consistency with get_cloud_size (L30).
 8. core/cloud_reporter.py:29, 47, 84 — make timeouts module-level constants or function parameters (currently 30/300/600 are hardcoded).
 9. Interface unification — consider making cloud_preflight.run_cloud_dry_run and cloud_sync accept a pre-built config_path (like cloud_verify and cloud_reporter already do) and centralize temp_rclone_config construction in flow.py. This eliminates the asymmetry where preflight/sync build their own config but verify/report share one. Risk: low (config content is identical). Touch points: flow.py:78-86, 96-109, cloud_preflight.py:14-39, cloud_sync.py:82-108.
10. tests/test_cloud_reporter.py:10-15 + tests/test_cloud_verify.py:9-14 — deduplicate _mock_result into tests/conftest.py or tests/_helpers.py.
11. IMPLEMENTATION_PLAN.md:1021 — fix stale import: from core.cloud_sync import write_temp_rclone_config no longer exists; replace with from core.rclone_config import temp_rclone_config.
12. MANUAL_CODE_REVIEW.md:22 flagged issue — flow.py:67-71 has a confusing write_temp_config as _write_temp_config alias chain; if the API rename to temp_rclone_config is being adopted, the old alias and any leftover from core.cloud_preflight import _write_temp_config should be removed.
