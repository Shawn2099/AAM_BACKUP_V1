"""Realistic no-internet lanes: the dummy service-account key must be well-formed, so rclone's first step is the NETWORK.

Pinned API (to implement in tools/livebox/rig.py):
  rig.dummy_service_account_json(variant: str) -> str     variant 'blackhole' | 'dns'; ValueError otherwise
  LaneRig.build()      writes <runtime_dir>/dummy-key.json for the default (blackhole) variant
  LaneRig.set_cloud(v) for 'blackhole'/'dns' rewrites <runtime_dir>/dummy-key.json with that variant's token_uri
The private key is a throwaway, clearly fake RSA PKCS8 key (generated or committed test-only); never a real credential.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path

import pytest

from tools.livebox import rig as rigmod

TOKEN_URI = {"blackhole": "http://127.0.0.1:9/token", "dns": "https://aam-livebox.invalid/token"}
_REPO = Path(__file__).resolve().parents[3]
RCLONE = _REPO / "deploy" / "bin" / "rclone.exe"
CONNECTION_WORDS = ("refused", "no such host", "lookup", "dial", "timeout", "context deadline", "timed out")


@pytest.fixture
def rig(tmp_path):
    r = rigmod.LaneRig(tmp_path / "stamp1" / "laneR").build()
    # the rclone remote file lives in Seams; build it without patching any module (no install())
    r.seams = rigmod.Seams(str(r.lane_dir), str(r.bucket_root), None)
    yield r
    r.remove()


def _key(rig):
    return json.loads((rig.runtime_dir / "dummy-key.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- (1) key shape
@pytest.mark.parametrize("mode", ["blackhole", "dns"])
def test_key_json_is_a_valid_service_account(mode):
    k = json.loads(rigmod.dummy_service_account_json(mode))
    assert k["type"] == "service_account"
    assert k["project_id"]
    assert k["client_email"].lower().endswith(".invalid")
    assert k["token_uri"] == TOKEN_URI[mode]
    pem = k["private_key"]
    assert pem.startswith("-----BEGIN PRIVATE KEY-----") and pem.strip().endswith("-----END PRIVATE KEY-----")
    body = "".join(pem.strip().splitlines()[1:-1])
    assert len(base64.b64decode(body, validate=True)) > 600   # a real RSA-2048 PKCS8 DER, not a stub


@pytest.mark.parametrize("mode", ["blackhole", "dns"])
def test_private_key_parses_as_rsa_pkcs8(mode):
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    k = json.loads(rigmod.dummy_service_account_json(mode))
    key = serialization.load_pem_private_key(k["private_key"].encode(), password=None)
    assert key.key_size >= 2048


def test_unknown_variant_rejected():
    for bad in ("local", "gone", "bogus"):
        with pytest.raises(ValueError):
            rigmod.dummy_service_account_json(bad)


# ---------------------------------------------------------------- (2) never real Google
@pytest.mark.parametrize("mode", ["blackhole", "dns"])
def test_nothing_points_at_google(mode, rig):
    assert "googleapis.com" not in rigmod.dummy_service_account_json(mode).lower()
    assert "google.com" not in rigmod.dummy_service_account_json(mode).lower()
    text = rigmod.rclone_config_text(mode, key_path="k.json")
    assert "googleapis.com" not in text.lower()
    rig.set_cloud(mode)
    on_disk = (rig.runtime_dir / "dummy-key.json").read_text(encoding="utf-8").lower()
    assert "googleapis.com" not in on_disk and "google.com" not in on_disk
    assert "googleapis.com" not in rig.seams.rclone_conf.read_text(encoding="utf-8").lower()


# ---------------------------------------------------------------- set_cloud writes the matching key
def test_build_default_key_is_valid_blackhole(rig):
    k = _key(rig)
    assert k["token_uri"] == TOKEN_URI["blackhole"] and k["private_key"].startswith("-----BEGIN PRIVATE KEY-----")


@pytest.mark.parametrize("mode", ["blackhole", "dns"])
def test_set_cloud_rewrites_key_for_mode(rig, mode):
    rig.set_cloud("dns" if mode == "blackhole" else "blackhole")   # start from the other one
    rig.set_cloud(mode)
    k = _key(rig)
    assert k["token_uri"] == TOKEN_URI[mode]
    assert k["client_email"].endswith(".invalid") and k["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
    conf = rig.seams.rclone_conf.read_text(encoding="utf-8")
    assert str(rig.runtime_dir / "dummy-key.json") in conf


# ---------------------------------------------------------------- (4) local and gone unchanged
def test_local_and_gone_unchanged(rig):
    rig.set_cloud("blackhole")
    rig.set_cloud("local")
    conf = rig.seams.rclone_conf.read_text(encoding="utf-8")
    assert "type = alias" in conf and "google cloud storage" not in conf
    assert rigmod.rclone_config_text("local").strip().endswith("type = local")
    rig.set_cloud("gone")
    assert (rig.bucket_root / (rig.bucket_name + ".gone")).exists()
    assert not (rig.bucket_root / rig.bucket_name).exists()
    rig.set_cloud("local")
    assert (rig.bucket_root / rig.bucket_name).exists()
    with pytest.raises(ValueError):
        rigmod.rclone_config_text("nope")


# ---------------------------------------------------------------- (3) real rclone: a network failure, not a key failure
@pytest.mark.skipif(not RCLONE.exists(), reason="deploy/bin/rclone.exe missing")
@pytest.mark.parametrize("mode", ["blackhole", "dns"])
def test_real_rclone_fails_on_the_network_not_the_key(rig, mode):
    rig.set_cloud(mode)
    p = subprocess.run(
        [str(RCLONE), "--config", str(rig.seams.rclone_conf), "ls", "aam_gcs:probe-bucket", "--max-depth", "1",
         "--retries", "1", "--low-level-retries", "1", "--timeout", "10s"],
        capture_output=True, text=True, timeout=120)
    err = (p.stderr or "") + (p.stdout or "")
    assert p.returncode != 0
    assert "private key should be" not in err, err
    low = err.lower()
    assert any(w in low for w in CONNECTION_WORDS), err
