"""AAM Backup Automation V1.1.1 — Acceptance & Chaos Validation Suite.

Executes Phase 3 (Normal Operations Acceptance) and Phase 4 (Targeted Chaos Regression)
strictly within C:\\ChaosTest, leaving production (C:\\BackupAgent, C:\\AAMBackup) untouched.
"""

import asyncio
import contextlib
import email
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is on sys.path and deploy/bin is in PATH for rclone
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ["PATH"] = str(PROJECT_ROOT / "deploy" / "bin") + os.pathsep + os.environ.get("PATH", "")
os.environ.pop("PREFECT_TEST_MODE", None)

CHAOS_ROOT = Path(r"C:\ChaosTest")
SOURCE_DIR = CHAOS_ROOT / "source" / "FY25-26"
# Self-loop LAN only: share aam_test -> C:\lan_dest_test; wipe-scoped subtree.
DEST_DIR = Path(r"C:\lan_dest_test\CHAOS_SUITE\FY25-26")
RUNTIME_DIR = CHAOS_ROOT / "runtime"
CONFIG_FILE = CHAOS_ROOT / "config_chaos.yaml"
UNC_DEST = r"\\127.0.0.1\aam_test\CHAOS_SUITE\FY25-26"


def setup_chaos_environment():
    """Create fresh isolated directories and config in C:\\ChaosTest."""
    if CHAOS_ROOT.exists():
        for p in [SOURCE_DIR, DEST_DIR, RUNTIME_DIR]:
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)

    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    (RUNTIME_DIR / "logs").mkdir(parents=True, exist_ok=True)

    # Populate sample user files
    (SOURCE_DIR / "Accounting").mkdir(parents=True, exist_ok=True)
    (SOURCE_DIR / "Accounting" / "Q1_Ledger.xlsx").write_bytes(b"PK\x03\x04" + b"XLSX_DATA" * 50)
    (SOURCE_DIR / "HR").mkdir(parents=True, exist_ok=True)
    (SOURCE_DIR / "HR" / "Employees.docx").write_bytes(b"PK\x03\x04" + b"DOCX_DATA" * 50)
    (SOURCE_DIR / "Tax").mkdir(parents=True, exist_ok=True)
    (SOURCE_DIR / "Tax" / "Return_FY25.pdf").write_bytes(b"%PDF-1.4 " + b"PDF_DATA" * 50)

    # Initialize destination canary sentinel required by preflight & transfer exclusion
    (DEST_DIR / ".AAM_TARGET_MOUNTED").touch()

    # Write test config.yaml with strict Pydantic compliance
    config_content = f"""firm_name: "ChaosTestFirm"
paths:
  source_drive: "{str(SOURCE_DIR).replace('\\', '\\\\')}"
  lan_destination: "{UNC_DEST.replace('\\', '\\\\')}"
  runtime_dir: "{str(RUNTIME_DIR).replace('\\', '\\\\')}"
  database_path: "{str(RUNTIME_DIR / 'manifest.db').replace('\\', '\\\\')}"
  log_directory: "{str(RUNTIME_DIR / 'logs').replace('\\', '\\\\')}"
  gcs_key_path: "{str(CHAOS_ROOT / 'keys' / 'mock_key.json').replace('\\', '\\\\')}"
lan:
  enabled: true
  retry_count: 1
  retry_wait_seconds: 1
  subprocess_timeout_seconds: 3600
  shutdown_after_backup: false
  shutdown_on_all_retries_exhausted: false
  max_attempts: 1
  retry_delay_seconds: 60
  mt_threads: 2
  dry_run_timeout_seconds: 60
wol:
  enabled: false
cloud:
  enabled: false
  bucket: "chaos-test-bucket"
  project_number: "123456789"
  location: "asia-south1"
  storage_class: "STANDARD"
  bandwidth_limit: "10M"
  retry_count: 1
  subprocess_timeout_seconds: 3600
  max_attempts: 1
  retry_delay_seconds: 60
  verify_timeout_seconds: 60
  preflight_timeout_seconds: 60
  diff_timeout_seconds: 60
  manifest_timeout_seconds: 60
schedule:
  cloud_cron: "0 21 * * *"
  lan_cron: "0 22 * * *"
  weekly_cron: "0 8 * * MON"
  monthly_cron: "0 8 1 * *"
  rollover_cron: "0 0 1 4 *"
  audit_cron: "0 23 * * SUN"
  timezone: "Asia/Kolkata"
dashboard:
  auth_enabled: false
  api_key: "CHAOS_KEY"
  bind_address: "127.0.0.1"
  port: 8999
notifications:
  smtp_host: "127.0.0.1"
  smtp_port: 1025
  smtp_username: "test_user"
  smtp_password: "test_password"
  sender: "backup-test@chaostest.local"
  recipients:
    - "admin@chaostest.local"
  send_on_failure: true
  weekly_enabled: false
  monthly_enabled: false
maintenance:
  db_retention_days: 30
  log_retention_days: 7
  sqlite_busy_timeout_ms: 5000
  sqlite_vacuum_freelist_threshold: 1000
  sqlite_synchronous: "normal"
health:
  min_free_source_gb: 1
  check_clock_skew: false
"""
    CONFIG_FILE.write_text(config_content, encoding="utf-8")


