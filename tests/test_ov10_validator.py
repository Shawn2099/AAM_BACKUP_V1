"""OV-10.3 (plan 5.10, R-25): the NAS FY folder must not sit directly at a share root (the share IS the FY folder,
a path like \\\\nas\\FY26-27) while the LAN leg and the overlap are on: the next year's folder could never be
created beside it."""

import pytest
from pydantic import ValidationError

from models.config import AppConfig, CloudConfig, LanConfig, PathsConfig, RolloverConfig, WolConfig


def _cfg(lan_destination, *, lan=True, overlap=True):
    return AppConfig(
        paths=PathsConfig(source_drive="E:\\FY26-27", lan_destination=lan_destination,
                          gcs_key_path="key.json"),
        lan=LanConfig(enabled=lan),
        wol=WolConfig(enabled=False),
        cloud=CloudConfig(enabled=not lan, bucket="my-bucket", project_number="1"),
        rollover=RolloverConfig(overlap_enabled=overlap),
    )


def test_fy_folder_as_the_share_itself_is_refused():
    with pytest.raises(ValidationError, match="share root"):
        _cfg("\\\\nas\\FY26-27")


def test_trailing_slash_does_not_hide_it():
    with pytest.raises(ValidationError, match="share root"):
        _cfg("\\\\nas\\FY26-27\\")


def test_fy_folder_inside_a_share_is_fine():
    assert _cfg("\\\\nas\\backup\\FY26-27").paths.lan_destination == "\\\\nas\\backup\\FY26-27"


def test_allowed_when_overlap_is_off():
    assert _cfg("\\\\nas\\FY26-27", overlap=False)


def test_allowed_when_lan_is_off():
    # cloud-only: the NAS path is an unused placeholder
    assert _cfg("\\\\nas\\FY26-27", lan=False)
