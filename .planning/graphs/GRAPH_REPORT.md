# Graph Report - /home/shawn/Desktop/aam_backup_automation_V1  (2026-06-01)

## Corpus Check
- 61 files · ~83,886 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1110 nodes · 1929 edges · 74 communities detected
- Extraction: 59% EXTRACTED · 41% INFERRED · 0% AMBIGUOUS · INFERRED: 788 edges (avg confidence: 0.79)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]

## God Nodes (most connected - your core abstractions)
1. `ManifestDB` - 107 edges
2. `LanConfig` - 28 edges
3. `TestManifestDB` - 23 edges
4. `NotificationConfig` - 21 edges
5. `get_fy_prefix()` - 20 edges
6. `_render_dashboard()` - 19 edges
7. `rollover()` - 19 edges
8. `run_cloud_sync()` - 19 edges
9. `load_config()` - 19 edges
10. `classify_rclone_exit()` - 18 edges

## Surprising Connections (you probably didn't know these)
- `backup()` --rationale_for--> `Two Deployments Design`  [INFERRED]
  flow.py → IMPLEMENTATION_PLAN.md
- `backup()` --rationale_for--> `Zero-Import Architecture`  [INFERRED]
  flow.py → IMPLEMENTATION_PLAN.md
- `AAM Backup Automation — Dashboard UI with manual triggers.  FastAPI server on po` --uses--> `ManifestDB`  [INFERRED]
  ui.py → core/manifest.py
- `Cloud backup pipeline: preflight → sync → verify → report → DB.` --uses--> `ManifestDB`  [INFERRED]
  flow.py → core/manifest.py
- `LAN backup pipeline: WoL → preflight → sync → manifest → shutdown.` --uses--> `ManifestDB`  [INFERRED]
  flow.py → core/manifest.py

## Hyperedges (group relationships)
- **Cloud Backup System** — flow_cloud_backup_task, plan_cloud_pipeline, plan_rclone_flags, plan_error_matrix, plan_deletion_tracking, findings_clock_skew, review_hardcoded_coldline [INFERRED 0.85]
- **LAN Backup System** — flow_lan_backup_task, plan_lan_pipeline, plan_robocopy_flags, findings_namedtemporaryfile, findings_network_share, audit_namedtemporaryfile_fix, plan_error_matrix [INFERRED 0.85]

## Communities

### Community 0 - "Community 0"
Cohesion: 0.04
Nodes (60): Backup repository — DB write operations for backup results.  Extracts duplicated, Record sync results to ManifestDB using bulk operations and prune stale entries., Record a backup run to run_history and checkpoint WAL., record_run_history(), record_sync_results(), cloud_record_task(), lan_record_task(), Record cloud sync results to ManifestDB. (+52 more)

### Community 1 - "Community 1"
Cohesion: 0.03
Nodes (72): Exception, generate_report_html(), Generate an HTML report string for the given time period.      Returns "" if no, Tests for dashboard UI — authentication, helpers, rendering, and reports., test_report_downloads_html(), test_report_returns_404_when_no_runs(), test_report_returns_503_when_no_db(), TestApiKeyHeader (+64 more)

### Community 2 - "Community 2"
Cohesion: 0.04
Nodes (42): load_config(), Load and validate configuration from a YAML file., _child_path(), create_new_fy_folders(), detect_rollover(), _fy_name(), _parent_path(), Fiscal year rollover — detects FY boundary, runs final backup of closing FY, cre (+34 more)