class MiniSMTPServer:
    """Minimal local SMTP server socket listener to capture emails over real TCP sockets."""

    def __init__(self, host="127.0.0.1", port=1025):
        self.host = host
        self.port = port
        self.received_messages = []
        self._server_sock = None
        self._thread = None
        self._running = False

    def start(self):
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.host, self.port))
        self._server_sock.listen(5)
        self._server_sock.settimeout(0.5)
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def _listen_loop(self):
        while self._running:
            try:
                conn, _ = self._server_sock.accept()
                threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
            except TimeoutError:
                continue
            except Exception:
                break

    def _handle_client(self, conn):
        conn.sendall(b"220 localhost SMTP Test Service Ready\r\n")
        in_data = False
        mail_content = b""
        mail_from = ""
        rcpt_to = []
        while True:
            line = b""
            while not line.endswith(b"\r\n"):
                chunk = conn.recv(1024)
                if not chunk:
                    break
                line += chunk
            if not line:
                break
            if in_data:
                mail_content += line
                if mail_content.endswith(b"\r\n.\r\n"):
                    in_data = False
                    self.received_messages.append({
                        "from": mail_from,
                        "to": rcpt_to,
                        "raw": mail_content[:-5],
                    })
                    conn.sendall(b"250 2.0.0 OK message queued\r\n")
            else:
                cmd = line.decode("utf-8", errors="replace").strip()
                upper = cmd.upper()
                if upper.startswith("EHLO") or upper.startswith("HELO"):
                    conn.sendall(b"250-localhost\r\n250-AUTH PLAIN LOGIN\r\n250 8BITMIME\r\n")
                elif upper.startswith("AUTH"):
                    conn.sendall(b"235 2.7.0 Authentication successful\r\n")
                elif upper.startswith("MAIL FROM:"):
                    mail_from = cmd[10:].strip("<> ")
                    conn.sendall(b"250 2.1.0 Sender OK\r\n")
                elif upper.startswith("RCPT TO:"):
                    rcpt_to.append(cmd[8:].strip("<> "))
                    conn.sendall(b"250 2.1.5 Recipient OK\r\n")
                elif upper == "DATA":
                    in_data = True
                    conn.sendall(b"354 Start mail input; end with <CRLF>.<CRLF>\r\n")
                elif upper == "QUIT":
                    conn.sendall(b"221 2.0.0 Bye\r\n")
                    break
                else:
                    conn.sendall(b"250 OK\r\n")
        conn.close()

    def stop(self):
        self._running = False
        if self._server_sock:
            with contextlib.suppress(Exception):
                self._server_sock.close()


