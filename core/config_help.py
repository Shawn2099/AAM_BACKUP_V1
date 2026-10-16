"""Plain-language names and help for every setting (owner 2026-10-15:
"make it easy to understand for the user too").

The technical key stays visible in small print for the support team; these
texts are what a person at the firm reads. Keep them short, concrete, and
free of tool jargon. A key without an entry falls back to the model's
technical description (a test checks every key has one).
"""

from __future__ import annotations

SECTION_TITLES = {
    "general": "Firm",
    "schedule": "Backup timetable",
    "notifications": "Emails",
    "paths": "Folders",
    "lan": "Backup to the NAS (office network)",
    "wol": "Switching the NAS on and off",
    "cloud": "Backup to Google Cloud",
    "budget": "Maximum running time",
    "dashboard": "Dashboard",
    "alerts": "Alert emails",
    "maintenance": "Housekeeping",
    "health": "Checks before each backup",
    "run_logs": "Detailed logs",
    "guards": "Protection against accidental deletion",
    "approvals": "Approvals",
    "rollover": "New financial year",
    "audit": "Weekly safety check",
    "watchdog": "Self-repair (watchdog)",
}

# key: (label, help)
HELP: dict[str, tuple[str, str]] = {
    "firm_name": ("Firm name", "Shown in emails and reports."),

    # ── timetable ──
    "schedule.lan_cron": ("LAN backup (copy to the NAS)",
                          "When the files are copied to the NAS in the office."),
    "schedule.cloud_cron": ("Cloud backup (copy to Google Cloud)",
                            "When the files are copied to Google Cloud."),
    "schedule.weekly_cron": ("Weekly report email", "When the weekly summary email is sent."),
    "schedule.monthly_cron": ("Monthly report email", "When the monthly summary email is sent."),
    "schedule.audit_cron": ("Weekly safety check",
                            "When the backups are compared file by file with the originals. "
                            "It only reads, it never changes anything."),
    "schedule.rollover_cron": ("New-financial-year check",
                               "Daily check that switches the backups to the new financial year "
                               "on 1 April. On other days it does nothing."),
    "schedule.timezone": ("Time zone", "Time zone of the timetable. India = Asia/Kolkata (IST)."),

    # ── emails ──
    "notifications.recipients": ("Who receives the emails",
                                 "Everyone who should get alerts and reports. One address per box."),
    "notifications.sender": ("Sent from (address)", "The address the emails come from."),
    "notifications.smtp_host": ("Mail server", "Address of the mail server, e.g. smtp.gmail.com."),
    "notifications.smtp_port": ("Mail server port", "Usually 587 (or 465)."),
    "notifications.smtp_username": ("Mail login name", "Name used to log in to the mail server."),
    "notifications.smtp_password": ("Mail login password",
                                    "Password for the mail server. Leave empty to keep the current one."),
    "notifications.smtp_security": ("Mail connection security",
                                    "Leave on 'auto' unless your mail provider says otherwise."),
    "notifications.smtp_auth": ("Mail server needs a login", "Off only for an internal mail relay."),
    "notifications.send_on_failure": ("Email me when something goes wrong",
                                      "Send an alert email when a backup fails or needs a decision."),
    "notifications.weekly_enabled": ("Send the weekly report", ""),
    "notifications.monthly_enabled": ("Send the monthly report", ""),
    "notifications.smtp_timeout_seconds": ("Mail server time-out", "How long to wait for the mail server."),
    "notifications.retry_attempts": ("Mail send attempts", "How often to try again when the mail server is busy."),
    "notifications.retry_base_seconds": ("Wait between mail attempts", "Pause before trying again (grows each time)."),

    # ── folders ──
    "paths.source_drive": ("Folder to back up",
                           "The firm's data folder for the current financial year, e.g. E:\\DATA\\FY26-27."),
    "paths.lan_destination": ("Backup folder on the NAS",
                              "Where the copy is kept on the NAS, e.g. \\\\NAS\\backup\\FY26-27."),
    "paths.gcs_key_path": ("Google Cloud key file", "The key file that lets this server use Google Cloud."),
    "paths.runtime_dir": ("Program data folder", "Where the program keeps its database and logs."),
    "paths.database_path": ("Database file", "Leave empty."),
    "paths.log_directory": ("Log folder", "Leave empty."),

    # ── LAN ──
    "lan.enabled": ("Back up to the NAS", "Copy the files to the NAS in the office."),
    "lan.shutdown_after_backup": ("Switch the NAS off after the backup",
                                  "Saves power: the NAS is switched on for the backup and off again."),
    "lan.shutdown_on_all_retries_exhausted": ("Switch the NAS off even if the backup failed", ""),
    "lan.use_backup_mode": ("Copy files that are locked or restricted",
                            "Lets the backup copy files the account could not open otherwise."),
    "lan.min_free_dest_gb": ("Free space to keep on the NAS", "The backup stops if the NAS would get fuller than this."),
    "lan.retry_count": ("Retries per file", "How often to retry a file that could not be copied."),
    "lan.retry_wait_seconds": ("Wait between file retries", ""),
    "lan.subprocess_timeout_seconds": ("Longest copy run", "The copy is stopped after this long."),
    "lan.max_attempts": ("Attempts per night", "How often the whole NAS backup is tried if it fails."),
    "lan.retry_delay_seconds": ("Wait between attempts", ""),
    "lan.preflight_retries": ("Pre-check retries", "How often the check before copying is repeated if it fails."),
    "lan.preflight_retry_delay_seconds": ("Wait between pre-check retries", ""),
    "lan.mt_threads": ("Files copied at the same time", "More is faster but busier for the NAS."),
    "lan.log_tail_kb": ("Log excerpt size", "How much of the copy log is shown in alerts."),
    "lan.dry_run_timeout_seconds": ("Time for the pre-check", "Before copying, the program checks what would change."),
    "lan.preflight_smb_attempts": ("NAS reachability checks", "How often to check the NAS answers before giving up."),
    "lan.preflight_smb_wait_seconds": ("Wait between NAS checks", ""),
    "lan.privilege_check_timeout_seconds": ("Time for the rights check", ""),

    # ── WoL ──
    "wol.enabled": ("Switch the NAS on automatically", "Wake the NAS over the network before the backup."),
    "wol.mac_address": ("NAS network card address (MAC)", "Looks like AA:BB:CC:DD:EE:FF. Found in the NAS settings."),
    "wol.server_ip": ("NAS address (IP)", "e.g. 192.168.1.100."),
    "wol.broadcast_address": ("Wake-up broadcast address", "Leave empty unless the NAS is on a different network."),
    "wol.wake_timeout_seconds": ("How long the NAS may take to start", "Set to about twice its real start-up time."),
    "wol.ping_interval_seconds": ("Check every", "How often to check whether the NAS is up."),
    "wol.stability_wait_seconds": ("Settle time after start-up", "Extra wait after the NAS answers, before copying."),
    "wol.smb_connect_timeout_seconds": ("Time per NAS check", ""),
    "wol.wake_retry_count": ("Wake-up signals sent", ""),
    "wol.wake_retry_interval_seconds": ("Pause between wake-up signals", ""),
    "wol.shutdown_delay_seconds": ("Delay before the NAS switches off",
                                   "Time in which someone can still cancel the switch-off on the NAS."),
    "wol.shutdown_command_timeout_seconds": ("Time for the switch-off command", ""),
    "wol.shutdown_verify_timeout_seconds": ("Time to confirm the NAS is off",
                                            "If the NAS is still on after this, you get a warning."),
    "wol.shutdown_verify_closed_probes": ("Checks that confirm it is off", ""),

    # ── cloud ──
    "cloud.enabled": ("Back up to Google Cloud", "Copy the files to Google Cloud."),
    "cloud.bandwidth_limit": ("Upload speed limit", "Keeps the office internet usable during the upload."),
    "cloud.bucket": ("Cloud storage name (bucket)", "Set up once by the support team."),
    "cloud.project_number": ("Google project number", "Set up once by the support team."),
    "cloud.location": ("Cloud region", "Where Google keeps the data, e.g. asia-south1 (Mumbai)."),
    "cloud.storage_class": ("Storage type", "STANDARD for daily backups."),
    "cloud.write_probe_enabled": ("Test cloud access before each backup", ""),
    "cloud.retry_count": ("Retries per file", ""),
    "cloud.subprocess_timeout_seconds": ("Longest upload run", "The upload is stopped after this long."),
    "cloud.max_attempts": ("Attempts per night", "How often the upload is tried if it fails."),
    "cloud.retry_delay_seconds": ("Wait between attempts", ""),
    "cloud.preflight_retries": ("Cloud access check retries", "How often the check before uploading is repeated if it fails."),
    "cloud.preflight_retry_delay_seconds": ("Wait between access check retries", ""),
    "cloud.verify_retries": ("After-upload check retries", "How often the check after uploading is repeated if it fails."),
    "cloud.verify_retry_delay_seconds": ("Wait between after-upload check retries", ""),
    "cloud.max_duration_seconds": ("Stop uploading after", "Empty = automatic."),
    "cloud.verify_timeout_seconds": ("Time for the after-upload check", ""),
    "cloud.preflight_timeout_seconds": ("Time for the cloud access check", ""),
    "cloud.probe_retries": ("Network retries in the access check", ""),
    "cloud.probe_retries_sleep_seconds": ("Wait between access check network retries", ""),
    "cloud.dry_run_timeout_seconds": ("Time for the pre-check", ""),
    "cloud.diff_timeout_seconds": ("Time for the difference report", ""),
    "cloud.manifest_timeout_seconds": ("Time for the cloud file list", ""),
    "cloud.cloud_size_timeout_seconds": ("Time for the cloud size check", ""),
    "cloud.transfers": ("Files uploaded at the same time", ""),
    "cloud.checkers": ("Files compared at the same time", ""),
    "cloud.modify_window_seconds": ("Time difference treated as equal",
                                    "File times closer than this count as the same."),
    "cloud.retries_sleep_seconds": ("Wait between network retries in the difference report", ""),
    "cloud.buffer_size": ("Memory per upload", ""),
    "cloud.error_tail_lines": ("Error lines in alerts", ""),

    # ── budgets ──
    "budget.lan_max_hours": ("NAS backup may run for at most",
                             "After this it stops and continues the next night. The timetable must leave this much time."),
    "budget.cloud_max_hours": ("Cloud backup may run for at most",
                               "After this it stops and continues the next night (a first full upload takes several nights)."),
    "budget.audit_max_hours": ("Weekly safety check may run for at most", ""),
    "budget.cloud_verify_reserve_minutes": ("Time kept for the after-upload check", ""),
    "budget.lan_post_reserve_minutes": ("Time kept for switching the NAS off", ""),

    # ── dashboard ──
    "dashboard.public_url": ("Dashboard address in emails",
                             "The link people click in alert emails, e.g. http://backup-server:8080."),
    "dashboard.stale_after_hours": ("Warn when the last good backup is older than", ""),
    "dashboard.alert_failure_window_days": ("Warn about failed emails from the last", ""),
    "dashboard.auth_enabled": ("Dashboard needs a login", "Changed only on the server."),
    "dashboard.api_key": ("Dashboard password", "Changed only on the server."),
    "dashboard.bind_address": ("Who can open the dashboard", "Changed only on the server."),
    "dashboard.port": ("Dashboard port", "Changed only on the server."),
    "dashboard.admin_session_minutes": ("Advanced login locks after", ""),
    "dashboard.admin_password_min_length": ("Shortest admin password", ""),

    # ── alerts / housekeeping / health ──
    "alerts.repeat_cooldown_hours": ("Repeat the same alert at most every",
                                     "Stops your inbox filling with the same alert."),
    "alerts.body_max_chars": ("Alert text length", "Longer details are attached as a file."),
    "alerts.attachment_max_kb": ("Largest alert attachment", ""),
    "maintenance.concurrency_wait_seconds": ("How long a backup waits for the previous one", ""),
    "maintenance.sqlite_synchronous": ("Database safety level", ""),
    "maintenance.db_retention_days": ("Keep backup history for", ""),
    "maintenance.log_retention_days": ("Keep program logs for", ""),
    "maintenance.sqlite_busy_timeout_ms": ("Database wait time", ""),
    "maintenance.sqlite_vacuum_freelist_threshold": ("Database clean-up threshold", ""),
    "maintenance.config_backups_keep": ("Settings backups to keep", "A copy of the settings is saved before every change."),
    "maintenance.abandon_running_after_hours": (
        "Mark a stuck backup as abandoned after",
        "A backup whose process has stopped is marked abandoned (with an email) at once; this "
        "limit only applies when that cannot be checked."),
    "health.max_clock_skew_seconds": ("Allowed clock difference", "Google refuses uploads if the server clock is far off."),
    "health.clock_check_timeout_seconds": ("Time for the clock check", ""),
    "health.clock_unverified_policy": ("If the clock cannot be checked",
                                       "'warn' = back up anyway and show a warning; 'block' = stop the cloud backup."),
    "health.ntp_server": ("Time server", ""),
    "health.w32tm_timeout_seconds": ("Time for the time-server check", ""),
    "health.min_free_source_gb": ("Free space needed on the data drive", ""),
    "health.rollover_auth_timeout_seconds": ("Time for cloud sign-in at year end", ""),
    "health.rollover_archive_timeout_seconds": ("Time to archive last year's cloud data", ""),
    "run_logs.dir": ("Detailed log folder", "Leave empty."),
    "run_logs.retention_days": ("Keep detailed logs for", ""),
    "run_logs.compress_after_kb": ("Compress logs larger than", ""),
    "run_logs.min_free_gb": ("Warn when free space for logs is below", ""),
    "run_logs.service_log_retention_days": ("Keep service logs for", ""),

    # ── safety ──
    "guards.max_delete_files": ("Stop if a backup would delete more than",
                                "Above this the backup waits for your approval on the dashboard."),
    "guards.max_delete_percent": ("...or more than this share of all files", ""),
    "guards.max_source_shrink_percent": ("Stop if the data folder suddenly shrinks by more than",
                                         "Protects against an empty or wrong drive being mirrored."),
    "guards.forbid_local_destination": ("Never back up onto this same server", ""),
    "approvals.expiry_hours": ("An approval is valid for", ""),
    "approvals.tolerance_percent": ("Allowed change after approval", ""),
    "rollover.source_placeholder_name": ("Note file placed in a new year folder", ""),
    "audit.checkers": ("Files checked at the same time", ""),
    "audit.retries": ("Retries", ""),
    "audit.retries_sleep_seconds": ("Wait between retries", ""),
    "audit.modify_window_seconds": ("Time difference treated as equal",
                                    "File times closer than this count as the same."),
    "audit.detail_max_paths": ("File names listed in a result", ""),
    "audit.network_error_patterns": ("Extra 'could not run' messages", "For the support team."),

    # ── watchdog ──
    "watchdog.prefect_health_url": ("Scheduler health address", ""),
    "watchdog.check_interval_seconds": ("Check the scheduler every", ""),
    "watchdog.failure_threshold": ("Restart after failed checks in a row", ""),
    "watchdog.request_timeout_seconds": ("Time per check", ""),
    "watchdog.backup_wait_interval_seconds": ("Re-check during a backup every", ""),
    "watchdog.max_deferrals": ("Waits before alerting (no copy running)", ""),
    "watchdog.max_transfer_deferrals": ("Waits before alerting (copy running)", ""),
    "watchdog.restart_cooldown_seconds": ("Pause after a restart", ""),
    "watchdog.max_starts_per_window": ("Automatic starts allowed per hour", ""),
    "watchdog.start_window_seconds": ("Counting window for starts", ""),
    "watchdog.stop_pending_timeout_seconds": ("Force-stop a stuck service after", ""),
    "watchdog.kill_wait_seconds": ("Wait after force-stop", ""),
    "watchdog.api_ready_timeout_seconds": ("Time for the scheduler to start", ""),
    "watchdog.service_poll_seconds": ("Service check interval", ""),
    "watchdog.sc_timeout_seconds": ("Time per service command", ""),
    "watchdog.log_rotation_mb": ("Start a new watchdog log at", ""),
    "watchdog.stale_check_interval_seconds": (
        "Check for overdue backups every",
        "An email is sent when a backup has not succeeded for longer than the "
        "'last good backup' limit."),
}


def label_and_help(key: str, technical: str) -> tuple[str, str]:
    """(plain label, plain help) - falls back to the technical description."""
    if key in HELP:
        label, help_text = HELP[key]
        return label, help_text
    return key, technical
