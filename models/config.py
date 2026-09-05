"""Pydantic v2 configuration models for AAM Backup Automation V1.

Validated on load. No dead sections. Only what's actually used.
"""

import ipaddress
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_PATH = "config.yaml"

_DEFAULT_RUNTIME_DIR = os.environ.get(
    "AAM_RUNTIME_DIR",
    r"C:\BackupAgent",
)


class PathsConfig(BaseModel):
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
    gcs_key_path: str = Field(..., description="Path to GCS service account JSON key file")

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
        if not v:
            raise ValueError("gcs_key_path must not be empty when cloud is enabled")
        return v


class LanConfig(BaseModel):
    enabled: bool = True
    retry_count: int = Field(default=3, ge=1, le=10)
    retry_wait_seconds: int = Field(default=10, ge=1, le=300)
    subprocess_timeout_seconds: int = Field(default=14400, ge=3600)
    shutdown_after_backup: bool = True
    max_attempts: int = Field(default=2, ge=1, le=10, description="Flow-level retry attempts for LAN backup orchestration")
    retry_delay_seconds: int = Field(default=600, ge=60, le=3600, description="Delay between flow-level retry attempts")
    mt_threads: int = Field(default=4, ge=1, le=128, description="Robocopy /MT multi-threaded copy count")
    # F8: the preflight /L walk scales with dataset size (1M files ~= 5-10 min
    # over SMB; 2.5M ~= 12-25 min). The old hardcoded 300s failed at exactly the
    # scale this deployment targets, aborting a healthy backup before it started.
    dry_run_timeout_seconds: int = Field(default=900, ge=60, le=7200, description="Timeout for the robocopy /L preflight dry-run (seconds)")


class WolConfig(BaseModel):
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
    wake_timeout_seconds: int = Field(default=300, ge=60, le=600)
    ping_interval_seconds: int = Field(default=15, ge=5, le=60)
    stability_wait_seconds: int = Field(default=30, ge=0)
    # G3: a single unicast-ish broadcast packet is frequently dropped
    # (managed switches, NIC firmware, half-initiated NIC wake state).
    # Standard practice (cf. etherwake -s/-I): send several rounds.
    # 3 rounds × 5 s adds only ~10 s against a 300 s wake timeout.
    wake_retry_count: int = Field(default=3, ge=1, le=10)
    wake_retry_interval_seconds: int = Field(default=5, ge=1, le=30)

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


class CloudConfig(BaseModel):
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
    project_number: str = "920173882190"
    location: str = "asia-south1"
    storage_class: str = "STANDARD"
    bandwidth_limit: str = "10M"
    retry_count: int = Field(default=3, ge=1, le=10)
    subprocess_timeout_seconds: int = Field(default=21600, ge=3600)
    max_attempts: int = Field(default=3, ge=1, le=10, description="Flow-level retry attempts for cloud backup orchestration")
    retry_delay_seconds: int = Field(default=300, ge=60, le=3600, description="Delay between flow-level retry attempts")
    verify_timeout_seconds: int = Field(default=14400, ge=60, le=86400, description="Timeout for post-sync rclone check verify step (seconds). Increase to 14400+ for large datasets on HDD.")
    preflight_timeout_seconds: int = Field(default=300, ge=30, le=3600, description="Timeout for pre-sync rclone check dry-run (seconds)")
    # F7: listing timeouts sized for the target scale (200GB / ~1M files, growing
    # to ~2.5M by yr-5). Measured order-of-magnitude: rclone size over GCS takes
    # ~40-150s at 1M objects, the lsjson manifest ~2-5 min, the diff ~3-8 min —
    # the old defaults (30/300/600) were tuned for a demo-sized bucket and
    # would time out (and misreport "0 files") at the real scale.
    diff_timeout_seconds: int = Field(default=1800, ge=30, le=7200, description="Timeout for rclone check --combined diff report (seconds)")
    manifest_timeout_seconds: int = Field(default=900, ge=30, le=7200, description="Timeout for rclone lsjson manifest listing (seconds)")
    cloud_size_timeout_seconds: int = Field(default=300, ge=10, le=3600, description="Timeout for rclone size GCS object count query (seconds)")
    transfers: int = Field(default=2, ge=1, le=64, description="rclone --transfers concurrent file transfers")
    checkers: int = Field(default=4, ge=1, le=64, description="rclone --checkers concurrent file checkers")
    buffer_size: str = Field(default="64M", description="rclone --buffer-size per transfer slot. 2 transfers × 64M = 128M total.")

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


class NotificationConfig(BaseModel):
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

    @field_validator("smtp_port")
    @classmethod
    def valid_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError(f"Invalid SMTP port: {v}")
        return v

    def __repr__(self) -> str:
        return (
            f"NotificationConfig(smtp_host='{self.smtp_host}', smtp_port={self.smtp_port}, "
            f"smtp_username='{self.smtp_username}', smtp_password='***', "
            f"sender='{self.sender}', recipients={self.recipients}, "
            f"send_on_failure={self.send_on_failure})"
        )

    def __str__(self) -> str:
        return self.__repr__()


class MaintenanceConfig(BaseModel):
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


class HealthConfig(BaseModel):
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


class DashboardConfig(BaseModel):
    auth_enabled: bool = True
    api_key: str = Field(default="", description="API key for dashboard authentication")
    bind_address: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1024, le=65535)

    @model_validator(mode="after")
    def api_key_required_when_auth_enabled(self) -> "DashboardConfig":
        if self.auth_enabled and not self.api_key:
            raise ValueError("api_key must be set when auth_enabled is True")
        return self

    def __repr__(self) -> str:
        return f"DashboardConfig(auth_enabled={self.auth_enabled}, api_key='***', bind_address='{self.bind_address}', port={self.port})"

    def __str__(self) -> str:
        return self.__repr__()


class ScheduleConfig(BaseModel):
    """Per-deployment cron schedule configuration."""
    cloud_cron: str = Field(default="0 18 * * *", description="Cloud backup cron expression")
    lan_cron: str = Field(default="0 1 * * *", description="LAN backup cron expression")
    weekly_cron: str = Field(default="0 8 * * MON", description="Weekly report cron expression")
    monthly_cron: str = Field(default="0 8 1 * *", description="Monthly report cron expression")
    rollover_cron: str = Field(
        default="0 6 * * *",
        description="Daily FY rollover check cron (no-op except on the fiscal-year boundary)",
    )
    audit_cron: str = Field(
        default="0 3 * * SUN",
        description="Weekly read-only integrity audit (Sunday low-load window, single checker)",
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


class AppConfig(BaseModel):
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
        return cls(**data)


def load_config(config_path: str = CONFIG_PATH) -> AppConfig:
    """Load and validate configuration from a YAML file."""
    return AppConfig.from_yaml(config_path)