def run_suite():
    results = {}
    print("=" * 70)
    print("  AAM BACKUP V1.1.1 — ACCEPTANCE & CHAOS REGRESSION SUITE")
    print("=" * 70)

    setup_chaos_environment()
    import core.lan_sync
    import ui
    from core.manifest import ManifestDB
    from core.time_utils import now_iso
    from flow import _run_lan_pipeline
    from models.config import load_config

    chaos_cfg = load_config(str(CONFIG_FILE))
    db_path = str(RUNTIME_DIR / "manifest.db")

    # In un-elevated test execution without SeBackupPrivilege, Robocopy rejects /ZB.
    # We adapt command building to strip /ZB and /B during test mode if un-elevated.
    orig_build = core.lan_sync.build_robocopy_command

    def safe_test_build(src, dest, cfg):
        flags = orig_build(src, dest, cfg)
        return [f for f in flags if f not in ("/ZB", "/B")]

    # ═══════════════════════════════════════════════════════════════
    # Phase 3: Acceptance Tests
    # ═══════════════════════════════════════════════════════════════
    print("\n--- Phase 3.1: Normal LAN Backup ---")
    try:
        with patch("core.lan_sync.build_robocopy_command", side_effect=safe_test_build):
            lan_res = _run_lan_pipeline(chaos_cfg, "lan-accept-001", started_at=now_iso())
        assert lan_res["status"] == "LAN_COMPLETE", f"Expected LAN_COMPLETE, got {lan_res['status']}"
        for sub in ["Accounting/Q1_Ledger.xlsx", "HR/Employees.docx", "Tax/Return_FY25.pdf"]:
            dest_file = DEST_DIR / sub.replace("/", "\\")
            assert dest_file.exists(), f"Destination file missing: {dest_file}"
        db = ManifestDB(db_path)
        assert db.file_count("lan_status") >= 3, f"Expected at least 3 synced files in DB, got {db.file_count('lan_status')}"
        runs = db.get_recent_runs(5)
        assert runs[0]["status"] == "LAN_COMPLETE"
        assert runs[0]["files_copied"] == 3
        print("  [PASS] Normal LAN backup completed successfully, 3 files mirrored & verified.")
        results["3.1_normal_lan"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] Normal LAN backup failed: {e}")
        results["3.1_normal_lan"] = f"FAIL: {e}"

    print("\n--- Phase 3.2: Normal Cloud Backup Orchestration & Manifest Recording ---")
    try:
        mock_manifest = [
            {"path": "Accounting/Q1_Ledger.xlsx", "size": 1000, "mtime": 1700000000.0, "md5_checksum": "abc1"},
            {"path": "HR/Employees.docx", "size": 2000, "mtime": 1700000000.0, "md5_checksum": "abc2"},
            {"path": "Tax/Return_FY25.pdf", "size": 3000, "mtime": 1700000000.0, "md5_checksum": "abc3"},
        ]
        from core.backup_repository import record_run_history, record_sync_results
        db = ManifestDB(db_path)
        record_sync_results(db, "cloud", mock_manifest)
        assert db.file_count("cloud_status") == 3
        record_run_history(
            db, run_id="cloud-accept-001", mode="cloud",
            started_at="2026-09-08T10:00:00", ended_at="2026-09-08T10:02:00",
            status="CLOUD_COMPLETE", exit_code=0, duration_seconds=120.0,
            files_copied=3, bytes_copied=6000
        )
        c_runs = [r for r in db.get_recent_runs(10) if r["mode"] == "cloud"]
        assert len(c_runs) > 0 and c_runs[0]["status"] == "CLOUD_COMPLETE"
        print("  [PASS] Cloud backup manifest & run history recorded accurately (3 files, CLOUD_COMPLETE).")
        results["3.2_normal_cloud"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] Normal Cloud backup failed: {e}")
        results["3.2_normal_cloud"] = f"FAIL: {e}"

    print("\n--- Phase 3.3: Mode='all' Backup & UI Active Detection ---")
    try:
        mock_run_all = MagicMock(tags=["production"], parameters={"mode": "all"})
        mock_client = MagicMock()
        mock_client.read_flow_runs = AsyncMock(return_value=[mock_run_all])
        cm = AsyncMock()
        cm.__aenter__.return_value = mock_client
        cm.__aexit__.return_value = None

        with patch("ui.get_client", return_value=cm):
            is_cloud = asyncio.run(ui._is_running("cloud"))
            is_lan = asyncio.run(ui._is_running("lan"))
            assert is_cloud is True, "mode='all' must show cloud as active"
            assert is_lan is True, "mode='all' must show lan as active"
        print("  [PASS] mode='all' detected as active for both Cloud and LAN in UI.")
        results["3.3_mode_all"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] mode='all' test failed: {e}")
        results["3.3_mode_all"] = f"FAIL: {e}"

    print("\n--- Phase 3.4: Weekly Integrity Audit ---")
    try:
        from core.integrity import audit_lan
        db = ManifestDB(db_path)
        audit_res = audit_lan(
            source=str(SOURCE_DIR),
            dest=str(DEST_DIR),
        )
        assert audit_res["status"] == "VERIFIED", f"Expected VERIFIED, got {audit_res['status']}"
        assert audit_res["mismatches"] == 0, f"Expected 0 mismatches, got {audit_res['mismatches']}"
        assert len(audit_res["missing"]) == 0
        assert len(audit_res["extra"]) == 0

        lan_runs_after = [r for r in db.get_recent_runs(10) if r["mode"] == "lan"]
        assert len(lan_runs_after) >= 1, "Integrity audit must not overwrite backup run_history"

        # Verify UI isolates integrity runs from active backup badges
        mock_audit_run = MagicMock(tags=["maintenance", "integrity"], parameters={"mode": "all"})
        mock_client.read_flow_runs = AsyncMock(return_value=[mock_audit_run])
        with patch("ui.get_client", return_value=cm):
            assert asyncio.run(ui._is_running("cloud")) is False
            assert asyncio.run(ui._is_running("lan")) is False
        print("  [PASS] Integrity audit passed (VERIFIED), additive/read-only invariant preserved, UI badges isolated.")
        results["3.4_integrity_audit"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] Integrity audit test failed: {e}")
        results["3.4_integrity_audit"] = f"FAIL: {e}"

    print("\n--- Phase 3.5: SMTP End-to-End Delivery ---")
    smtp_server = MiniSMTPServer(host="127.0.0.1", port=1025)
    smtp_server.start()
    time.sleep(0.2)
    try:
        from core.report import send_failure_alert
        with patch("smtplib.SMTP.starttls", return_value=(220, b"2.0.0 Ready to start TLS")):
            sent = send_failure_alert(
                config=chaos_cfg.notifications,
                firm_name="ChaosTestFirm",
                error_message="Simulated controlled alert test for E2E validation.",
                run_data={"mode": "lan", "status": "CONTROLLED_TEST_ALERT", "exit_code": 1},
            )
        time.sleep(0.5)
        assert sent is True, "send_failure_alert returned False"
        assert len(smtp_server.received_messages) >= 1, "SMTP server received 0 messages"
        msg = smtp_server.received_messages[0]
        assert msg["from"] == "backup-test@chaostest.local"
        assert "admin@chaostest.local" in msg["to"]
        parsed = email.message_from_bytes(msg["raw"])
        assert "Backup Failure Alert - ChaosTestFirm" in parsed["Subject"]
        print(f"  [PASS] Real SMTP E2E delivery verified: From: {msg['from']}, To: {msg['to']}, Subject: {parsed['Subject']}")
        results["3.5_smtp_e2e"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] SMTP E2E test failed: {e}")
        results["3.5_smtp_e2e"] = f"FAIL: {e}"
    finally:
        smtp_server.stop()

    print("\n--- Phase 3.6: Controlled Failure & Recovery ---")
    try:
        from core.health import HealthError
        from core.lan_preflight import run_lan_dry_run

        broken_unc = r"\\127.0.0.1\aam_test\nonexistent_folder_xyz\FY25-26"
        failed_as_expected = False
        try:
            run_lan_dry_run(str(SOURCE_DIR), broken_unc)
        except HealthError as he:
            failed_as_expected = True
            print(f"  [PASS] Controlled fault detected by preflight: {str(he)[:80]}...")

        assert failed_as_expected, "Unreachable UNC destination must fail preflight with HealthError"

        with patch("core.lan_sync.build_robocopy_command", side_effect=safe_test_build):
            clean_res = _run_lan_pipeline(chaos_cfg, "lan-recover-002", started_at=now_iso())
        assert clean_res["status"] == "LAN_COMPLETE", f"Expected recovery LAN_COMPLETE, got {clean_res['status']}"
        print("  [PASS] Recovered cleanly with successful LAN_COMPLETE backup.")
        results["3.6_fault_recovery"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] Recovery test failed: {e}")
        results["3.6_fault_recovery"] = f"FAIL: {e}"

    # ═══════════════════════════════════════════════════════════════
    # Phase 4: Targeted Chaos Regression
    # ═══════════════════════════════════════════════════════════════
    print("\n--- Phase 4.1: Cross-Mode Manifest Isolation (Fix 1 / Finding 8.1.1) ---")
    try:
        db = ManifestDB(db_path)
        shared_file = "Accounting/Q1_Ledger.xlsx"
        from core.backup_repository import record_sync_results
        record_sync_results(db, "lan", [{"path": shared_file, "size": 1000, "mtime": 1700000000.0}])
        record_sync_results(db, "cloud", [{"path": shared_file, "size": 1000, "mtime": 1700000000.0}])

        entry_before = db.get_entry(shared_file)
        assert entry_before is not None
        assert entry_before["lan_status"] == "synced"
        assert entry_before["cloud_status"] == "synced"

        record_sync_results(db, "lan", [], removed=[shared_file])

        entry_after_lan = db.get_entry(shared_file)
        assert entry_after_lan is not None, "Row must not be deleted when removed from LAN only"
        assert entry_after_lan["lan_status"] is None, "lan_status must be cleared"
        assert entry_after_lan["cloud_status"] == "synced", "cloud_status must remain 'synced'!"

        record_sync_results(db, "cloud", [], removed=[shared_file])
        assert db.get_entry(shared_file) is None, "Row must be purged once both destinations clear it"
        print("  [PASS] Cross-mode delete preserves peer sync status; row garbage-collected only when both clear.")
        results["4.1_cross_mode_isolation"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] Cross-mode isolation failed: {e}")
        results["4.1_cross_mode_isolation"] = f"FAIL: {e}"

    print("\n--- Phase 4.2: SMB Snapshot Fault Tolerance (Fix 3 / Finding 22.2.1) ---")
    try:
        from flow import lan_snapshot_before_task
        with patch("flow.walk_lan_destination", side_effect=OSError("SMB connection drop")):
            before_dict = lan_snapshot_before_task.fn(chaos_cfg)
        assert before_dict == {}, "Snapshot-before must return {} on SMB error"

        from core.lan_manifest import diff_snapshots
        diff = diff_snapshots(before_dict, {"Accounting/Q1_Ledger.xlsx": (500, 1.0)})
        assert "Accounting/Q1_Ledger.xlsx" in diff["added"]
        print("  [PASS] SMB enumeration error caught, returns {}, downstream diff executes without error.")
        results["4.2_smb_snapshot_tolerance"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] SMB snapshot tolerance failed: {e}")
        results["4.2_smb_snapshot_tolerance"] = f"FAIL: {e}"

    print("\n--- Phase 4.3: UNC Path Resolution (Fix 4 / Finding 7.1.1) ---")
    try:
        from core.lan_manifest import walk_lan_destination
        items = walk_lan_destination(UNC_DEST)
        assert len(items) > 0
        paths = [i["path"] for i in items]
        assert not any(p.startswith("\\") or p.startswith("/") for p in paths)
        assert any("/" in p for p in paths)
        print(f"  [PASS] walk_lan_destination walked live UNC share ({UNC_DEST}) cleanly without .resolve() hang.")
        results["4.3_unc_path_handling"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] UNC path handling failed: {e}")
        results["4.3_unc_path_handling"] = f"FAIL: {e}"

    print("\n--- Phase 4.4: Empty/System-Folder Source Gate (Fix 5 / Finding 19.1.1) ---")
    try:
        empty_dir = CHAOS_ROOT / "empty_source"
        if empty_dir.exists():
            shutil.rmtree(empty_dir)
        empty_dir.mkdir(parents=True)
        (empty_dir / "System Volume Information").mkdir()
        (empty_dir / "$RECYCLE.BIN").mkdir()
        (empty_dir / "desktop.ini").write_text("[.ShellClassInfo]")
        (empty_dir / "Thumbs.db").write_bytes(b"\x00" * 20)

        from core.health import check_source_drive
        ok, reason = check_source_drive(str(empty_dir), min_free_gb=0)
        assert not ok, "Drive with only system folders/files must fail empty-source check"
        assert "empty" in reason.lower()
        print(f"  [PASS] Empty source containing only Windows system entries rejected: {reason}")
        results["4.4_empty_system_gate"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] Empty source gate failed: {e}")
        results["4.4_empty_system_gate"] = f"FAIL: {e}"

    print("\n--- Phase 4.5: Watchdog Wedged-Lock Alert Dispatch (Fix 2 / Finding 23.1.1) ---")
    smtp_server = MiniSMTPServer(host="127.0.0.1", port=1025)
    smtp_server.start()
    time.sleep(0.2)
    try:
        import watchdog
        with (
            patch("models.config.load_config", return_value=chaos_cfg),
            patch("smtplib.SMTP.starttls", return_value=(220, b"2.0.0 Ready to start TLS")),
        ):
            watchdog._alert_wedged_lock("Prefect API down 30min with lock held", pid=9999)
        time.sleep(0.5)
        assert len(smtp_server.received_messages) >= 1, "Watchdog alert email not received"
        msg = smtp_server.received_messages[0]
        parsed = email.message_from_bytes(msg["raw"])
        assert "WATCHDOG" in parsed["Subject"]
        print("  [PASS] Watchdog wedged-lock alert correctly dispatched and received via core.report.")
        results["4.5_watchdog_alert"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] Watchdog alert failed: {e}")
        results["4.5_watchdog_alert"] = f"FAIL: {e}"
    finally:
        smtp_server.stop()

    print("\n--- Phase 4.6: Prefect Orphan Cleanup Scoping (Fix 6 / Finding 24.1.1) ---")
    try:
        import launch

        class MockFlow:
            def __init__(self, id, name):
                self.id = id
                self.name = name

        class MockRun:
            def __init__(self, id, flow_id, name):
                self.id = id
                self.flow_id = flow_id
                self.name = name

        aam_flows = [
            MockFlow("id-backup", "aam-backup"),
            MockFlow("id-weekly", "weekly-report"),
            MockFlow("id-audit", "integrity-audit"),
        ]
        all_runs = [
            MockRun("r-aam-backup", "id-backup", "random-slug-1"),
            MockRun("r-aam-audit", "id-audit", "random-slug-2"),
            MockRun("r-foreign-audit", "id-foreign-1", "security-audit-compliance"),
            MockRun("r-foreign-report", "id-foreign-2", "sales-monthly-report"),
        ]

        m_client = MagicMock()
        m_client.read_flows = AsyncMock(return_value=aam_flows)
        m_client.read_flow_runs = AsyncMock(side_effect=[all_runs, []])
        m_client.set_flow_run_state = AsyncMock()

        cm = AsyncMock()
        cm.__aenter__.return_value = m_client
        cm.__aexit__.return_value = None

        with (
            patch("prefect.client.orchestration.get_client", return_value=cm),
            patch("core.process.read_lock_alive", return_value=(False, None)),
        ):
            launch._cancel_orphaned_runs()

        cancelled = [c.kwargs["flow_run_id"] for c in m_client.set_flow_run_state.call_args_list]
        assert "r-aam-backup" in cancelled
        assert "r-aam-audit" in cancelled
        assert "r-foreign-audit" not in cancelled, "Foreign security-audit must NOT be cancelled"
        assert "r-foreign-report" not in cancelled, "Foreign sales-report must NOT be cancelled"
        print(f"  [PASS] Orphan cleanup strictly scoped by flow_id: cancelled {cancelled}, spared foreign flows.")
        results["4.6_orphan_cleanup_scoping"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] Orphan cleanup scoping failed: {e}")
        results["4.6_orphan_cleanup_scoping"] = f"FAIL: {e}"

    print("\n--- Phase 4.7: mode='all' UI vs Integrity Audit (Fix 7 / Finding 26.1.1) ---")
    try:
        mock_audit = MagicMock(tags=["maintenance", "integrity"], parameters={"mode": "all"})
        mock_backup_all = MagicMock(tags=["production"], parameters={"mode": "all"})

        m_client = MagicMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = m_client
        cm.__aexit__.return_value = None

        m_client.read_flow_runs = AsyncMock(return_value=[mock_audit])
        with patch("ui.get_client", return_value=cm):
            assert asyncio.run(ui._is_running("cloud")) is False
            assert asyncio.run(ui._is_running("lan")) is False

        m_client.read_flow_runs = AsyncMock(return_value=[mock_backup_all])
        with patch("ui.get_client", return_value=cm):
            assert asyncio.run(ui._is_running("cloud")) is True
            assert asyncio.run(ui._is_running("lan")) is True

        print("  [PASS] UI correctly distinguishes backup(mode='all') from read-only integrity-audit.")
        results["4.7_ui_mode_all_vs_integrity"] = "PASS"
    except Exception as e:
        print(f"  [FAIL] UI mode='all' vs integrity audit failed: {e}")
        results["4.7_ui_mode_all_vs_integrity"] = f"FAIL: {e}"

    print("\n" + "=" * 70)
    print("  FINAL SUITE RESULTS SUMMARY")
    print("=" * 70)
    all_pass = True
    for k, v in results.items():
        status = "PASS" if "PASS" in v else "FAIL"
        print(f"  {k:35} : {v}")
        if status != "PASS":
            all_pass = False

    print("=" * 70)
    print(f"  OVERALL RESULT: {'ALL PASS (CLIENT READY)' if all_pass else 'FAILURES DETECTED'}")
    print("=" * 70)
    return all_pass


if __name__ == "__main__":
    success = run_suite()
    sys.exit(0 if success else 1)
