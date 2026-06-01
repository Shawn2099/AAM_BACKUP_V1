Chunk 2 Audit: core/manifest.py + core/backup_repository.py
Methods analyzed (ManifestDB, 20 methods, 518 lines)
#	Method	Line	Verdict
1	__init__	71	OK — minimal init; path/string cast; mkdir(parents=True) at construction is correct.
2	_get_conn	77	Thread-unsafe init (see issues); ad-hoc pre-migration dedup; DDL runs every connect. **[FALSE — thread-safety claim: every caller holds self._lock when calling _get_conn(); grep confirms all 17 call sites are inside `with self._lock:` blocks]**
3	close	115	OK — silent close under lock.
4	upsert_file_entry	126	OK — parameterized, normalized paths, lock+commit; CASE-based "preserve first sync time" is a thoughtful design choice.
5	bulk_upsert_synced	180	OK — f-string SQL with allowlisted mode, chunked executemany, single commit. Logically duplicates upsert_file_entry.
6	mark_lan_synced	245	Verbatim duplicate of mark_cloud_synced (L263) with only the column name swapped.
7	mark_cloud_synced	263	See above.
8	delete_entries	281	OK — f-string with generated ? placeholders (not user data); chunked at 500.
9	update_checksums	297	OK — parameterized, no mode distinction (correct — md5 is mode-agnostic).
10	get_entry	311	OK — parameterized, returns dict.
11	file_count	320	OK — f-string SQL with {"lan_status","cloud_status"} allowlist (L321–323). Safe.
12	get_cloud_synced_entries	331	Hardcoded to cloud_status, parallel to get_synced_paths(mode). Inconsistent.
13	get_synced_paths	345	OK — allowlisted mode; overlaps with get_cloud_synced_entries.
14	prune_stale_synced	360	Two-phase lock (call + return) is inefficient; DELETE-trailing-totals runs every call.
15	insert_run	398	OK — required-key guard, ON CONFLICT upsert.
16	get_runs_since	437	No LIMIT — unbounded result; parallel to get_recent_runs(limit).
17	last_run	458	LIMIT 1 of a get_recent_runs-shaped query. Could unify.
18	get_recent_runs	472	OK.
19	wal_checkpoint	483	No return value — caller can't detect failure.
20	purge_old_runs	489	OK — conditional VACUUM based on freelist (best practice).
Methods analyzed (backup_repository.py, 2 functions, 82 lines)
#	Function	Line	Verdict
1	record_sync_results	12	OK — normalizes rclone (Path/Size/ModTime) and walk (path/size/mtime) dict formats; chained bulk_upsert_synced → prune_stale_synced → delete_entries. The 3-key dict-coercion pattern e.get("X") if e.get("X") is not None else e.get("x", default) is repeated 3 times in L32–34.
2	record_run_history	50	Swallows exceptions silently at L81–82 — caller cannot detect recording failure. Drops files_failed, even though insert_run schema supports it.
Cross-file duplications (verified by grep + gitnexus_impact)
Duplication	Locations	Resolution
mark_lan_synced ≡ mark_cloud_synced	manifest.py:245 vs manifest.py:263	Verbatim duplicate except column names lan_* ↔ cloud_*. Already mitigated by bulk_upsert_synced. Both mark_*_synced methods are still called from tests (test_manifest.py:41, 49, 55–56, 96–97) and test_manifest_edge_cases.py:96–97 but zero production callers — they are dead code.
record_sync_results (backup_repository.py:12) vs bulk_upsert_synced (manifest.py:180)	backup_repository:30–47 wraps bulk_upsert_synced + prune_stale_synced + delete_entries	bulk_upsert_synced is the canonical atomic bulk operation; record_sync_results is the canonical orchestration. No duplication — the layering is clean.
get_cloud_synced_entries (manifest.py:331) vs get_synced_paths("cloud") (manifest.py:345)	Manifest:331–343 vs Manifest:345–358	Same SQL pattern with different return shape. get_cloud_synced_entries is called from flow.py:355 for the cloud differential calc; get_synced_paths is called only by prune_stale_synced (manifest.py:378). Could be unified into one get_synced_entries(mode) -> {path: (size, mtime)} method.
last_run(mode) (manifest.py:458) vs get_recent_runs(1) (manifest.py:472)	—	Two methods with near-identical SQL and ORDER BY. Could fold into one.
_run_cloud_pipeline and _run_lan_pipeline (flow.py:334, 436)	flow.py:334–429 vs flow.py:436–497	Both manually ManifestDB(...) + try/finally db.close() 7 times each (flow.py:159–166, 234–246, 352–359, 511–523, 542–547, 562–567, 651–654). No context manager (__enter__/__exit__) on ManifestDB.
record_sync_results 3× get-with-fallback (backup_repository.py:32–34)	—	Same e.get("X") if e.get("X") is not None else e.get("x", default) template repeated for Path/Size/ModTime. Also duplicated in flow.py:380–382 for the cloud-differential calculation. Four copies in total.
SQL/correctness issues
#	File:line	Severity	Issue
1	manifest.py:78	HIGH (race)	if self._conn is None: is outside self._lock. Two threads racing on first call both create a connection; the second one overwrites self._conn and the first becomes leaked (file handle + WAL handle). Move the None check inside the lock or use double-checked locking.
2	manifest.py:97, 108, 511	LOW	conn.commit() inside _get_conn for the pre-migration dedup and ALTER TABLE. This implicitly uses an outer transaction (sqlite3 default), but mixing this with the auto-DDL executescript on the same connection can interleave autocommit/transaction state in surprising ways. The cleaner pattern is with conn: for the whole init block, then return conn.
3	manifest.py:84–99	MEDIUM	Pre-migration dedup is ad-hoc (try/except → logger.debug). Two processes starting simultaneously each run the dedup before CREATE UNIQUE INDEX is created. The busy_timeout=30000 masks SQLITE_BUSY but not silent double-DML. A real migration system (e.g., db_meta.schema_version already half-built in DDL) would prevent this.
4	manifest.py:15, 64	MEDIUM	SCHEMA_VERSION = 1 is defined and inserted into db_meta, but never read anywhere. The migration logic instead uses PRAGMA table_info() to detect the extended_metrics column. Dead constant.
5	manifest.py:201, 223, 385	LOW	status_field = f"{mode}_status" and ts_field = f"{mode}_last_synced_at" are safe because mode is validated against {"cloud","lan"} at the function entry — but the safety relies on the allowlist being correct. A future maintainer who adds "all" to the validator without checking the column name would introduce SQL injection. The file_count and get_synced_paths methods have the same pattern.
6	manifest.py:212, 291	LOW	f-string SQL: f"""...{status_field}...""" (L212, 291, 327, 356, 385). All are safe due to allowlists, but this is the No. 1 SQL injection risk in the file — recommended mitigation is to switch to a column-name→column-name dict, not f-string interpolation: _STATUS_COLUMN = {"cloud": "cloud_status", "lan": "lan_status"}.get(mode).
7	manifest.py:154–162	LOW	The CASE WHEN excluded.X_status = 'synced' AND file_entries.X_status != 'synced' pattern in upsert_file_entry only updates the timestamp on the first transition to synced. Re-syncing the same file does not update lan_last_synced_at or cloud_last_synced_at. This is a design choice (preserve first sync time) but is inconsistent with bulk_upsert_synced (L224–228) which has identical behavior — both methods agree, but neither matches what most users would expect from the column name "last_synced_at".
8	manifest.py:209–242	LOW	The bulk_upsert_synced chunks at 100 rows (~700 params) — the comment says SQLITE_MAX_VARIABLE_NUMBER=999 is the limit. executemany with 8 columns × 100 rows = 800 params. Off by one is the difference between success and too many SQL variables. 99 would be safer.
9	manifest.py:288–294	LOW	delete_entries chunks at 500 paths. With the ON DELETE cascade disabled and 500 placeholders, this is well under 999. But executemany is more idiomatic for bulk DELETEs than execute with a long IN clause.
10	manifest.py:360–394	MEDIUM	prune_stale_synced calls get_synced_paths(mode) (which acquires & releases self._lock) and then re-acquires the lock at L382. The window between releases is small but non-zero — files added by another thread between the two locks will not be pruned (correct, since they're not stale) but also won't be excluded from the NULL-or-NULL DELETE. Acceptable but easy to fix by inlining get_synced_paths under a single lock.
11	manifest.py:389–392	LOW	The trailing DELETE FROM file_entries WHERE lan_status IS NULL AND cloud_status IS NULL runs on every call to prune_stale_synced, even when no rows were just nulled out. Should be gated on len(stale_paths) > 0.
12	manifest.py:437–456	MEDIUM	get_runs_since(days, mode) has no LIMIT and is the path used by core/report.py:113 (weekly/monthly reports). For a long-running prod with 5 years of daily runs in run_history and a 7-day filter, this returns ~14 rows (fine). But get_runs_since(0, ...) would return all runs — the cutoff_iso function returns now - days, so days=0 is essentially all of them. The implementation has no upper bound.
13	manifest.py:483–487	LOW	wal_checkpoint has no return value. The sqlite3 PRAGMA wal_checkpoint returns (busy, log_pages, checkpointed_pages). A caller wanting to know if the WAL was actually truncated has no way to detect it. record_run_history in backup_repository.py:80 calls it but discards the result.
14	manifest.py:101	LOW	conn.executescript(DDL) runs on every connection open (every first call to _get_conn in every process). With IF NOT EXISTS on every DDL statement this is idempotent and cheap, but it does 5 PRAGMA + 3 CREATE + 3 INDEX + 1 INSERT queries every time. For a long-lived connection that opens once per process (the normal case) this is fine.
15	manifest.py:74	LOW	self._lock = threading.Lock() is held during all read AND write operations. With WAL mode + check_same_thread=False, multiple readers can run concurrently. The current design serializes all access — correct for writes (only one writer), wasteful for reads. A threading.RLock or a reader-writer lock (threading.Lock for writes only) would be more efficient.
16	backup_repository.py:81–82	HIGH	record_run_history wraps db.insert_run(...) + db.wal_checkpoint() in a try/except that logs the error and returns. The caller in flow.py:511–523 will never know the run was not recorded. Subsequent retries will fail to find the run_id (because it was never inserted) and the run history is silently lost. This is the only place in the codebase that swallows a DB exception.
17	backup_repository.py:50–64	MEDIUM	record_run_history signature accepts files_copied, bytes_copied, error_message but does not accept files_failed — even though ManifestDB.insert_run schema (L398) supports it. flow.py:510–523 constructs the dict in _record_run and could pass it but doesn't. The cloud run_data dict at flow.py:506 is a passthrough.
18	backup_repository.py:32–34	LOW	The triple e.get("X") if e.get("X") is not None else e.get("x", default) pattern is duplicated 3 times in one function. Extract a helper: def _key(d, primary, fallback, default): return d.get(primary) if d.get(primary) is not None else d.get(fallback, default). The same pattern is duplicated again in flow.py:380–382.
19	flow.py:158–166, 234–246, 352–359, 511–523, 542–547, 562–567, 651–654	MEDIUM	ManifestDB is opened and closed manually with try/finally db.close() in 7 places. A context manager (__enter__/__exit__) on the class would eliminate this boilerplate.
Anti-patterns
#	File:line	Pattern	Fix
A1	manifest.py:137, 204, 250, 268, 286, 301, 313, 324, 337, 353, 382, 403, 439, 459, 473, 485, 499	Manual with self._lock: + conn.commit() instead of with self._lock, conn:	sqlite3 connection context manager auto-commits on __exit__ and rolls back on exception. The codebase has 19 manual with self._lock blocks — all should pair with with conn:.
A2	manifest.py:208, 226, 261, 278, 295, 309, 393, 435, 511, 518	Manual conn.commit() calls	Replace with with conn: block.
A3	manifest.py:106, 322, 348, 357, 414, 440, 460, 471, 476, 481	conn.execute(...).fetchone() / conn.execute(...).fetchall() chained returns	OK, this is actually idiomatic — fetchone after a single-statement execute is the right pattern. No change needed.
A4	manifest.py:97, 110, 120, 518	try/except Exception → logger.debug("skipped")	These are intentional "swallow and continue" patterns for migration code. The DDL exception at L99 is genuinely expected (legacy vs fresh DB). The close() at L120 is in a destructor-like context. OK as-is.
A5	backup_repository.py:81–82	try/except Exception → logger.error + return None	Bug — caller cannot distinguish success from failure. The caller in flow.py:511–523 has no return value to check. Either re-raise, return a result, or document that this function is fire-and-forget.
A6	manifest.py:15, 17–65, 104–110	Hardcoded DDL + ad-hoc migration	The schema is split between DDL (L17–65) and an in-line ALTER TABLE migration (L104–110). The SCHEMA_VERSION = 1 constant is dead. A versioned migration system (e.g., migrations.py with migration_v1.py, migration_v2.py) and a real check on db_meta.schema_version would be cleaner.
A7	manifest.py:126, 297, 311, 320, 331, 345, 398, 437, 458, 472	Returns dict (from sqlite3.Row)	Pydantic/dataclass model would be more type-safe. The codebase already uses Pydantic for AppConfig (models/config.py:189). However, the dict shape mirrors SQL column order and is convenient for JSON serialization in the UI. Acceptable but a FileEntry and RunRecord dataclass would prevent silent key typos.
A8	manifest.py:329	row["cnt"]	After f"SELECT COUNT(*) as cnt ...", the alias is required. Could simplify to SELECT COUNT(*) ... .fetchone()[0]. Cosmetic.
A9	manifest.py:435	COALESCE(excluded.extended_metrics, run_history.extended_metrics)	Good — preserves existing metrics on partial update.
A10	manifest.py:151, 222, 419	COALESCE(excluded.X, file_entries.X) in three places	Good pattern — preserves existing value when new is NULL.
A11	manifest.py:202, 352, 376	status_field = f"{mode}_status" and similar	The safe version of the f-string SQL pattern, but brittle. See SQL #6.
A12	manifest.py:88, 198, 350, 374	mode not in ("cloud", "lan") allowlist	Repeated 4 times. Extract to a class constant: _MODES = ("cloud", "lan") and a _validate_mode(mode) helper.
A13	manifest.py:288–294	placeholders = ",".join("?" for _ in chunk)	Idiomatic. OK.
A14	manifest.py:104–110	try/except → ALTER TABLE	If a future column is added incorrectly, this silently swallows the error and the next INSERT will fail with a schema error elsewhere. Should at minimum logger.error + raise.
A15	flow.py:354–359, 542–547, 562–567, 651–654	ManifestDB(...) + try/finally db.close() ×7	Add __enter__/__exit__ to ManifestDB so the call sites can use with ManifestDB(path) as db:.
Schema design
Aspect	Status	Notes
Documented schema?	YES	IMPLEMENTATION_PLAN.md:267–325 documents the file_entries and run_history tables. However, it does not document the later-added extended_metrics TEXT column in run_history (DDL L52) or the WAL/busy_timeout PRAGMAs.
Schema-versioning system?	HALF-BUILT	SCHEMA_VERSION = 1 is defined and db_meta table is created, but no code reads it. The actual migration logic uses PRAGMA table_info(run_history) to detect missing columns. This will break if a column is renamed or its type changes (cannot detect).
md5_checksum type	TEXT	Storing MD5 hex digests as TEXT (32 chars) is standard. No issue. Could be BLOB (16 bytes) for 50% size savings but TEXT is more debuggable.
relative_path collation	NOCASE	Smart — Windows path comparison is case-insensitive by default, and UNC paths are case-insensitive.
lan_status / cloud_status typed?	No	TEXT DEFAULT 'unknown'. Status values documented in the plan (L282–283) as "unknown, synced, failed, deleted" but not enforced at the DB level. No CHECK constraint, no enum. A typo like "sync" would silently insert. SQLite supports CHECK constraints and the project would benefit.
WAL mode	Set in DDL	PRAGMA journal_mode=WAL in DDL (L18). This is set every time _get_conn runs executescript(DDL). Idempotent — sqlite ignores the second WAL set. OK.
busy_timeout=30000	Set in DDL	30-second wait on SQLITE_BUSY. Sensible.
foreign_keys=ON	Set in DDL	PRAGMA, not in DDL string but in same executescript. Note: PRAGMA foreign_keys is a no-op inside a transaction in older SQLite builds — but executescript may run in implicit transaction. Verify the DDL executes outside a transaction (sqlite3 executescript first issues COMMIT before running, so this is OK).
PRAGMA journal_mode=WAL is a per-database setting	CORRECT	The pragma is sticky once set on a connection. executescript will re-issue it on every new connection but it's a no-op after the first.
Race condition: two processes set WAL simultaneously?	MITIGATED	First process creates the DB file → sets WAL. Second process opens existing DB → PRAGMA journal_mode=WAL returns the current mode (no-op). OK.
Indexes on lan_status / cloud_status	PRESENT	L36–37. Good for WHERE X_status = 'synced' queries.
Index on run_history(started_at)	PRESENT	L55. Good for ORDER BY started_at DESC.
UNIQUE INDEX on run_history(run_id)	PRESENT	L57. Good — combined with ON CONFLICT(run_id) DO UPDATE in insert_run, enables upsert.
INSERT OR IGNORE INTO db_meta VALUES ('schema_version', '1')	GOOD	Seeded once. But nothing reads it.
How the 10 importers use the class
Importer	Methods called
flow.py:159 cloud_record_task	bulk_upsert_synced (via backup_repository), close
flow.py:234 lan_record_task	bulk_upsert_synced, prune_stale_synced, delete_entries (via backup_repository), close
flow.py:352 _run_cloud_pipeline	get_cloud_synced_entries, close
flow.py:511 _record_run	insert_run (via backup_repository), close
flow.py:542, 562 weekly/monthly report flows	get_runs_since (via report), close
flow.py:651 end of backup()	purge_old_runs, close
ui.py:133 dashboard _DB_INSTANCE	get_recent_runs, file_count, last_run (lines 279, 315, 316, 513, 520, 585, 586, 609)
core/report.py:113 generate_report_html	get_runs_since
tests/test_manifest.py	every method except get_synced_paths
tests/test_manifest_edge_cases.py	bulk_upsert_synced, insert_run, delete_entries, mark_lan_synced, mark_cloud_synced, file_count, get_entry
tests/test_edge_cases.py	bulk + concurrency
tests/test_backup_repository.py	record_sync_results, record_run_history (via backup_repository)
tests/test_workflows.py	bulk_upsert_synced, insert_run, purge_old_runs, last_run, get_recent_runs, get_entry, file_count
tests/test_ui.py	mocked (MagicMock)
tests/test_flow_orchestration.py	mocked (MagicMock)
Key observation: of the 10 importer files, only flow.py and ui.py are production code. The other 8 are tests. The 5 manifest.py methods get_synced_paths, mark_lan_synced, mark_cloud_synced, update_checksums, wal_checkpoint are called from production in only 1 path (wal_checkpoint via record_run_history → _record_run). The mark_*_synced methods are dead in production.
Recommendations (concrete, file:line)
Priority	File:line	Recommendation
P0	backup_repository.py:81–82	Re-raise or return success flag from record_run_history. Silent swallow loses run history.
P0	manifest.py:78	Move if self._conn is None: inside with self._lock: or use double-checked locking.
P1	manifest.py:245, 263	Delete mark_lan_synced and mark_cloud_synced — zero production callers, replaced by bulk_upsert_synced. Keep the public tests (or update them). Saves ~35 lines.
P1	manifest.py:331, 345	Unify get_cloud_synced_entries and get_synced_paths into one get_synced_entries(mode) -> {path: (size, mtime)}.
P1	backup_repository.py:32–34	Extract _key(d, primary, fallback, default) helper. Reuse in flow.py:380–382.
P1	flow.py:159, 234, 352, 511, 542, 562, 651	Add __enter__/__exit__ to ManifestDB and convert all ManifestDB(path) + try/finally close() to with ManifestDB(path) as db:.
P2	manifest.py:104–110	Either commit to the db_meta.schema_version system or remove the dead SCHEMA_VERSION constant + db_meta table. Currently both are half-implemented.
P2	manifest.py:201, 223, 352, 376, 385	Replace f"{mode}_status" with a class constant: _STATUS_COL = {"cloud": "cloud_status", "lan": "lan_status"}. Same for _TS_COL. Eliminates the "future maintainer adds 'all' to allowlist" footgun.
P2	manifest.py:198, 350, 374	Extract _validate_mode(mode) helper and _MODES = ("cloud", "lan") constant. Called 4×.
P2	manifest.py:389–392	Gate the trailing DELETE ... WHERE lan_status IS NULL AND cloud_status IS NULL on len(stale_paths) > 0.
P2	manifest.py:458, 472	Implement last_run(mode) as get_recent_runs(1, mode=mode) (which would require adding mode to get_recent_runs signature).
P2	manifest.py:209	Change chunk size from 100 to 99 in bulk_upsert_synced to be safely under SQLITE_MAX_VARIABLE_NUMBER=999 even on 8-column tables. Currently 100 × 8 = 800 → fine, but 100 × 9 = 900 → still fine, 100 × 10 = 1000 → fails. Bumping to 11 columns later would be silent corruption.
P2	manifest.py:483	Have wal_checkpoint return (busy, log, checkpointed) so callers can detect failures.
P2	manifest.py:437	Add an upper bound (e.g., 10,000 rows) to get_runs_since.
P2	manifest.py:126, 154–162	Document the "preserve first sync time" design decision in the docstring, since lan_last_synced_at reads as if it should be the most recent.
P2	manifest.py:73	Hold threading.Lock for writes only; readers can use a separate RLock or no lock.
P3	manifest.py:15, 17–65	Add CHECK (lan_status IN ('unknown', 'synced', 'failed', 'deleted')) constraints to file_entries. SQLite supports CHECK since 3.3.0.
P3	core/report.py:123–124	r["files_copied"] (without .get) will KeyError if a legacy row exists; the same for r["bytes_copied"]. Add .get(..., 0).
P3	backup_repository.py:50–64	Add files_failed: int = 0 to signature and pass through to insert_run.
P3	ui.py:315, 316, 585, 586	db.file_count("lan_status") called twice in the same /status request — could cache.
P3	flow.py:354–359, 542–547, 562–567	Each manually ManifestDB(...) + try/finally — same boilerplate, see P1.
P3	manifest.py:518	else: conn.commit() (the non-VACUUM branch) commits after the DELETE — this is correct but could be unified into a single with conn: pattern that auto-commits the DELETE and lets the VACUUM run auto-commit at the end.
Summary
ManifestDB is well-engineered for a single-writer backup tool. The core design (WAL + per-process connection + thread lock + parameterized SQL) is sound. The INSERT ... ON CONFLICT ... DO UPDATE upsert pattern is used correctly. The f-string SQL with mode allowlist is safe but should be hardened via a column-name dict to prevent future regressions.
Three real bugs found:
1. manifest.py:78 — _get_conn is not thread-safe on first call.
2. backup_repository.py:81–82 — record_run_history silently swallows DB errors.
3. backup_repository.py:50–64 — drops files_failed data.
Two clear dead-code removals:
1. mark_lan_synced / mark_cloud_synced — replaced by bulk_upsert_synced in production, only used in 2 test files.
2. SCHEMA_VERSION constant + db_meta table — defined but never read.
One structural cleanup:
- ManifestDB should implement __enter__/__exit__ to eliminate 7 try/finally db.close() blocks in flow.py.
