"""Pydantic v2 configuration models for AAM Backup Automation V1.

Validated on load. No dead sections. Only what's actually used.
"""

import ipaddress
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PathsConfig(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source_drive: str = Field(..., description="Source drive root path, e.g. D:\\")
    lan_destination: str = Field(..., description="LAN UNC path, e.g. \\\\192.168.10.10\\share$")
    database_path: str = Field(..., description="Path to SQLite manifest database")
    log_directory: str = Field(default="C:\\BackupAgent\\logs")
    temp_directory: str = Field(default="C:\\BackupAgent\\rclone_temp")
    gcs_key_path: str = Field(..., description="Path to GCS service account JSON key file")

    @field_validator("source_drive")
    @classmethod
    def source_drive_exists(cls, v: str) -> str:
        if not Path(v).exists():
            raise ValueError(f"Source drive does not exist: {v}")
        return v

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
        if not Path(v).exists():
            raise ValueError(f"GCS key file not found: {v}")
        return v


class LanConfig(BaseModel):
    enabled: bool = True
    retry_count: int = Field(default=3, ge=1, le=10)
    retry_wait_seconds: int = Field(default=10, ge=1, le=300)
    subprocess_timeout_seconds: int = Field(default=14400, ge=3600)
    shutdown_after_backup: bool = True


class WolConfig(BaseModel):
    enabled: bool = True
    mac_address: str
    server_ip: str = "192.168.10.10"
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


class CloudConfig(BaseModel):
    enabled: bool = True
    bucket: str = "aam-backup-demo-innovizta"
    project_number: str = "920173882190"
    location: str = "asia-south1"
    storage_class: str = "COLDLINE"
    bandwidth_limit: str = "10M"
    retry_count: int = Field(default=3, ge=1, le=10)
    subprocess_timeout_seconds: int = Field(default=21600, ge=3600)

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
    weekly_summary_day: str = "monday"
    weekly_summary_time: str = "08:00"

    @field_validator("smtp_port")
    @classmethod
    def valid_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError(f"Invalid SMTP port: {v}")
        return v


class AppConfig(BaseModel):
    firm_name: str = "AAM Associates"
    paths: PathsConfig
    lan: LanConfig = Field(default_factory=LanConfig)
    wol: WolConfig = Field(default_factory=WolConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)

    @model_validator(mode="after")
    def cross_field_validation(self) -> "AppConfig":
        if self.lan.enabled and not self.paths.lan_destination.startswith("\\\\"):
            raise ValueError("paths.lan_destination must be a UNC path when LAN is enabled")
        if self.cloud.enabled and not self.paths.gcs_key_path:
            raise ValueError("paths.gcs_key_path is required when cloud is enabled")
        if not self.lan.enabled and not self.cloud.enabled:
            raise ValueError("At least one destination (lan or cloud) must be enabled")
        return self

    @classmethod
    def from_yaml(cls, path: str) -> "AppConfig":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)


def load_config(config_path: str) -> AppConfig:
    """Load and validate configuration from a YAML file."""
    return AppConfig.from_yaml(config_path)
