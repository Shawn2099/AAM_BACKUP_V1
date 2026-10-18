"""Pydantic v2 configuration models for AAM Backup Automation V1.

Validated on load. No dead sections. Only what's actually used.
"""

import ipaddress
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PROJECT_DIR = Path(__file__).resolve().parent.parent

# Absolute config path: AAM_CONFIG env (set by the service installer) or
# <project>/config.yaml. Never CWD-relative — a service or tool started from
# another working directory must still load the same file.
CONFIG_PATH = str(
    Path(os.environ["AAM_CONFIG"]).resolve()
    if os.environ.get("AAM_CONFIG")
    else _PROJECT_DIR / "config.yaml"
)

# Keys that were valid in earlier releases and are now ignored on purpose.
# They are dropped with a WARNING instead of failing the strict
# (extra="forbid") validation, so an older client config still loads.
# Anything not listed here is rejected loudly.
# name@domain.tld, no spaces - deliberately simple (no new dependency); the
# SMTP server is the final judge, this catches typos before they cost an alert.
_EMAIL = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}")

DEPRECATED_KEYS: dict[str, frozenset[str]] = {
    # F12: no code path ever read it; success emails were removed.
    "notifications": frozenset({"send_on_success"}),
}


class StrictModel(BaseModel):
    """Base for every config section: unknown keys are a validation error.

    A typo (e.g. `shutdown_after_bakup`) must fail at load with the key
    name in the message — silently falling back to the default is how a
    safety switch gets flipped without anyone noticing.
    """

    model_config = ConfigDict(extra="forbid")


def model_default(model: type[BaseModel], field: str):
    """The default of `model.field`, for a function's fallback parameter.

    Owner rule: a tunable's default lives only in this module. Functions that
    keep a fallback for direct callers (tests, tools) read it from here
    instead of repeating a literal that can drift (it did: ManifestDB's
    vacuum threshold said 1000 while the model said 10000).
    """
    return model.model_fields[field].get_default(call_default_factory=True)


_DEFAULT_RUNTIME_DIR = os.environ.get(
    "AAM_RUNTIME_DIR",
    r"C:\BackupAgent",
)


