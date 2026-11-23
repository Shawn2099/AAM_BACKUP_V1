"""M4 4.6 - config.example.yaml documents every key, with the code's defaults.

Finding: 21 keys and the whole run_logs / alerts sections were missing from
the example, and it still showed superseded values (WoL wake 300 s /
stability 30 s instead of 900 s / 90 s; 30-day log retention instead of the
owner's 3 months). Operators copy this file, so a gap or a stale value
there is a wrong production setting later.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from models.config import AppConfig

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"

# Values that are deliberately placeholders or site choices, not defaults.
SITE_SPECIFIC = {
    "paths.source_drive", "paths.lan_destination", "paths.gcs_key_path",
    "wol.mac_address", "wol.server_ip",
    "cloud.bucket", "cloud.project_number",
    "schedule.lan_cron", "schedule.cloud_cron",   # the production-like proven schedule
    "notifications.smtp_host", "notifications.smtp_username", "notifications.smtp_password",
    "notifications.sender", "notifications.recipients",
    "dashboard.api_key", "firm_name",
}


@pytest.fixture(scope="module")
def example():
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def _sections():
    for name, field in AppConfig.model_fields.items():
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            yield name, ann


def test_every_key_is_documented(example):
    missing = [f"{name}.{key}" for name, model in _sections()
               for key in model.model_fields if key not in (example.get(name) or {})]
    assert missing == []


def test_no_unknown_keys(example):
    unknown = [f"{name}.{key}" for name, model in _sections()
               for key in (example.get(name) or {}) if key not in model.model_fields]
    assert unknown == []


def test_values_are_the_code_defaults_except_site_choices(example):
    stale = []
    for name, model in _sections():
        for key, value in (example.get(name) or {}).items():
            default = model.model_fields[key].get_default(call_default_factory=True)
            if f"{name}.{key}" in SITE_SPECIFIC:
                continue
            if value != default:
                stale.append(f"{name}.{key}: example {value!r} != default {default!r}")
    assert stale == []


def test_the_example_is_a_valid_config(example):
    AppConfig(**example)   # strict keys + the schedule / run-budget check
