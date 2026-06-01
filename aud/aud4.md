Chunk 4 Audit: LAN pipeline + WoL + shutdown
Functions analyzed
lan_sync.py
- _validate_required_flags(flags: list[str]) -> None (lines 20–23)
- Intent: guard against /NC which strips file-class labels that the downstream ConvertFrom-RobocopLog parser needs.
- Correctness: matches case-insensitively for both / and - prefixes. Tested (test_lan_sync.py:44-58).
- Gap: only forbids /NC. If the project ever adds another "forbidden" flag (e.g. /NFL to suppress file names for log parsing), the validator must be updated. Currently 1-element tuple — the loop is overkill for a single check.
- classify_exit_code(code: int) -> str (lines 26–43)
- Intent: decode robocopy's bitmask exit code into LAN_COMPLETE / LAN_PARTIAL / LAN_FAILED.
- Bitmask verified against Microsoft robocopy docs and WINDOWS_LEARNINGS.md:24-44:
- 0–7 (bits 0–2 only) = success, including "no copy needed" (0), "extras present" (2), "mismatches detected and overwritten" (4)
- 8–15 (bit 3 set) = partial — some files failed to copy
- 16+ (bit 4 set) = fatal
- Negative codes: -1 & 16 == 16 in Python (infinite two's complement), so negative exit codes from subprocess.run (e.g. our -1 sentinel for Timeout/FileNotFoundError) correctly land in FAILED via the first check, not the fallback. Tested (test_negative_code_returns_failed).
- Verified bit-pattern correctness with all 13 representative values (run above): every code maps to the right bucket.
- Doc nit: docstring at line 30–34 says "Bit 0 (1): Files copied successfully" — accurate but doesn't mention that bit 0 = 0 (i.e. value 0 with no bits) means "no copy needed, source/dest in sync." Adding a "code 0" line would help future readers.
- build_robocopy_command(source, dest, lan_config) (lines 46–66)
- Intent: assemble the canonical /MIR + restartable + multi-threaded + verbose robocopy command.
- Flag correctness vs. robocopy docs:
- /MIR — mirror (purge extras). ✅
- /Z — restartable mode (legacy; superseded by /ZB for backup-mode fallback). ✅
- /ZB — restartable + backup-mode fallback (uses COPY_FILE_RESTARTABLE Win32 flag, falls back to backup privileges if denied). ✅
- /XJ — exclude junction points (critical for /MIR to avoid infinite recursion through reparse points). ✅
- /MT:n — multi-threaded copy. ✅
- /R:n /W:n — retry count and wait between retries. ✅
- /V /TS /FP — verbose + timestamps + full paths in the log. ✅
- /NJH /NJS /NDL /NP — suppress header, summary, directory list, per-file progress. Necessary so the /LOG: file isn't 90% noise. ✅
- /XD "System Volume Information" — excludes the VSS metadata folder. ✅
- Minor concern: /NC is checked for in _validate_required_flags, but /V (verbose) is added at line 60 — and the validator doesn't run against the built list at line 65 until after the call. This is fine because the flags are hardcoded, but if a future caller injects custom flags, validation runs only over the combined list. Not a bug; just noting the order.
- run_lan_sync(source, dest, lan_config) -> dict (lines 69–134)
- Intent: spawn robocopy with a temp log file, capture the tail on failure, always clean up the temp file.
- Temp-file pattern is correct Windows-safe (per EDGE_CASE_AUDIT.md:33-54): tempfile.mkstemp returns (fd, path), then os.close(fd) is called immediately to release the handle so robocopy can write to it. This was previously broken in cloud_sync/cloud_reporter/cloud_preflight and was fixed in V1 (commit 15d16f2).
- subprocess.run(..., stdout=DEVNULL, stderr=DEVNULL): justified because robocopy with /LOG:path writes all output to the file. The comment on lines 98–99 explicitly explains this. Verified: robocopy exits 0 through 7 normally and the log file is the only artifact.
- Bug-shaped observation: error tail is captured only on LAN_FAILED (line 107). On LAN_PARTIAL (e.g., exit 8), the error_msg is None and the user gets no diagnostic info from the log. The bit-3 case often means "some files failed to copy" — exactly when you'd want the log tail. Recommend: capture log tail on both FAILED and PARTIAL.
- Cleanup finally is correct: if log_path and log_path.exists() guards against the case where mkstemp failed, and OSError is silently swallowed because log cleanup is best-effort.
- TimeoutExpired returns exit_code=-1 consistent with classify_exit_code(-1) == "LAN_FAILED".
lan_preflight.py
- run_lan_dry_run(source, dest, timeout=300) -> dict (lines 12–66)
- Intent: cheap /L (list-only) probe to validate UNC reachability + permissions + junction behavior before committing to a multi-hour /MIR.
- Flag set: /L /MIR /XJ /NJH /NJS /NP — no retry flags, no thread count, no verbose. This is correct for a probe: we want to fail fast and produce minimal output.
- Threshold < 8 is correct for robocopy's "no copy errors" guarantee (matches the bitmask from classify_exit_code). Note the asymmetry: bits 3 and 4 (8, 16) are treated as fatal for preflight, but the same codes in a real sync are only FAILED/FAILED. This is intentional — if your dry-run shows copy errors, aborting the real sync is the right call.
- capture_output=True, text=True — captures both streams. Only stderr[:200] is logged. The 200-char snippet is enough for "Access denied" / "network name not found" but loses context on multi-line errors.
- Symmetric counterpart: cloud_preflight.py:run_cloud_dry_run uses rclone check --one-way and code < 2 (since rclone's exit codes are 0=ok, 1=differences, 2+=error). The threshold difference reflects the underlying tool's semantics, not a design choice. Could share a @ok_on_exit(max_code) decorator (see Duplications below).
lan_manifest.py
- walk_lan_destination(unc_path: str) -> list[dict] (lines 13–43)
- Intent: walk the destination share, return every file with relative path / size / mtime.
- O(1) per file: os.walk is C-level; the inner loop is os.stat (one syscall per file, unavoidable). Total cost is O(n) over all files — no quadratic patterns.
- UNC safety verified: os.path.join("\\\\192.168.10.10\\share$", "sub", "file.txt") returns "\\\\192.168.10.10\\share$\\sub\\file.txt" correctly (run above). os.path.relpath against the resolved base also works. No os.path.join surprises on Windows.
- Skipping behavior: try/except OSError around os.stat(full) skips locked/deleted files mid-walk. Tested (test_lan_manifest.py:37-40). This is correct — a transient lock should not abort the whole walk.
- Skips "the things it should": does NOT skip hidden files (no filter for name.startswith(".")), does NOT filter by extension. In /MIR mode, the share already mirrors the source exactly, so there's nothing to filter on. The "skips files it should (locked, hidden)" question in the prompt: locked is handled, hidden is NOT (and arguably shouldn't be — System Volume Information is filtered at the robocopy level via /XD, not the walk level).
- Offline share handling: if the share is unreachable, os.walk(unc_path) raises OSError (WinError 53 / "network path not found") on the first scandir call. The function does NOT catch this. The caller in flow.py:lan_snapshot_after_task will re-raise, the task will fail, the run is marked LAN_FAILED, and the before snapshot is discarded because we never reach lan_record_task. This is a design choice: fail loud rather than silently record an empty "after" state. Defensible, but the user prompt's "return empty vs raise" question is relevant: returning [] would let the pipeline still record "no new files" and continue. For a real production system, I'd lean toward raise + upstream retry, which is what we have.
- Mtime comparison concern (raised in prompt): both before and after walks read from the same SMB share, so the mtime precision is consistent (whatever the share's underlying FS returns — typically 1-second or 2-second on SMB). The float equality check in diff_snapshots (before[p] != after[p]) is safe within a single share walk. The risk would only manifest if a file is rewritten between the two walks, in which case the mtime would be a fresh timestamp and the inequality would correctly catch it. ✅
- The base = str(Path(unc_path).resolve()) at line 25: on Windows, Path.resolve() follows UNC symlinks and normalizes the path. For a share like \\192.168.10.10\share$, this returns the UNC path back. os.path.relpath against it yields paths with \\ separators (Windows default). The downstream backup_repository.record_sync_results (line 136) normalizes \\ → / before SQLite insert, so the inconsistency is absorbed.
- snapshot_to_dict(files: list[dict]) -> dict[str, tuple[int, float]] (lines 46–48)
- Intent: O(1)-lookup index for diffing.
- Correctness: trivial dict comprehension. No collision handling (last-write-wins if the same path appears twice, but os.walk never returns the same path twice, so safe).
- diff_snapshots(before, after) -> dict (lines 51–81)
- Intent: O(n) diff producing 4 categories.
- Uses set arithmetic (&, -) and a single intersection walk. Sorted output. O(n) memory and time. Verified by 7 tests in test_lan_manifest.py.
- No edge-case issues — empty inputs return empty categories.
wol.py
- _smb_port_open(server_ip, port=445, timeout=5.0) -> bool (lines 19–27)
- Intent: TCP connect to SMB port (more reliable than ICMP ping, which firewalls often block).
- Correctness: with socket.socket(...) as sock + sock.settimeout(timeout) + connect_ex is the textbook pattern. Tested.
- Why manual socket instead of httpx.head or psutil? Because this is port liveness, not HTTP. No library is more appropriate than socket here.
- _send_magic_packet(mac_address: str) -> None (lines 30–36)
- The prompt's note that this is "manual socket.sendto" is incorrect. Line 10 imports from wakeonlan import send_magic_packet as wol_send, and line 33 calls wol_send(mac_address, ip_address="255.255.255.255", port=9). The wakeonlan library handles the UDP packet construction (the 6-byte sync stream + 16 repetitions of the MAC). Verified the library signature with help(send_magic_packet).
- However, the codebase-review note I7 in CODE_REVIEW_2026-06-01.md:485 still applies: the try/except OSError wrapper raises a new OSError with a stringified message. This is a no-op anti-pattern. Either let the original OSError propagate (with the wakeonlan message) or define WolSendError(RuntimeError) for type-safety.
- Minor: the as wol_send alias is awkward. import wakeonlan + wakeonlan.send_magic_packet(...) is just as clear.
- wait_for_server(server_ip, wake_timeout, ping_interval, stability_wait) (lines 39–62)
- Intent: poll SMB port until server is up, then sleep for stability_wait seconds to let services finish initializing.
- Polling with linear backoff (ping_interval between checks). The time.sleep inside the loop with time.time() - start_time is the standard pattern. No drift.
- stability_wait > 0 guard is correct — allows disabling stability wait via config (the WolConfig.stability_wait_seconds: int = Field(default=30, ge=0) constraint allows 0).
- Improvement opportunity: linear polling wastes cycles if the server is slow to boot. An exponential backoff (1s → 2s → 4s → 8s, capped at ping_interval) would converge faster on average.
- ensure_server_online(config) -> bool (lines 65–89)
- Intent: short-circuit if WoL disabled, skip if already online, otherwise send magic packet + wait.
- The double _smb_port_open check (line 77 before WoL, then implicitly in wait_for_server) means we won't unnecessarily send a WoL to an already-online server. Good.
- Returns True on success, raises WolTimeout on failure. Note: the function only raises — it does not return False for "couldn't reach server." This means the caller in flow.py:wol_check_task will fail the whole backup if WoL times out. The intent is clear: if you can't reach the server, the backup cannot proceed.
shutdown.py
- shutdown_server(server_ip: str) -> dict (lines 11–50)
- Command: shutdown /s /m \\<server_ip> /t 300 /f — verified against Microsoft docs:
- /s = shutdown
- /m \\<computer> = target remote computer (UNC-formatted)
- /t 300 = 5-minute delay (max 315360000, but 300 is the practical default for "give staff time to cancel")
- /f = force running apps to close
- The 5-minute (300s) delay is hardcoded. This should be a config value for two reasons:
1. Tests can't safely invoke this with a 5-minute wait without a real cancel (shutdown /a) on the target.
2. A small deployment might want a different window (e.g., 60s for unattended servers, 900s for staffed offices).
Add shutdown_delay_seconds: int = Field(default=300, ge=0, le=3600) to LanConfig and read from there.
- Safety concern (raised in prompt): shutdown /m \\192.168.10.10 will shut down whatever responds to that IP. If wol.mac_address is for server A but wol.server_ip is misconfigured to a different machine's IP, you'd shut down the wrong host. The current defense is: the preflight just confirmed the share is accessible at that IP, and the WoL ping confirmed that IP's SMB is up. A more robust check would compare the resolved hostname or MAC OUI, but for a small, single-server deployment this is acceptable.
- Stylistic nit (CODE_REVIEW_2026-06-01.md:I6): the f"\\\\{server_ip}" is a manually-escaped UNC. The project uses raw strings elsewhere (r"\\server\share"). Consider rf"\\{server_ip}" for clarity.
- subprocess.run(cmd, capture_output=True, text=True, timeout=30): shutdown.exe returns 0 on success and 1190 if another shutdown is already pending. The current code treats any non-zero as failure, which is correct.
- Missing Windows creation flag: per WINDOWS_LEARNINGS.md:1-20, subprocesses spawned without CREATE_NO_WINDOW (0x08000000) can pop a console window. Shutdown is unlikely to pop a window (it's a Windows built-in), but applying the same flag used elsewhere would be consistent.
Symmetry check: LAN vs cloud
Concern	LAN side	Cloud side	Symmetric?
Preflight command shape	run_lan_dry_run with /L probe	run_cloud_dry_run with rclone check --one-way	Same intent (cheap pre-sync validation), different shapes
Preflight threshold	code < 8 (robocopy bitmask)	code < 2 (rclone: 0=match, 1=diff, 2+=error)	Symmetric in intent, different in value
Sync exit classification	classify_exit_code (3 states)	classify_rclone_exit (3 states, dict-mapping 0-10)	Symmetric in shape, different in implementation
Sync subprocess pattern	tempfile.mkstemp + subprocess.run(..., stdout=DEVNULL, stderr=DEVNULL)	tempfile.mkstemp + subprocess.run(..., stdout=DEVNULL, stderr=stderr_file)	Near-identical — could be one helper
Manifest production	walk_lan_destination (Python os.walk + os.stat)	get_cloud_manifest (delegates to rclone lsjson)	Asymmetric by design — LAN has no unified CLI to delegate to
Manifest size query	MISSING	get_cloud_size (via rclone size)	Asymmetric — LAN has no size query
Diff production	diff_snapshots (pure Python, in-memory)	get_cloud_diff (delegates to rclone check --combined + parses +/-/=/* prefixes)	Asymmetric — Python vs subprocess+parse
Sync result envelope	{"status", "exit_code", "error"}	{"status", "exit_code", "error"}	Symmetric ✅
Preflight result envelope	{"ok", "exit_code", "error"}	{"ok", "matched", "exit_code", "error"}	Cloud has matched, LAN doesn't (LAN has no concept of "is already in sync" because /L doesn't tell you)
Temp-file cleanup	finally: if log_path and log_path.exists(): log_path.unlink()	finally: if stderr_path: Path(stderr_path).unlink()	Same pattern, slightly different guards
Symmetry verdicts
1. lan_manifest vs cloud_reporter: should they share an abstraction? No, not worth it. The data shapes are different (lowercase path/size/mtime from os.walk vs PascalCase Path/Size/ModTime from rclone lsjson), the sources of truth are different (filesystem vs cloud bucket), and the backup_repository.record_sync_results (line 30-37) already normalizes both into a common shape. Adding a shared abstraction layer would be a third module that translates between the two — net negative.
2. lan_preflight vs cloud_preflight: same intent (cheap pre-sync validation), different shapes. The threshold difference is a property of the underlying tool, not a design choice. A shared helper would have to take a max-code parameter, which negates most of the savings.
3. The real symmetry win is the subprocess-with-temp-log + three-exception-handler pattern (W3 in CODE_REVIEW_2026-06-01.md). 7+ files duplicate it.
Duplications
1. subprocess.run + temp log file + three-exception handler (W3)
- In: lan_sync.py:96-128, lan_preflight.py:39-66, shutdown.py:32-49, cloud_sync.py:112-150, cloud_preflight.py:53-68, cloud_verify.py, cloud_reporter.py.
- 7 sites, each ~10 lines of identical try/except/except/except. The error-dict shape varies ({"ok", "error", "exit_code"} for preflight vs {"status", "exit_code", "error"} for sync), but the boilerplate is identical.
- Fix: one helper in core/subprocess_runner.py (new module):
@dataclass
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    not_found: bool
    os_error: str | None
def run_subprocess(cmd, *, timeout, binary_name, capture_stderr_to_file=False) -> SubprocessResult
Then each caller shrinks to a 1-2 line invocation.
2. Exit-code classification in lan_sync.py and cloud_sync.py (W2-adjacent)
- classify_exit_code uses bitmask rules; classify_rclone_exit uses an explicit dict. Both produce *_COMPLETE | *_PARTIAL | *_FAILED.
- Fix: a thin @ok_on_exit(*codes) decorator OR a _Classify protocol. Probably not worth it — the rules are tool-specific and small enough that a dict vs. if-chain is fine. The bigger win is the envelope shape (the dict returned), which is already aligned.
3. dest = f"aam_gcs:{bucket}/{fy_prefix}" (W1)
- In: cloud_preflight.py:40, cloud_sync.py:59, cloud_reporter.py:25, 43, 70 (3x), cloud_verify.py:31.
- Trivial to extract: core/rclone_config.py:_remote_dest(bucket, fy_prefix). Not in scope for this audit, but called out in CODE_REVIEW_2026-06-01.md.
Anti-patterns
- wol.py:30-36 — _send_magic_packet wraps OSError with a new OSError (CODE_REVIEW_2026-06-01.md:I7). The "no-op anti-pattern": re-raising the same exception type with a stringified message loses the original traceback context.
- Fix: either let the original OSError propagate (raise) or define class WolSendError(RuntimeError) for type-safety.
- wol.py:10 — from wakeonlan import send_magic_packet as wol_send. The alias is unnecessary and slightly hurts grep-ability.
- Fix: import wakeonlan + wakeonlan.send_magic_packet(...).
- wol.py:39-62 — wait_for_server uses linear polling. Acceptable for a 300s timeout with 15s interval, but exponential backoff would converge faster on average.
- Fix (optional): delay = min(ping_interval, 1 * 2 ** attempts).
- lan_sync.py:107 — log tail only captured on FAILED, not PARTIAL. Bit-3 set (code 8-15) means "some files failed to copy" — exactly when the user wants the log tail.
- Fix: change if status == "LAN_FAILED": to if status in ("LAN_FAILED", "LAN_PARTIAL"):.
- shutdown.py:14 — hardcoded 300-second delay. Not a config value. See Recommendations.
- shutdown.py:33 — no creationflags=CREATE_NO_WINDOW. Inconsistent with WINDOWS_LEARNINGS.md:1-20 guidance. Low risk for shutdown.exe (Windows built-in) but inconsistent.
UNC/safety issues
- lan_manifest.py:13-43 — assumes path is reachable, no SMB-port check. The caller (lan_snapshot_after_task in flow.py:209) will fail with an uncaught OSError(WinError 53). This propagates up to _run_lan_pipeline and fails the whole run. Defensible behavior (fail loud), but if a production deployment wanted "best-effort recording," wrapping the walk in try/except and returning [] would be the lever.
- lan_manifest.py:25 — str(Path(unc_path).resolve()). On Windows, Path.resolve() on a UNC path follows symlinks. For a /MIR destination, this is fine (the share is a flat destination, no symlinks), but worth noting. If the share contains symlinks pointing outside the share, the resolved path could escape the base, and os.path.relpath could produce paths with .. components.
- shutdown.py:11-50 — no server-identity verification beyond the IP. A misconfigured wol.server_ip could shut down the wrong host. Mitigations (none currently implemented): verify the resolved hostname matches wol.server_name if added to config; verify the same MAC that was sent the WoL responds to ARP; or have the server confirm receipt by calling back.
- lan_sync.py:80 (in the prompt) — os.path.join on UNC paths. Verified safe: os.path.join("\\\\server\\share", "sub", "file.txt") returns "\\\\server\\share\\sub\\file.txt". The lan_sync.py file doesn't actually use os.path.join at all — only os.close on line 89. The os.path.join is in lan_manifest.py:29, and it's also safe. ✅
Recommendations (priority order)
 1. lan_sync.py:107 — capture log tail on PARTIAL too (5 min)
Change if status == "LAN_FAILED": → if status in ("LAN_FAILED", "LAN_PARTIAL"):. The LAN_PARTIAL state is the user-facing "something went wrong, look at the log" signal — suppressing the log on that branch is a UX bug.
 2. shutdown.py:11-50 — promote shutdown_delay_seconds to config (15 min)
Add to LanConfig in models/config.py:
shutdown_delay_seconds: int = Field(default=300, ge=0, le=3600)
shutdown_command_timeout_seconds: int = Field(default=30, ge=5, le=300)
Read both in shutdown_server(server_ip, delay, timeout) -> dict. This is the highest-value change because it unblocks test coverage of the shutdown path with a 0-second delay.
 3. wol.py:30-36 — fix the OSError-wrapping anti-pattern (5 min)
Either delete the try/except and let the original OSError propagate, or define class WolSendError(RuntimeError) and raise that. Either way, the traceback chain is preserved.
 4. wol.py:10 — remove the as wol_send alias (1 min)
import wakeonlan + wakeonlan.send_magic_packet(...). Pure style.
 5. Extract _run_with_log(cmd, *, timeout, binary_name, capture_stderr_to_file) -> SubprocessResult helper (60 min, high payoff)
Replace 7 sites of the try/except/except/except pattern. Returns a typed result; each caller interprets the result into its own envelope ({"status", ...} for sync, {"ok", ...} for preflight). Add core/subprocess_runner.py with the helper.
 6. lan_manifest.py — add get_lan_size for symmetry with get_cloud_size (30 min)
Single-line: return {"count": len(files), "bytes": sum(f["size"] for f in files), "sizeless": "0"}. Closes the symmetry gap noted in the Symmetry table. Trivial but useful for the dashboard.
 7. lan_manifest.py:13 — guard against offline share with try/except returning [] (10 min, design decision)
Wrap os.walk(unc_path) in try/except OSError. On offline, log a warning and return [] so lan_record_task can still record "no new files" instead of failing the whole run. This is a behavior change — current behavior is fail-loud, which is also defensible. Recommend not doing this unless ops wants it; flag for product decision.
 8. shutdown.py:33 — add creationflags=subprocess.CREATE_NO_WINDOW on Windows (1 min)
Matches the pattern from WINDOWS_LEARNINGS.md:1-20. Low risk for shutdown.exe but consistent with the rest of the project.
 9. wol.py:39-62 — exponential backoff in wait_for_server (15 min, optional)
delay = 1; while not _smb_port_open(): sleep(delay); delay = min(delay * 2, ping_interval). Reaches the success state ~2x faster on average; on timeout, just exits the loop normally.
10. lan_sync.py:30-34 — add code 0 line to the docstring (1 min)
Note that "code 0 = no copy needed, source/dest in sync" is also COMPLETE. Future readers will appreciate the explicit mention.
Test coverage observed
- 66 tests pass across the 5 modules (test_lan_sync.py: 31, test_lan_preflight.py: 6, test_lan_manifest.py: 14, test_wol.py: 11, test_shutdown.py: 4).
- The classify_exit_code test matrix covers all 5 bit categories (0, 1, 2, 4, 7, 8, 9, 16, 24, -1) — full bitmask coverage.
- The walk_lan_destination tests don't cover the offline-share case (no test that asserts walk_lan_destination("\\\\nonexistent\\share") raises or returns []). Recommendation 7 would require a new test.
Files NOT changed
Read-only audit per instructions. All recommendations are advisory.