class PathsConfig(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source_drive: str = Field(..., description="Source drive root path, e.g. D:\\")
    lan_destination: str = Field(..., description="LAN UNC path, e.g. \\\\192.168.10.10\\share$")
    runtime_dir: str = Field(
        default=_DEFAULT_RUNTIME_DIR,
        description="Root directory for logs, database, lock file, and Prefect home. "
                    "All runtime data lives here, separate from the code installation.",
    )
    database_path: str = Field(default="", description="Path to SQLite manifest database (auto-derived from runtime_dir if empty)")
    log_directory: str = Field(default="", description="Log directory (auto-derived from runtime_dir if empty)")
    gcs_key_path: str = Field(default="", description="Path to GCS service account JSON key file (required when cloud is enabled; leave empty for LAN-only)")

    @model_validator(mode="after")
    def derive_runtime_paths(self) -> "PathsConfig":
        rt = Path(self.runtime_dir)
        if not self.log_directory:
            self.log_directory = str(rt / "logs")
        if not self.database_path:
            self.database_path = str(rt / "manifest.db")
        if not self.database_path.endswith(".db"):
            raise ValueError(f"database_path must end with .db: {self.database_path}")
        return self

    @property
    def backup_lock_path(self) -> Path:
        """Derive backup.lock path from the database_path parent directory."""
        return Path(self.database_path).parent / "backup.lock"

    @property
    def prefect_home(self) -> Path:
        """Prefect home directory inside runtime_dir."""
        return Path(self.runtime_dir) / ".prefect"

    @field_validator("source_drive")
    @classmethod
    def source_drive_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source_drive must not be empty")
        return v.strip()

    @field_validator("lan_destination")
    @classmethod
    def is_unc_path(cls, v: str) -> str:
        if not re.match(r"^\\\\.+\\", v):
            raise ValueError(f"LAN destination must be a UNC path (\\\\server\\share): {v}")
        return v

    @field_validator("gcs_key_path")
    @classmethod
    def gcs_key_exists(cls, v: str) -> str:
        # LAN-only deployments leave this empty; cloud-enabled enforcement
        # lives in AppConfig.cross_field_validation (fail-closed there).
        return v


class LanConfig(StrictModel):
    enabled: bool = True
    retry_count: int = Field(default=3, ge=1, le=10)
    retry_wait_seconds: int = Field(default=10, ge=1, le=300)
    subprocess_timeout_seconds: int = Field(default=14400, ge=3600)
    shutdown_after_backup: bool = True
    shutdown_on_all_retries_exhausted: bool = Field(
        default=False,
        description=(
            "If true, initiates NAS shutdown even if the LAN backup ended in LAN_PARTIAL "
            "or LAN_FAILED after all retry attempts are exhausted. Failure alerts are always "
            "dispatched before shutdown is initiated."
        ),
    )
    max_attempts: int = Field(default=2, ge=1, le=10, description="Flow-level retry attempts for LAN backup orchestration")
    retry_delay_seconds: int = Field(default=600, ge=60, le=3600, description="Delay between flow-level retry attempts")
    preflight_retries: int = Field(
        default=1, ge=0, le=5,
        description="Extra tries of the LAN preflight (robocopy /L dry-run) after a failure")
    preflight_retry_delay_seconds: int = Field(
        default=30, ge=5, le=3600, description="Seconds between LAN preflight tries")
    mt_threads: int = Field(default=4, ge=1, le=128, description="Robocopy /MT multi-threaded copy count")
    log_tail_kb: int = Field(default=100, ge=4, le=10240, description="Size of the robocopy log tail (KB) used for the result decision and carried into alerts/DB; the full log is always kept in run_logs")
    # F8: the preflight /L walk scales with dataset size (1M files ~= 5-10 min
    # over SMB; 2.5M ~= 12-25 min). The old hardcoded 300s failed at exactly the
    # scale this deployment targets, aborting a healthy backup before it started.
    dry_run_timeout_seconds: int = Field(default=900, ge=60, le=7200, description="Timeout for the robocopy /L preflight dry-run (seconds)")
    preflight_smb_attempts: int = Field(
        default=8, ge=1, le=60,
        description="LAN preflight: SMB port-445 reachability attempts before failing "
                    "(each up to wol.smb_connect_timeout_seconds)")
    preflight_smb_wait_seconds: int = Field(
        default=15, ge=1, le=300, description="LAN preflight: seconds between SMB attempts")
    use_backup_mode: bool = Field(
        default=True,
        description="robocopy /ZB (backup mode) so files the account cannot open normally "
                    "are still copied (owner DEC-8). Needs SeBackupPrivilege on the service "
                    "account; the preflight checks it. false = no /ZB.")
    min_free_dest_gb: float = Field(
        default=5, ge=0, le=100_000,
        description="Preflight: the NAS must have the bytes this backup will copy plus this "
                    "many GB free.")
    privilege_check_timeout_seconds: int = Field(
        default=60, ge=5, le=600,
        description="Timeout for the `whoami /priv` SeBackupPrivilege check (seconds)")


class WolConfig(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    enabled: bool = True
    # F11: empty default + conditional validation below. The MAC is only
    # meaningful when WoL is enabled; requiring it for disabled pipelines made
    # lan-only / cloud-only deployments carry a fake MAC.
    mac_address: str = ""
    server_ip: str = "192.168.10.10"
    broadcast_address: str = Field(
        default="",
        description=(
            "WoL magic packet broadcast target. Leave empty to auto-derive from server_ip "
            "(e.g. 192.168.10.10 → 192.168.10.255). Set explicitly if the NAS is on a "
            "different VLAN or managed switch that blocks 255.255.255.255."
        ),
    )
    # WP10 (owner issue 2026-10-14): a NAS cold boot (POST + disk spin-up +
    # OS + SMB service) routinely takes minutes. Generous defaults; set to
    # 2x the cold-boot time measured on site (plan WP17).
    wake_timeout_seconds: int = Field(
        default=900, ge=60, le=3600,
        description="Max seconds to wait for the NAS to become usable over SMB after WoL")
    ping_interval_seconds: int = Field(
        default=15, ge=5, le=120, description="Seconds between readiness polls")
    stability_wait_seconds: int = Field(
        default=90, ge=0, le=900,
        description="After SMB first answers, wait this long and re-check before using the NAS")
    smb_connect_timeout_seconds: float = Field(
        default=15, ge=1, le=120,
        description="TCP connect timeout for each SMB (port 445) readiness probe")
    # G3: a single unicast-ish broadcast packet is frequently dropped
    # (managed switches, NIC firmware, half-initiated NIC wake state).
    # Standard practice (cf. etherwake -s/-I): send several rounds.
    # 3 rounds × 5 s adds only ~10 s against a 300 s wake timeout.
    wake_retry_count: int = Field(
        default=5, ge=1, le=20, description="Magic-packet rounds sent per wake attempt")
    wake_retry_interval_seconds: int = Field(
        default=10, ge=1, le=120, description="Seconds between magic-packet rounds")
    # WP10: the shutdown result is verified, not assumed.
    shutdown_delay_seconds: int = Field(
        default=300, ge=0, le=3600,
        description="shutdown.exe /t delay; staff can cancel with `shutdown /a` on the NAS meanwhile")
    shutdown_command_timeout_seconds: int = Field(
        default=120, ge=5, le=600, description="Max seconds for shutdown.exe itself to return")
    shutdown_verify_timeout_seconds: int = Field(
        default=600, ge=60, le=7200,
        description=("Seconds (from the command) within which the NAS must stop answering on "
                     "port 445; must be longer than shutdown_delay_seconds. The backup slot is "
                     "held meanwhile, so a queued job never starts against a NAS about to power off"))
    shutdown_verify_closed_probes: int = Field(
        default=2, ge=1, le=10,
        description="Consecutive closed-port probes (ping_interval_seconds apart) that count as 'off'")

    @field_validator("mac_address")
    @classmethod
    def valid_mac(cls, v: str) -> str:
        # F11: an empty MAC is allowed here; enabled-ness is checked in the
        # model validator below (field validators can't see sibling fields).
        if v and not re.match(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$", v):
            raise ValueError(f"Invalid MAC address format: {v}")
        return v

    @model_validator(mode="after")
    def _mac_required_when_enabled(self) -> "WolConfig":
        if self.enabled and not self.mac_address:
            raise ValueError("wol.mac_address is required when wol.enabled is true")
        return self

    @model_validator(mode="after")
    def _verify_window_outlasts_delay(self) -> "WolConfig":
        if self.shutdown_verify_timeout_seconds <= self.shutdown_delay_seconds:
            raise ValueError(
                f"wol.shutdown_verify_timeout_seconds ({self.shutdown_verify_timeout_seconds}) "
                f"must be longer than wol.shutdown_delay_seconds ({self.shutdown_delay_seconds}): "
                "the NAS only powers off after the delay")
        return self

    @field_validator("server_ip")
    @classmethod
    def valid_ipv4(cls, v: str) -> str:
        try:
            ipaddress.IPv4Address(v)
        except ipaddress.AddressValueError:
            raise ValueError(f"Invalid IPv4 address: {v}")
        return v

    @field_validator("broadcast_address")
    @classmethod
    def valid_broadcast_address(cls, v: str) -> str:
        """Validate broadcast_address if provided. Empty string means auto-derive."""
        if v and v != "":
            try:
                ipaddress.IPv4Address(v)
            except ipaddress.AddressValueError:
                raise ValueError(
                    f"Invalid broadcast_address IPv4 '{v}'. "
                    "Set to empty string to auto-derive from server_ip."
                )
        return v

    def get_broadcast_address(self) -> str:
        """Return effective WoL broadcast address.

        If broadcast_address is explicitly set in config, use it.
        Otherwise, auto-derive the /24 subnet broadcast from server_ip:
          192.168.10.100  →  192.168.10.255

        This covers the common case (same /24 as source PC) without any
        manual config. Users on a different VLAN must set broadcast_address
        explicitly to their NAS subnet's broadcast address.
        """
        if self.broadcast_address:
            return self.broadcast_address
        # Derive /24 subnet broadcast: replace last octet with 255
        parts = self.server_ip.rsplit(".", 1)
        return f"{parts[0]}.255"


class CloudConfig(StrictModel):
    enabled: bool = True
    # C-A: in-rclone duration cap. None = auto (subprocess timeout minus a
    # 300s margin); 0 disables the flag entirely; explicit value wins.
    max_duration_seconds: int | None = Field(
        default=None,
        ge=0,
        description=(
            "rclone --max-duration cap so transfers self-terminate gracefully "
            "(cutoff-mode SOFT) instead of being hard-killed at the subprocess "
            "timeout. Auto-derived when omitted."
        ),
    )
    # F11: no real-looking default. An empty bucket is only valid when cloud
    # is disabled; an enabled cloud backup must name its bucket explicitly.
    bucket: str = ""
    project_number: str = ""
    location: str = "asia-south1"
    storage_class: str = "STANDARD"
    bandwidth_limit: str = "10M"
    retry_count: int = Field(default=3, ge=1, le=10)
    subprocess_timeout_seconds: int = Field(default=18000, ge=3600)
    max_attempts: int = Field(default=3, ge=1, le=10, description="Flow-level retry attempts for cloud backup orchestration")
    retry_delay_seconds: int = Field(default=300, ge=60, le=3600, description="Delay between flow-level retry attempts")
    preflight_retries: int = Field(
        default=1, ge=0, le=5,
        description="Extra tries of the cloud preflight (source + GCS access probe) after a failure")
    preflight_retry_delay_seconds: int = Field(
        default=30, ge=5, le=3600, description="Seconds between cloud preflight tries")
    verify_retries: int = Field(
        default=1, ge=0, le=5,
        description="Extra tries of the after-upload check and report step after a failure")
    verify_retry_delay_seconds: int = Field(
        default=60, ge=5, le=3600, description="Seconds between after-upload check tries")
    verify_timeout_seconds: int = Field(default=18000, ge=60, le=86400, description="Timeout for post-sync rclone check verify step (seconds). Conservative 5h initial limit; tune from month-one telemetry.")
    preflight_timeout_seconds: int = Field(default=300, ge=30, le=3600, description="Timeout for pre-sync rclone check dry-run (seconds)")
    probe_retries: int = Field(
        default=2, ge=0, le=10,
        description="rclone --retries for the preflight GCS access probe (lsjson --max-depth 0)")
    probe_retries_sleep_seconds: int = Field(
        default=5, ge=0, le=300,
        description="rclone --retries-sleep for the preflight GCS access probe (seconds)")
    dry_run_timeout_seconds: int = Field(
        default=1800, ge=60, le=86400,
        description="Timeout (seconds) for each guard measurement before the cloud sync: "
                    "`rclone sync --dry-run` (planned deletions) and `rclone size` of the "
                    "source. Default ~4x the measured 3-8 min diff time at 1M files; "
                    "tune from run_logs telemetry.")
    # F7: listing timeouts sized for the target scale (200GB / ~1M files, growing
    # to ~2.5M by yr-5). Measured order-of-magnitude: rclone size over GCS takes
    # ~40-150s at 1M objects, the lsjson manifest ~2-5 min, the diff ~3-8 min —
    # the old defaults (30/300/600) were tuned for a demo-sized bucket and
    # would time out (and misreport "0 files") at the real scale.
    diff_timeout_seconds: int = Field(default=18000, ge=30, le=86400, description="Timeout for rclone check --combined diff report (seconds). Conservative 5h initial limit; tune from month-one telemetry.")
    manifest_timeout_seconds: int = Field(default=18000, ge=30, le=86400, description="Timeout for rclone lsjson manifest listing (seconds). Conservative 5h initial limit; tune from month-one telemetry.")
    cloud_size_timeout_seconds: int = Field(default=300, ge=10, le=3600, description="Timeout for rclone size GCS object count query (seconds)")
    transfers: int = Field(default=2, ge=1, le=64, description="rclone --transfers concurrent file transfers")
    checkers: int = Field(default=4, ge=1, le=64, description="rclone --checkers concurrent file checkers")
    modify_window_seconds: int = Field(
        default=2, ge=0, le=10,
        description="(max 10: a wide window makes the upload skip same-size edits) rclone --modify-window for the upload, the after-upload check and the "
                    "difference report (seconds); 2 = NTFS modification-time granularity")
    sync_retries_sleep_seconds: int = Field(
        default=30, ge=1, le=600,
        description="rclone --retries-sleep for the upload and its dry-run (seconds); set "
                    "explicitly so a retry cannot reset the --max-duration deadline")
    stats_interval_seconds: int = Field(
        default=60, ge=1, le=3600,
        description="rclone --stats: progress lines in the upload log every N seconds "
                    "(the final totals line is written regardless)")
    retries_sleep_seconds: int = Field(
        default=10, ge=0, le=600,
        description="rclone --retries-sleep for the difference report (seconds); its "
                    "--retries is cloud.retry_count")
    buffer_size: str = Field(default="64M", description="rclone --buffer-size per transfer slot. 2 transfers × 64M = 128M total.")
    error_tail_lines: int = Field(default=100, ge=1, le=10000, description="Max rclone error lines carried into alerts/DB/dashboard; the full log is always kept in run_logs")
    write_probe_enabled: bool = Field(
        default=True,
        description="Preflight: create and delete a tiny test object under "
                    "<bucket>/_aam_preflight/ (outside every FY folder) to prove write and "
                    "delete rights before syncing (owner DEC-5). Uses "
                    "preflight_timeout_seconds.")

    @field_validator("bucket")
    @classmethod
    def valid_bucket(cls, v: str) -> str:
        # F11: empty is legal here; the enabled-ness check lives in the model
        # validator (a field validator cannot see `enabled`).
        if v and not re.match(r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$", v):
            raise ValueError(f"Invalid bucket name: {v}")
        return v

    @model_validator(mode="after")
    def _bucket_required_when_enabled(self) -> "CloudConfig":
        if self.enabled and not re.match(r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$", self.bucket):
            raise ValueError(
                "cloud.bucket is required when cloud.enabled is true — "
                f"got {self.bucket!r}. Set it to the real GCS bucket name."
            )
        if self.enabled and not self.project_number.strip():
            raise ValueError(
                "cloud.project_number is required when cloud.enabled is true — "
                "got empty. Set it to the real GCP project number; "
                "LAN-only deployments should set cloud.enabled=false instead."
            )
        return self

    @field_validator("storage_class")
    @classmethod
    def valid_storage_class(cls, v: str) -> str:
        valid = {"STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"}
        if v.upper() not in valid:
            raise ValueError(f"Invalid storage_class '{v}'. Must be one of: {sorted(valid)}")
        return v.upper()

    @field_validator("bandwidth_limit")
    @classmethod
    def valid_bandwidth(cls, v: str) -> str:
        if not re.match(r"^\d+[kMG]$", v):
            raise ValueError(f"Invalid bandwidth_limit '{v}'. Format: 10M, 500k, 1G")
        return v


class NotificationConfig(StrictModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    sender: str = ""
    recipients: list[str] = Field(default_factory=list)
    # F12: the old `send_on_success` flag was removed — no code path ever
    # read it (success emails would have been 5 a.m. noise), and a config
    # switch that does nothing is worse than none. Pydantic ignores unknown
    # keys, so existing configs that still list it load fine.
    send_on_failure: bool = True
    weekly_enabled: bool = True
    monthly_enabled: bool = True
    smtp_security: str = Field(
        default="auto", pattern="^(auto|starttls|ssl|none)$",
        description="auto = SSL on port 465, STARTTLS otherwise; none = plaintext "
                    "(internal relay only)",
    )
    smtp_auth: bool = Field(
        default=True,
        description="Log in with smtp_username/smtp_password. false = unauthenticated "
                    "internal relay. When true, missing credentials are a recorded "
                    "delivery FAILURE, never a silent skip.",
    )
    smtp_timeout_seconds: float = Field(default=30, ge=1, le=600)
    retry_attempts: int = Field(default=3, ge=1, le=10,
                                description="SMTP send attempts for transient errors")
    retry_base_seconds: float = Field(default=10, ge=0, le=600,
                                      description="Base of the exponential retry backoff")

    @field_validator("smtp_port")
    @classmethod
    def valid_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError(f"Invalid SMTP port: {v}")
        return v

    # DEC-9 follow-up: an address typo meant alerts silently went nowhere.
    @field_validator("sender")
    @classmethod
    def valid_sender(cls, v: str) -> str:
        if v and not _EMAIL.fullmatch(v):
            raise ValueError(f"'{v}' is not an email address (expected name@domain.tld)")
        return v

    @field_validator("recipients")
    @classmethod
    def valid_recipients(cls, v: list[str]) -> list[str]:
        cleaned = [r.strip() for r in v]
        bad = [r for r in cleaned if not _EMAIL.fullmatch(r)]
        if bad:
            raise ValueError("not an email address (expected name@domain.tld): "
                             + ", ".join(repr(b) for b in bad))
        dupes = sorted({r for r in cleaned if cleaned.count(r) > 1})
        if dupes:
            raise ValueError(f"listed more than once: {', '.join(dupes)}")
        return cleaned

    def __repr__(self) -> str:
        return (
            f"NotificationConfig(smtp_host='{self.smtp_host}', smtp_port={self.smtp_port}, "
            f"smtp_username='{self.smtp_username}', smtp_password='***', "
            f"sender='{self.sender}', recipients={self.recipients}, "
            f"send_on_failure={self.send_on_failure})"
        )

    def __str__(self) -> str:
        return self.__repr__()


class AlertsConfig(StrictModel):
    """Alert behaviour (plan §5.5)."""

    repeat_cooldown_hours: float = Field(
        default=12, ge=0, le=720,
        description="The same alert (same key) is re-sent at most once per this many "
                    "hours while unresolved; repeats inside the window are recorded "
                    "as SUPPRESSED. 0 = never suppress.",
    )
    body_max_chars: int = Field(default=1000, ge=100, le=100_000,
                                description="Inline message length in the email body")
    attachment_max_kb: int = Field(default=2048, ge=1, le=20_480,
                                   description="Max size of the full-detail attachment")


class MaintenanceConfig(StrictModel):
    """Operational housekeeping settings."""
    concurrency_wait_seconds: int = Field(
        default=3600,
        ge=1,
        le=86400,
        description=(
            "P2-CONC: max seconds a pipeline waits for the global backup "
            "concurrency slot before giving up (production default 1h; "
            "tests pass small values)"
        ),
    )
    sqlite_synchronous: str = Field(
        default="normal",
        pattern="^(normal|full)$",
        description=(
            "R4: SQLite WAL synchronous level. normal = no corruption risk, "
            "possible loss of the last commit(s) on power cut; full = per-"
            "commit WAL fsync for maximum durability. Operators backing up "
            "critical telemetry may choose full."
        ),
    )
    db_retention_days: int = Field(
        default=90,
        ge=7,
        le=3650,
        description="Days of run history to keep in ManifestDB (7–3650)",
    )
    log_retention_days: int = Field(
        default=90,
        ge=1,
        le=365,
        description="Days of log files to retain before Loguru auto-deletion (1–365)",
    )
    orphan_robocopy_log_max_age_hours: int = Field(
        default=24,
        ge=1,
        le=720,
        description="G8: robocopy temp logs left in %TEMP% by a killed run are deleted once "
                    "older than this (hours); the age gate protects a running backup's log",
    )
    sqlite_busy_timeout_ms: int = Field(
        default=30000,
        ge=1000,
        le=120000,
        description="SQLite PRAGMA busy_timeout in milliseconds — how long to wait on a locked DB (1000–120000)",
    )
    sqlite_vacuum_freelist_threshold: int = Field(
        default=10000,
        ge=100,
        le=50000,
        description="VACUUM triggers when SQLite freelist page count exceeds this value (~40 MB at default). Lower = more frequent VACUUM.",
    )
    config_backups_keep: int = Field(
        default=50, ge=5, le=1000,
        description="config.yaml.bak-* copies kept next to config.yaml (one per settings save "
                    "or FY rollover); older ones are deleted after a successful settings save")
    abandon_running_after_hours: float = Field(
        default=24, ge=1, le=168,
        description="A run whose process is gone is recorded ABANDONED (with a failure email) "
                    "at the next agent or backup start; only when the process cannot be read "
                    "does a run still RUNNING after this many hours count as ABANDONED.",
    )


class RunLogsConfig(StrictModel):
    """Full per-run tool logs (plan WP2): kept complete, compressed, purged."""

    dir: str = Field(
        default="",
        description="Root for per-run artefacts (full rclone/robocopy logs, check "
                    "outputs). Empty = <runtime_dir>/run_logs.",
    )
    retention_days: int = Field(
        default=90, ge=7, le=3650,
        description="Delete run artefact folders older than this many days.",
    )
    compress_after_kb: int = Field(
        default=1024, ge=0, le=10_485_760,
        description="gzip artefact files larger than this after the run (0 = compress all).",
    )
    min_free_gb: float = Field(
        default=5.0, ge=0, le=10_000,
        description="Warn (and mark the run artefacts degraded) when the artefact "
                    "volume has less free space than this.",
    )
    service_log_retention_days: int = Field(
        default=90, ge=7, le=3650,
        description="Delete NSSM-rotated service logs (agent_svc-*, prefect_svc-*, "
                    "watchdog_svc-*) older than this many days.",
    )


class GuardsConfig(StrictModel):
    """Data-safety guards (plan WP3, §6.3; defaults = owner DEC-2).

    A guard that trips stops the run BEFORE any transfer as *_BLOCKED and
    asks a human (dashboard approval). G2/G4 compare with the last
    successful run of the same leg and the same FY.
    """

    max_delete_files: int = Field(
        default=500, ge=0, le=100_000_000,
        description="G3: block when a backup would delete more than this many "
                    "destination files. Also the cloud --max-delete backstop.",
    )
    max_delete_percent: float = Field(
        default=10, ge=0, le=100,
        description="G3: block when a backup would delete more than this percent "
                    "of the destination files.",
    )
    max_source_shrink_percent: float = Field(
        default=20, ge=0, le=100,
        description="G2: block when the source has this percent fewer files than "
                    "at the last successful backup of the same FY.",
    )
    forbid_local_destination: bool = Field(
        default=True,
        description="G4: block a LAN destination that resolves to this machine "
                    "(a backup on the same machine does not protect against its loss).",
    )


class ApprovalsConfig(StrictModel):
    """Human approval of a BLOCKED run (plan §6.2, owner DEC-3)."""

    expiry_hours: float = Field(
        default=12, ge=1, le=168,
        description="An approval given on the dashboard is valid for this many hours "
                    "and for ONE run only.",
    )
    tolerance_percent: float = Field(
        default=0, ge=0, le=100,
        description="How much worse the re-measured numbers may be than the approved "
                    "ones (0 = must not be worse at all).",
    )


class AuditConfig(StrictModel):
    """Weekly read-only integrity audit (plan WP7)."""

    checkers: int = Field(default=1, ge=1, le=16,
                          description="rclone check --checkers (1 = gentle on the HDD)")
    retries: int = Field(default=3, ge=0, le=10, description="rclone check --retries")
    retries_sleep_seconds: int = Field(default=10, ge=0, le=600,
                                       description="rclone check --retries-sleep (seconds)")
    modify_window_seconds: int = Field(
        default=2, ge=0, le=10,
        description="rclone check --modify-window (seconds); 2 = NTFS modification-time "
                    "granularity")
    detail_max_paths: int = Field(default=100, ge=1, le=10_000,
                                  description="Max divergent paths listed in the audit detail "
                                              "(counts are always complete)")
    network_error_patterns: list[str] = Field(
        default_factory=list,
        description="Extra regular expressions (case-insensitive) that mark an audit as "
                    "'could not run' (AUDIT_INCOMPLETE) instead of a content difference - "
                    "added to the built-in TLS/DNS/auth/network/listing signatures.",
    )

    @field_validator("network_error_patterns")
    @classmethod
    def valid_regexes(cls, v: list[str]) -> list[str]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"audit.network_error_patterns: invalid regex {pattern!r}: {e}")
        return v


class RolloverConfig(StrictModel):
    """FY rollover (plan WP11, owner DEC-4)."""

    source_placeholder_name: str = Field(
        default="README_FY.txt", min_length=1, max_length=100,
        description="Small text file written into the NEW source FY folder at rollover "
                    "(never overwritten), so the first backups of the new FY have a real "
                    "file and guard G1 does not block an intentionally new, empty FY.",
    )

    @field_validator("source_placeholder_name")
    @classmethod
    def plain_file_name(cls, v: str) -> str:
        if any(c in v for c in "\\/:") or v.strip(".") == "":
            raise ValueError(f"rollover.source_placeholder_name must be a plain file name, got {v!r}")
        return v


class HealthConfig(StrictModel):
    """Pre-backup health check tuning."""
    max_clock_skew_seconds: int = Field(
        default=600,
        ge=60,
        le=3600,
        description="Maximum acceptable clock skew vs Google time for GCS JWT auth (60–3600). GCS rejects tokens if skew >600s.",
    )
    clock_check_timeout_seconds: int = Field(
        default=10,
        ge=5,
        le=60,
        description="Timeout for the HTTPS connection to googleapis.com during clock skew check (seconds)",
    )
    # WP12 / DEC-6: an unverifiable clock is reported, never silently OK.
    clock_unverified_policy: str = Field(
        default="warn", pattern="^(warn|block)$",
        description=("When neither Google nor ntp_server can tell the time: 'warn' = the cloud "
                     "backup runs and the run row + dashboard show 'clock unverified'; "
                     "'block' = the cloud backup fails with the reason"),
    )
    ntp_server: str = Field(
        default="time.windows.com", min_length=1,
        description="NTP server for the w32tm fallback when Google's Date header cannot be read")
    w32tm_timeout_seconds: int = Field(
        default=30, ge=5, le=120, description="Max seconds for one `w32tm /stripchart` sample")
    min_free_source_gb: int = Field(
        default=1,
        ge=1,
        le=500,
        description="Minimum free space required on the source drive before backup proceeds (GB)",
    )
    rollover_auth_timeout_seconds: int = Field(
        default=30,
        ge=10,
        le=120,
        description="Timeout for gcloud auth activate-service-account during FY rollover (seconds)",
    )
    rollover_archive_timeout_seconds: int = Field(
        default=600,
        ge=60,
        le=7200,
        description="Timeout for gcloud storage objects update --storage-class=ARCHIVE during FY rollover (seconds)",
    )


class BudgetConfig(StrictModel):
    """Per-job run budgets (plan WP14). Counted from the job's start, including
    any wait for the backup slot; a job that reaches its budget stops cleanly as
    *_PARTIAL and continues next run. Starting values (owner 2026-10-15): tune
    from the duration of real runs."""
    cloud_max_hours: float = Field(
        default=6, ge=1, le=72,
        description="Cloud backup run budget (sync + size check). A first full upload "
                    "spreads over several nights within this budget")
    lan_max_hours: float = Field(default=4, ge=0.5, le=72,
                                 description="LAN backup run budget (NAS wake + mirror + shutdown)")
    audit_max_hours: float = Field(default=8, ge=0.5, le=72,
                                   description="Weekly integrity audit run budget (both legs)")
    cloud_verify_reserve_minutes: int = Field(
        default=60, ge=0, le=600,
        description="Of the cloud budget, kept for the size check / manifest after the upload")
    lan_post_reserve_minutes: int = Field(
        default=30, ge=0, le=600,
        description="Of the LAN budget, kept after the mirror for the destination walk and the "
                    "verified NAS shutdown")


class WatchdogConfig(StrictModel):
    """AamWatchdog tuning (plan WP13). Read once at watchdog start."""
    prefect_health_url: str = Field(
        default="http://127.0.0.1:4200/api/health", description="Prefect API health endpoint")
    check_interval_seconds: int = Field(default=60, ge=10, le=600,
                                        description="Normal poll interval")
    failure_threshold: int = Field(
        default=5, ge=1, le=60, description="Consecutive failed health checks before acting")
    request_timeout_seconds: int = Field(default=30, ge=1, le=120,
                                         description="HTTP timeout per health check")
    backup_wait_interval_seconds: int = Field(
        default=120, ge=10, le=1800, description="Re-check interval while a backup is running")
    max_deferrals: int = Field(
        default=15, ge=1, le=1000,
        description="Lock held but no rclone/robocopy: deferrals before the CRITICAL alert")
    max_transfer_deferrals: int = Field(
        default=240, ge=1, le=10_000,
        description=("rclone/robocopy running: deferrals before the zombie-transfer handling "
                     "(240 x 120 s = 8 h, above the longest legitimate transfer)"))
    restart_cooldown_seconds: int = Field(
        default=120, ge=0, le=1800, description="Pause after a restart before resuming checks")
    max_starts_per_window: int = Field(
        default=3, ge=1, le=20, description="Circuit breaker: auto-starts per service per window")
    start_window_seconds: int = Field(default=3600, ge=60, le=86_400,
                                      description="Circuit-breaker window")
    stop_pending_timeout_seconds: int = Field(
        default=600, ge=60, le=7200,
        description=("A service still STOP_PENDING after this long has its process killed - "
                     "only when no backup holds the lock; otherwise alert and wait. Must exceed "
                     "the agent's graceful stop (NSSM AppStopMethodConsole 180 s)"))
    kill_wait_seconds: int = Field(
        default=60, ge=5, le=600, description="After killing a stuck service: wait for STOPPED")
    api_ready_timeout_seconds: int = Field(
        default=300, ge=30, le=1800,
        description="Restart: wait this long for the Prefect API before starting the agent")
    service_poll_seconds: int = Field(default=5, ge=1, le=60,
                                      description="Poll interval while waiting for a service state")
    sc_timeout_seconds: int = Field(default=30, ge=5, le=120,
                                    description="Max seconds for one sc.exe call")
    log_rotation_mb: int = Field(
        default=10, ge=1, le=1000,
        description="Rotate the watchdog's own log (watchdog_app.log) at this size; kept "
                    "run_logs.service_log_retention_days")
    stale_check_interval_seconds: int = Field(
        default=1800, ge=60, le=86_400,
        description="How often the watchdog checks that every enabled leg had a successful "
                    "backup within dashboard.stale_after_hours; a stale leg is emailed "
                    "(repeated per alerts.repeat_cooldown_hours while it stays stale)")


class DashboardConfig(StrictModel):
    auth_enabled: bool = True
    api_key: str = Field(default="", description="API key for dashboard authentication")
    bind_address: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1024, le=65535)
    public_url: str = Field(
        default="",
        description="Dashboard address put into attention emails (e.g. "
                    "http://backup-server:8080). Empty = http://<bind_address>:<port>.",
    )
    # Owner 2026-10-18: office network only, never public. The installer opens
    # the dashboard port in Windows Firewall to these remote addresses only.
    firewall_remote_addresses: list[str] = Field(
        default_factory=lambda: ["LocalSubnet"], min_length=1,
        description="Who may open the dashboard over the network (Windows Firewall "
                    "RemoteAddress): LocalSubnet (the server's own office subnet), an IP, "
                    "a CIDR (10.10.151.0/24) or a range (10.10.151.10-10.10.151.50). "
                    "Applied by deploy/06_install_services.ps1 when bind_address is not "
                    "127.0.0.1.")
    stale_after_hours: float = Field(
        default=26, ge=1, le=720,
        description="Banner + /health turn red when an enabled leg has had no successful "
                    "backup for this many hours.")
    alert_failure_window_days: int = Field(
        default=7, ge=1, le=90,
        description="Banner + /health turn red when an alert or report email failed to be "
                    "delivered within this many days.")
    # Owner 2026-10-15: Advanced settings need a second, separate login.
    admin_password_hash: str = Field(
        default="",
        description="Advanced-settings login. Empty = the same as dashboard.api_key. Set it "
                    "ONLY via 'Change admin password' in Advanced (stores a salted PBKDF2 hash, "
                    "never the password).")
    admin_session_minutes: int = Field(
        default=30, ge=5, le=480,
        description="The Advanced-settings login expires after this many minutes")
    admin_password_min_length: int = Field(
        default=10, ge=8, le=128, description="Minimum length of a new admin password")

    @field_validator("admin_password_hash")
    @classmethod
    def _valid_admin_hash(cls, v: str) -> str:
        if v and not v.startswith("pbkdf2_sha256$"):
            raise ValueError("admin_password_hash must be empty or a hash written by the "
                             "dashboard's 'Change admin password' (never a plain password)")
        return v

    @field_validator("firewall_remote_addresses")
    @classmethod
    def _office_addresses_only(cls, v: list[str]) -> list[str]:
        ip = r"\d{1,3}(?:\.\d{1,3}){3}"
        allowed = re.compile(rf"^(?:LocalSubnet|{ip}(?:/\d{{1,2}})?|{ip}-{ip})$")
        bad = [a for a in v if not allowed.fullmatch(a.strip())]
        if bad:
            raise ValueError("office addresses only (LocalSubnet, an IP, a CIDR or a range), "
                             "never Any: " + ", ".join(repr(b) for b in bad))
        return [a.strip() for a in v]

    @model_validator(mode="after")
    def api_key_required_when_auth_enabled(self) -> "DashboardConfig":
        if self.auth_enabled and not self.api_key:
            raise ValueError("api_key must be set when auth_enabled is True")
        return self

    def __repr__(self) -> str:
        return f"DashboardConfig(auth_enabled={self.auth_enabled}, api_key='***', bind_address='{self.bind_address}', port={self.port})"

    def __str__(self) -> str:
        return self.__repr__()


class ScheduleConfig(StrictModel):
    """Per-deployment cron schedule configuration."""
    cloud_cron: str = Field(default="0 18 * * *", description="Cloud backup cron expression")
    lan_cron: str = Field(default="0 1 * * *", description="LAN backup cron expression")
    weekly_cron: str = Field(default="0 8 * * MON", description="Weekly report cron expression")
    monthly_cron: str = Field(
        default="30 8 1 * *",
        description="Monthly report cron expression. Default 08:30 so it never starts in the "
                    "same minute as the Monday 08:00 weekly report when the 1st is a Monday.")
    rollover_cron: str = Field(
        default="0 6 * * *",
        description="Daily FY rollover check cron (no-op except on the fiscal-year boundary)",
    )
    audit_cron: str = Field(
        default="0 10 * * SUN",
        description="Weekly read-only integrity audit (Sunday, single checker). 10:00 so the "
                    "default 01:00 LAN run (4 h budget) can never overlap it (WP14)",
    )
    timezone: str = Field(default="Asia/Kolkata", description="IANA timezone for all schedules")

    @field_validator("cloud_cron", "lan_cron", "weekly_cron", "monthly_cron", "rollover_cron", "audit_cron")
    @classmethod
    def valid_cron(cls, v: str) -> str:
        parts = v.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression '{v}': expected 5 fields (min hour dom month dow)")
        return v

    @model_validator(mode="after")
    def _validate_crons_with_prefect(self) -> "ScheduleConfig":
        """G1: validate cron VALUES and the timezone with Prefect's own
        CronSchedule model — the exact schema the Prefect server applies when
        deployments register.

        The old check accepted any 5-field string (e.g. "99 99 * * *" or
        timezone "Not/AZone"), which only failed later, at serve() deployment
        time — crash-looping the agent (and, since all deployments register in
        one serve() call, taking down every pipeline and report).

        Verified (probe E + 2026-08-18 experiment): standalone import works
        without a running server; ~3 ms per check; one-time ~1.8 s import cost
        at config-load time (accepted: watchdog/dashboard/flows load config at
        startup, not in a hot path).
        """
        from prefect.server.schemas.schedules import CronSchedule

        for field_name in ("cloud_cron", "lan_cron", "weekly_cron", "monthly_cron", "rollover_cron", "audit_cron"):
            try:
                CronSchedule(cron=getattr(self, field_name), timezone=self.timezone)
            except Exception as e:
                raise ValueError(
                    f"schedule.{field_name}={getattr(self, field_name)!r} "
                    f"(timezone={self.timezone!r}) rejected by Prefect's scheduler: {e}"
                ) from e
        return self


class AppConfig(StrictModel):
    firm_name: str = "AAM Associates"
    paths: PathsConfig
    lan: LanConfig = Field(default_factory=LanConfig)
    wol: WolConfig = Field(default_factory=WolConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    run_logs: RunLogsConfig = Field(default_factory=RunLogsConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    guards: GuardsConfig = Field(default_factory=GuardsConfig)
    approvals: ApprovalsConfig = Field(default_factory=ApprovalsConfig)
    rollover: RolloverConfig = Field(default_factory=RolloverConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    @model_validator(mode="after")
    def _schedule_fits_the_budgets(self) -> "AppConfig":
        """WP14 / DEC-7: the configured schedule must be provably free of slot
        overruns. Only jobs that take the backup slot are checked (reports and
        the daily no-op rollover check do not)."""
        from core.budget import schedule_problems

        jobs = {}
        if self.cloud.enabled:
            jobs["cloud"] = (self.schedule.cloud_cron, self.budget.cloud_max_hours * 3600)
        if self.lan.enabled:
            jobs["lan"] = (self.schedule.lan_cron, self.budget.lan_max_hours * 3600)
        jobs["audit"] = (self.schedule.audit_cron, self.budget.audit_max_hours * 3600)
        problems = schedule_problems(jobs, self.schedule.timezone,
                                     self.maintenance.concurrency_wait_seconds)
        if problems:
            raise ValueError("schedule does not fit the run budgets (plan WP14):\n  - "
                             + "\n  - ".join(problems))
        return self

    @property
    def run_logs_dir(self) -> Path:
        """Effective artefact root (configured dir or <runtime_dir>/run_logs)."""
        if self.run_logs.dir:
            return Path(self.run_logs.dir)
        return Path(self.paths.runtime_dir) / "run_logs"

    @model_validator(mode="after")
    def cross_field_validation(self) -> "AppConfig":
        if self.lan.enabled and not self.paths.lan_destination.startswith("\\\\"):
            raise ValueError("paths.lan_destination must be a UNC path when LAN is enabled")
        if self.cloud.enabled and not self.paths.gcs_key_path:
            raise ValueError("paths.gcs_key_path is required when cloud is enabled")
        if not self.lan.enabled and not self.cloud.enabled:
            raise ValueError("At least one destination (lan or cloud) must be enabled")

        # FY Mismatch Safety Guard:
        # Prevent mirroring FY24-25 into FY23-24 if a human typo occurs.
        fy_pattern = re.compile(r"^FY\d{2}-\d{2}$", re.IGNORECASE)

        # G2: 4-digit FY trap. Rollover detection only understands the 2-digit
        # convention (FY25-26); a name like FY2026-27 silently disables rollover
        # forever. Refuse at config load instead of at the FY boundary.
        fy_4digit = re.compile(r"FY\d{4}-\d{2,4}\b", re.IGNORECASE)
        for path_name, path_value in (("source_drive", self.paths.source_drive),
                                      ("lan_destination", self.paths.lan_destination)):
            for seg in path_value.replace("\\", "/").split("/"):
                seg = seg.strip()
                if fy_4digit.search(seg):
                    raise ValueError(
                        f"paths.{path_name} uses a 4-digit FY name ({seg!r}) — "
                        "fiscal-year rollover only supports the 2-digit convention "
                        "(FY25-26). Rename the FY folder: a 4-digit name silently "
                        "disables rollover forever, and every run after the FY "
                        "boundary would keep overwriting the previous year's data."
                    )
        src_parts = self.paths.source_drive.replace("\\", "/").rstrip("/").split("/")
        src_fy = src_parts[-1].upper() if fy_pattern.match(src_parts[-1]) else None

        lan_parts = self.paths.lan_destination.replace("\\", "/").rstrip("/").split("/")
        lan_fy = lan_parts[-1].upper() if fy_pattern.match(lan_parts[-1]) else None

        if src_fy and lan_fy and src_fy != lan_fy:
            raise ValueError(
                f"CRITICAL DATA LOSS PREVENTION: source_drive FY ({src_fy}) and "
                f"lan_destination FY ({lan_fy}) do not match! "
                "The system refuses to start because syncing would overwrite the old FY data with the new FY data. "
                "Please manually correct config.yaml so both paths point to the identical FY folder."
            )

        return self

    @classmethod
    def from_yaml(cls, path: str) -> "AppConfig":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(**_drop_deprecated_keys(data or {}))


def _drop_deprecated_keys(data: dict) -> dict:
    """Remove DEPRECATED_KEYS from a raw config mapping, warning per key."""
    from loguru import logger

    for section, keys in DEPRECATED_KEYS.items():
        values = data.get(section)
        if not isinstance(values, dict):
            continue
        for key in sorted(keys & values.keys()):
            values.pop(key)
            logger.warning(
                f"config: '{section}.{key}' is deprecated and ignored - "
                "remove it from config.yaml"
            )
    return data


def load_config(config_path: str = CONFIG_PATH) -> AppConfig:
    """Load and validate configuration from a YAML file."""
    return AppConfig.from_yaml(config_path)