### Community 3 - "Community 3"
Cohesion: 0.04
Nodes (27): BaseModel, AppConfig, CloudConfig, DashboardConfig, from_yaml(), MaintenanceConfig, PathsConfig, Pydantic v2 configuration models for AAM Backup Automation V1.  Validated on loa (+19 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (28): LAN backup pipeline: WoL → preflight → sync → manifest → shutdown., Send weekly backup summary report., Send monthly backup summary report., AAM Backup Automation — nightly backup orchestrator.      Modes:         cloud —, Cloud backup pipeline: preflight → sync → verify → report → DB., check_binary_exists(), check_clock_skew(), check_gcs_key() (+20 more)

### Community 5 - "Community 5"
Cohesion: 0.07
Nodes (25): build_rclone_sync_command(), classify_rclone_exit(), Cloud sync — rclone sync wrapper with temp config and exit classification.  Refe, Classify rclone exit code per official documentation.      0  → CLOUD_COMPLETE, Build rclone sync command with GCS-optimized flags., Execute rclone sync to mirror source → GCS.      Creates temp config, executes s, run_cloud_sync(), Tests for cloud_sync — rclone command building, exit classification, and orchest (+17 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (27): NotificationConfig, monthly_report_flow(), Send monthly backup summary report., Reports — failure alerts, weekly/monthly summaries via email.  Reads from Manife, Send aggregated summary report for a time period.      Args:         db: Manifes, Send aggregated summary report via email.      Returns True if email sent, False, Send email via SMTP. Returns True on success., Send email via SMTP. Returns True on success. (+19 more)

### Community 7 - "Community 7"
Cohesion: 0.06
Nodes (39): backup(), cloud_preflight_task(), cloud_publish_artifact_task(), cloud_sync_task(), health_check_task(), lan_preflight_task(), lan_publish_artifact_task(), lan_shutdown_task() (+31 more)

### Community 8 - "Community 8"
Cohesion: 0.05
Nodes (21): Record run history to ManifestDB., _record_run(), Tests for flow.py — decomposed tasks, pipeline orchestration, and failure alerti, test_handles_db_error(), test_records_run(), TestBackupDisabledPipelines, TestBackupModeRouting, TestCloudPreflightTask (+13 more)

### Community 9 - "Community 9"
Cohesion: 0.1
Nodes (26): LanConfig, build_robocopy_command(), LAN sync — robocopy /MIR wrapper with exit code classification.  Reference: AAM_, Classify robocopy exit code using bitmask rules.      Bit 0 (1): Files copied su, Build robocopy /MIR command with production-verified flags.      /V /TS /FP — ve, Build robocopy /MIR command with production-verified flags.      /V /TS /FP /BYT, Execute robocopy /MIR mirror sync.      Robocopy writes all output directly to t, Execute robocopy /MIR mirror sync.      Writes output to temp log file, classifi (+18 more)

### Community 10 - "Community 10"
Cohesion: 0.08
Nodes (19): lan_snapshot_after_task(), lan_snapshot_before_task(), Snapshot LAN destination before sync for diff comparison., Snapshot LAN destination after sync for diff comparison., diff_snapshots(), LAN manifest — walk destination share, produce file inventory + diffs.  No scann, Walk LAN share recursively. Returns every file with size and mtime.      Skips f, Convert walk result to {relative_path: (size, mtime)} for O(1) diff. (+11 more)

### Community 11 - "Community 11"
Cohesion: 0.09
Nodes (31): _base_args(), get_cloud_diff(), get_cloud_manifest(), get_cloud_size(), Cloud reporter — rclone native commands for GCS state reporting.  Every function, rclone size → {"count": int, "bytes": int, "sizeless": str}.      Instant — GCS, rclone lsjson -R → [{Path, Size, ModTime, MimeType, IsDir}, ...].      Files onl, rclone check --combined → {added, removed, modified, unchanged}.      Writes dif (+23 more)

### Community 12 - "Community 12"
Cohesion: 0.11
Nodes (26): Tests for wol — mock socket and wakeonlan., test_already_online_returns_true(), test_immediate_success(), test_os_error_raises(), test_os_error_returns_false(), test_port_closed_returns_false(), test_port_open_returns_true(), test_sends_packet() (+18 more)

### Community 13 - "Community 13"
Cohesion: 0.1
Nodes (25): _cancel_orphaned_runs(), _check_prefect_api(), _ensure_concurrency_limit(), main(), AAM Backup Automation V1 — Single Launch Script.  Starts all three services in o, Start dashboard UI — imports in-thread to avoid early config load., Verify Prefect API server is running. Raises if not reachable., Create the global and tag-based concurrency limits for backup serialization. (+17 more)

### Community 14 - "Community 14"
Cohesion: 0.1
Nodes (10): Dashboard HTML template — pure data-to-HTML rendering.  No imports from ui.py, c, Render the dashboard HTML. Pure function — no I/O, no imports., render_dashboard(), Tests for templates/dashboard.py — pure render function., TestRenderDashboard, Logical workflow tests — end-to-end flow verification.  Tests the actual busines, Template should render correctly with all dashboard data., Template should render correctly with default/empty data. (+2 more)

### Community 15 - "Community 15"
Cohesion: 0.11
Nodes (19): Cloud preflight — rclone check --one-way dry-run before sync.  Fast metadata-onl, Run rclone check --one-way as dry-run validation.      Exit 0 = everything match, run_cloud_dry_run(), Shared rclone temporary config writer — single source of truth.  Used by cloud_p, Write temporary rclone config file for GCS access.      Uses mkstemp + close to, Context manager: write temp config, yield path, auto-cleanup., temp_rclone_config(), write_temp_config() (+11 more)

### Community 16 - "Community 16"
Cohesion: 0.12
Nodes (10): classify_exit_code(), Classify robocopy exit code using bitmask rules.      Bit 0 (1): Files copied su, Exit code classification edge cases., CLOUD_PARTIAL (exit 4-6, 10) completes pipeline., CLOUD_FAILED (exit 1-3, 7-8) aborts pipeline., LAN_PARTIAL (exit 8-15) completes pipeline., LAN_FAILED (exit 16+) aborts pipeline., LAN_COMPLETE (exit 0-7). (+2 more)

### Community 17 - "Community 17"
Cohesion: 0.12
Nodes (19): Backup lock and process detection edge cases., Dead PID → lock removed, backup not running., Alive PID → lock honored, backup detected., No lock file, rclone running → backup detected., No lock, no rclone/robocopy → backup not running., TestWatchdogBackupDetection, _check_health(), _configure_logging() (+11 more)

### Community 18 - "Community 18"
Cohesion: 0.12
Nodes (17): Run one final backup of the closing FY to both destinations.      Returns (cloud, run_final_backup(), Remote backup server shutdown via Windows shutdown.exe.  Reference: AAM_BACKUP_V, Send shutdown command to backup server with 5-minute delay.      Command: shutdo, shutdown_server(), Verify run_cloud_sync is called with correct kwargs — not mocked away., The critical bug: run_cloud_sync must receive gcs_key_path, project_number, loca, Cloud disabled → only LAN backup runs, no cloud call. (+9 more)

### Community 19 - "Community 19"
Cohesion: 0.15
Nodes (7): Tests for fy_router — fiscal year prefix calculation., TestFyPrefix, FY should rollover on April 1., Same FY for all months within a fiscal year., TestFYRoutingWorkflow, get_fy_prefix(), Compute GCS fiscal year folder prefix from IST date.      Fiscal year starts Apr

### Community 20 - "Community 20"
Cohesion: 0.15
Nodes (8): compute_md5(), MD5 checksums — compatible with rclone hashsum md5., Compute MD5 digest for a file using streaming (Python 3.11+ file_digest).      R, Verify file checksum matches expected value.      Returns True if checksum match, verify_checksum(), Tests for hashing — MD5 checksum computation and verification., TestComputeMd5, TestVerifyChecksum

### Community 21 - "Community 21"
Cohesion: 0.24
Nodes (12): LAN preflight — robocopy /L dry-run before real /MIR sync.  Validates UNC reacha, Run robocopy in list-only mode to validate paths and permissions.      /L = list, run_lan_dry_run(), _mock_result(), Tests for lan_preflight — mock subprocess calls., test_exit_0_returns_ok(), test_exit_16_returns_not_ok(), test_exit_7_returns_ok() (+4 more)

### Community 22 - "Community 22"
Cohesion: 0.18
Nodes (8): configure(), configure_prefect_bridge(), Structured logging via Loguru — rotating daily, 30-day retention., Configure Loguru with daily rotating file + stderr output.      Args:         lo, Forward Loguru messages to the active Prefect run logger if running under Prefec, Tests for core/logging.py — configure_prefect_bridge idempotency., Calling configure_prefect_bridge multiple times should only add one sink., TestConfigurePrefectBridge

### Community 23 - "Community 23"
Cohesion: 0.2
Nodes (6): Falsy-value and type edge cases in manifest entry parsing., Size=0, path='', mtime=0 handled correctly with is-not-None checks., Unix timestamps compared directly, no pendulum parse., ISO strings parsed via pendulum., Invalid mtime falls back to string comparison., TestManifestParsing

### Community 24 - "Community 24"
Cohesion: 0.2
Nodes (9): prefect_harness(), Shared fixtures for AAM Backup Automation V1 tests., Create a temporary SQLite database path, cleaned up after test., Create a temporary directory, cleaned up after test., Return minimal valid YAML config string for testing., Start an ephemeral Prefect in-memory database and API for the duration of tests., sample_yaml_config(), temp_db_path() (+1 more)

### Community 25 - "Community 25"
Cohesion: 0.33
Nodes (4): pid_alive(), Cross-platform process utilities — PID alive check, lock helpers., Check if a process is alive (cross-platform)., TestPidAlive

### Community 26 - "Community 26"
Cohesion: 0.33
Nodes (2): Tests for serve.py — deployment creation., TestDeployments

### Community 27 - "Community 27"
Cohesion: 0.4
Nodes (5): NamedTemporaryFile Bug Fix, NamedTemporaryFile Handle Lock, Network Share Operations Constraint, Cloud Backup Pipeline Design, LAN Backup Pipeline Design

### Community 28 - "Community 28"
Cohesion: 0.5
Nodes (4): Clock Skew JWT Rejection, Config YAML UTF-8 Encoding, Implementation Plan, Clock Skew Check Warning

### Community 29 - "Community 29"
Cohesion: 1.0
Nodes (1): Fiscal year prefix router — IST date-based auto-rollover on April 1.

### Community 30 - "Community 30"
Cohesion: 1.0
Nodes (2): Error Handling Matrix, Duplicate Run History Records Blocker

### Community 31 - "Community 31"
Cohesion: 1.0
Nodes (2): Rclone Flag Reference, Hardcoded COLDLINE Warning

### Community 32 - "Community 32"
Cohesion: 1.0
Nodes (0): 

### Community 33 - "Community 33"
Cohesion: 1.0
Nodes (0): 

### Community 34 - "Community 34"
Cohesion: 1.0
Nodes (0): 

### Community 35 - "Community 35"
Cohesion: 1.0
Nodes (0): 

### Community 36 - "Community 36"
Cohesion: 1.0
Nodes (0): 

### Community 37 - "Community 37"
Cohesion: 1.0
Nodes (1): Exit codes 0-7 are OK (bits 0-2 only).

### Community 38 - "Community 38"
Cohesion: 1.0
Nodes (1): Bit 3 (8) = copy errors.

### Community 39 - "Community 39"
Cohesion: 1.0
Nodes (1): Bit 4 (16) = fatal error.

### Community 40 - "Community 40"
Cohesion: 1.0
Nodes (1): Test script: full cloud backup pipeline on Windows Server 2016.

### Community 41 - "Community 41"
Cohesion: 1.0
Nodes (1): Start dashboard UI on port 8080 — imports in-thread to avoid early config load.

### Community 42 - "Community 42"
Cohesion: 1.0
Nodes (1): Remove stale lock files from temp directory on exit.

### Community 43 - "Community 43"
Cohesion: 1.0
Nodes (1): Cancel any PENDING flow runs left over from a previous crashed session.      PEN

### Community 44 - "Community 44"
Cohesion: 1.0
Nodes (1): Raised when a pre-backup health check fails.

### Community 45 - "Community 45"
Cohesion: 1.0
Nodes (1): Verify source drive exists, has files, and has free space.      Returns:

### Community 46 - "Community 46"
Cohesion: 1.0
Nodes (1): Check if binary is available in PATH.

### Community 47 - "Community 47"
Cohesion: 1.0
Nodes (1): Verify GCS service account key file exists.

### Community 48 - "Community 48"
Cohesion: 1.0
Nodes (1): Verify system clock is within acceptable skew (default: 10 minutes).      Compar

### Community 49 - "Community 49"
Cohesion: 1.0
Nodes (1): Run all pre-backup health checks. Raises HealthError on failure.      Args:

### Community 50 - "Community 50"
Cohesion: 1.0
Nodes (1): Run rclone check --one-way to verify source matches GCS.      Exit 0 = everythin

### Community 51 - "Community 51"
Cohesion: 1.0
Nodes (1): SQLite manifest with WAL mode, thread-safe writes.

### Community 52 - "Community 52"
Cohesion: 1.0
Nodes (1): Bulk update: set lan_status='synced' on all given paths.

### Community 53 - "Community 53"
Cohesion: 1.0
Nodes (1): Bulk update: set cloud_status='synced' on all given paths.

### Community 54 - "Community 54"
Cohesion: 1.0
Nodes (1): Delete entries for files no longer on destination.

### Community 55 - "Community 55"
Cohesion: 1.0
Nodes (1): Bulk update md5_checksum for multiple files.

### Community 56 - "Community 56"
Cohesion: 1.0
Nodes (1): Truncate WAL file after backup run to prevent bloat.

### Community 57 - "Community 57"
Cohesion: 1.0
Nodes (1): Delete run_history entries older than retention_days.          Keeps file_entrie

### Community 58 - "Community 58"
Cohesion: 1.0
Nodes (1): Write temporary rclone config file for GCS access.      Uses mkstemp + close to

### Community 59 - "Community 59"
Cohesion: 1.0
Nodes (1): Run rclone check --one-way as dry-run validation.      Exit 0 = everything match

### Community 60 - "Community 60"
Cohesion: 1.0
Nodes (1): Compute GCS fiscal year folder prefix from IST date.      Fiscal year starts Apr

### Community 61 - "Community 61"
Cohesion: 1.0
Nodes (1): Write temporary rclone config file for GCS access.      Uses mkstemp + close to

### Community 62 - "Community 62"
Cohesion: 1.0
Nodes (1): Classify rclone exit code per official documentation.      0  → CLOUD_COMPLETE

### Community 63 - "Community 63"
Cohesion: 1.0
Nodes (1): Build rclone sync command with GCS-optimized flags.

### Community 64 - "Community 64"
Cohesion: 1.0
Nodes (1): Execute rclone sync to mirror source → GCS.      Creates temp config, executes s

### Community 65 - "Community 65"
Cohesion: 1.0
Nodes (1): Load and validate configuration from a YAML file.

### Community 66 - "Community 66"
Cohesion: 1.0
Nodes (1): Windows Server 2016 Deployment Findings

### Community 67 - "Community 67"
Cohesion: 1.0
Nodes (1): ManifestDB Schema Design

### Community 68 - "Community 68"
Cohesion: 1.0
Nodes (1): Deletion Tracking Flow

### Community 69 - "Community 69"
Cohesion: 1.0
Nodes (1): Robocopy Flag Reference

### Community 70 - "Community 70"
Cohesion: 1.0
Nodes (1): Edge Case Audit

### Community 71 - "Community 71"
Cohesion: 1.0
Nodes (1): Insert Run Key Validation Fix

### Community 72 - "Community 72"
Cohesion: 1.0
Nodes (1): Code Review

### Community 73 - "Community 73"
Cohesion: 1.0
Nodes (1): SMTP Connection Leak Warning

## Knowledge Gaps
- **324 isolated node(s):** `Prefect 3 deployment entry point for AAM Backup Automation V1.  Run from project`, `Public entry point — returns (cloud, lan, weekly, monthly) deployments.`, `Create deployments from config. Deferred so import doesn't trigger config load.`, `Return True if request is allowed, False if rate limited.`, `Remove expired sessions. Called on each new session creation.` (+319 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 29`** (2 nodes): `fy_router.py`, `Fiscal year prefix router — IST date-based auto-rollover on April 1.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 30`** (2 nodes): `Error Handling Matrix`, `Duplicate Run History Records Blocker`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 31`** (2 nodes): `Rclone Flag Reference`, `Hardcoded COLDLINE Warning`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 32`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 33`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 34`** (1 nodes): `download_nssm.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 35`** (1 nodes): `download_nssm.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 36`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 37`** (1 nodes): `Exit codes 0-7 are OK (bits 0-2 only).`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 38`** (1 nodes): `Bit 3 (8) = copy errors.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 39`** (1 nodes): `Bit 4 (16) = fatal error.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 40`** (1 nodes): `Test script: full cloud backup pipeline on Windows Server 2016.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 41`** (1 nodes): `Start dashboard UI on port 8080 — imports in-thread to avoid early config load.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 42`** (1 nodes): `Remove stale lock files from temp directory on exit.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 43`** (1 nodes): `Cancel any PENDING flow runs left over from a previous crashed session.      PEN`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 44`** (1 nodes): `Raised when a pre-backup health check fails.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (1 nodes): `Verify source drive exists, has files, and has free space.      Returns:`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (1 nodes): `Check if binary is available in PATH.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (1 nodes): `Verify GCS service account key file exists.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (1 nodes): `Verify system clock is within acceptable skew (default: 10 minutes).      Compar`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (1 nodes): `Run all pre-backup health checks. Raises HealthError on failure.      Args:`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 50`** (1 nodes): `Run rclone check --one-way to verify source matches GCS.      Exit 0 = everythin`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 51`** (1 nodes): `SQLite manifest with WAL mode, thread-safe writes.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 52`** (1 nodes): `Bulk update: set lan_status='synced' on all given paths.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 53`** (1 nodes): `Bulk update: set cloud_status='synced' on all given paths.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 54`** (1 nodes): `Delete entries for files no longer on destination.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 55`** (1 nodes): `Bulk update md5_checksum for multiple files.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 56`** (1 nodes): `Truncate WAL file after backup run to prevent bloat.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 57`** (1 nodes): `Delete run_history entries older than retention_days.          Keeps file_entrie`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 58`** (1 nodes): `Write temporary rclone config file for GCS access.      Uses mkstemp + close to`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 59`** (1 nodes): `Run rclone check --one-way as dry-run validation.      Exit 0 = everything match`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 60`** (1 nodes): `Compute GCS fiscal year folder prefix from IST date.      Fiscal year starts Apr`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 61`** (1 nodes): `Write temporary rclone config file for GCS access.      Uses mkstemp + close to`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 62`** (1 nodes): `Classify rclone exit code per official documentation.      0  → CLOUD_COMPLETE`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 63`** (1 nodes): `Build rclone sync command with GCS-optimized flags.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 64`** (1 nodes): `Execute rclone sync to mirror source → GCS.      Creates temp config, executes s`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 65`** (1 nodes): `Load and validate configuration from a YAML file.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 66`** (1 nodes): `Windows Server 2016 Deployment Findings`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 67`** (1 nodes): `ManifestDB Schema Design`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 68`** (1 nodes): `Deletion Tracking Flow`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 69`** (1 nodes): `Robocopy Flag Reference`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 70`** (1 nodes): `Edge Case Audit`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 71`** (1 nodes): `Insert Run Key Validation Fix`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 72`** (1 nodes): `Code Review`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 73`** (1 nodes): `SMTP Connection Leak Warning`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ManifestDB` connect `Community 0` to `Community 1`, `Community 4`, `Community 6`, `Community 7`, `Community 8`?**
  _High betweenness centrality (0.151) - this node is a cross-community bridge._
- **Why does `load_config()` connect `Community 2` to `Community 0`, `Community 1`, `Community 3`, `Community 6`, `Community 7`, `Community 13`?**
  _High betweenness centrality (0.128) - this node is a cross-community bridge._
- **Why does `rollover()` connect `Community 2` to `Community 18`, `Community 19`, `Community 13`?**
  _High betweenness centrality (0.101) - this node is a cross-community bridge._
- **Are the 85 inferred relationships involving `ManifestDB` (e.g. with `get_db()` and `cloud_record_task()`) actually correct?**
  _`ManifestDB` has 85 INFERRED edges - model-reasoned connections that need verification._
- **Are the 26 inferred relationships involving `LanConfig` (e.g. with `.test_basic_command_structure()` and `.test_mt_flag_from_config()`) actually correct?**
  _`LanConfig` has 26 INFERRED edges - model-reasoned connections that need verification._
- **Are the 17 inferred relationships involving `NotificationConfig` (e.g. with `.test_report_empty_db()` and `.test_report_with_body_html()`) actually correct?**
  _`NotificationConfig` has 17 INFERRED edges - model-reasoned connections that need verification._
- **Are the 18 inferred relationships involving `get_fy_prefix()` (e.g. with `status()` and `_render_dashboard()`) actually correct?**
  _`get_fy_prefix()` has 18 INFERRED edges - model-reasoned connections that need verification._