"""Pydantic v2 configuration models for AAM Backup Automation V1.

Validated on load. No dead sections. Only what's actually used.
"""

import ipaddress
import re

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_PATH = "config.yaml"


class PathsConfig(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source_drive: str = Field(..., description="Source drive root path, e.g. D:\\")
    lan_destination: str = Field(..., description="LAN UNC path, e.g. \\\\192.168.10.10\\share$")
    database_path: str = Field(..., description="Path to SQLite manifest database")
    log_directory: str = Field(default="C:\\BackupAgent\\logs")
    gcs_key_path: str = Field(..., description="Path to GCS service account JSON key file")

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

    @field_validator("database_path")
    @classmethod
    def db_path_ends_with_db(cls, v: str) -> str:
        if not v.endswith(".db"):
            raise ValueError(f"Database path must end with .db: {v}")
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
    mt_threads: int = Field(default=8, ge=1, le=128, description="Robocopy /MT multi-threaded copy count")


class WolConfig(BaseModel):
    enabled: bool = True
    mac_address: str
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

    @field_validator("mac_address")
    @classmethod
    def valid_mac(cls, v: str) -> str:
        if not v or not re.match(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$", v):
            raise ValueError(f"Invalid MAC address format: {v}")
        return v

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
    bucket: str = "aam-backup-demo-innovizta"
    project_number: str = "920173882190"
    location: str = "asia-south1"
    storage_class: str = "STANDARD"
    bandwidth_limit: str = "10M"
    retry_count: int = Field(default=3, ge=1, le=10)
    subprocess_timeout_seconds: int = Field(default=21600, ge=3600)
    max_attempts: int = Field(default=3, ge=1, le=10, description="Flow-level retry attempts for cloud backup orchestration")
    retry_delay_seconds: int = Field(default=300, ge=60, le=3600, description="Delay between flow-level retry attempts")
    verify_timeout_seconds: int = Field(default=600, ge=60, le=7200, description="Timeout for post-sync rclone check verify step")
    transfers: int = Field(default=4, ge=1, le=64, description="rclone --transfers concurrent file transfers")
    checkers: int = Field(default=16, ge=1, le=64, description="rclone --checkers concurrent file checkers")

    @field_validator("bucket")
    @classmethod
    def valid_bucket(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$", v):
            raise ValueError(f"Invalid bucket name: {v}")
        return v

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
    send_on_failure: bool = True
    send_on_success: bool = False
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
    db_retention_days: int = Field(
        default=90,
        ge=7,
        le=3650,
        description="Days of run history to keep in ManifestDB (7–3650)",
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
    timezone: str = Field(default="Asia/Kolkata", description="IANA timezone for all schedules")

    @field_validator("cloud_cron", "lan_cron", "weekly_cron", "monthly_cron")
    @classmethod
    def valid_cron(cls, v: str) -> str:
        parts = v.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression '{v}': expected 5 fields (min hour dom month dow)")
        return v


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
