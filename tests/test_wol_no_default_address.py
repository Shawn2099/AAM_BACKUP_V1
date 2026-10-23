"""No made-up NAS address: wol.server_ip has no default (it was 192.168.10.10, a real-looking address that a config
missing the key would silently wake / shut down)."""

import pytest
from pydantic import ValidationError

from models.config import WolConfig


def test_there_is_no_default_address():
    assert WolConfig(enabled=False).server_ip == ""


def test_wol_on_without_an_address_is_refused():
    with pytest.raises(ValidationError, match="NAS address"):
        WolConfig(enabled=True, mac_address="00:11:22:33:44:55")


def test_a_wrong_address_is_still_refused():
    with pytest.raises(ValidationError, match="valid IP address"):
        WolConfig(enabled=False, server_ip="nas01")
