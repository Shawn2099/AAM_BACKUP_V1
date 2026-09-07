"""Chaos-only safety enforcement. Importable guard — refuses anything
that is not provably the isolated chaos environment.

No existing module centralizes this (prod* scripts are point checks);
this guard runs before every fault, trigger, and cleanup action.
"""

import os

ALLOWED_PREFIXES = (
    r"C:\ChaosTest",
    r"C:\ChaosRuntime",
    r"\\127.0.0.1\aam_test",
)

DENIED_PREFIXES = (
    r"C:\AAMBackup",
    r"C:\BackupAgent",
    r"E:",
    r"F:",
    r"\\10.10.186.231",
)

CHAOS_BUCKET = "aam-chaos-67q1zs"
PROD_BUCKET = "aam-backup-demo-innovizta"

CHAOS_PREFECT_API = "http://127.0.0.1:4201/api"


class SafetyError(RuntimeError):
    pass


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def assert_chaos_path(path: str) -> str:
    """Allow only chaos-owned filesystem targets. Returns normalized path."""
    n = _norm(str(path))
    for denied in DENIED_PREFIXES:
        if n.startswith(_norm(denied)):
            raise SafetyError(f"production/forbidden target refused: {path!r}")
    for allowed in ALLOWED_PREFIXES:
        if n.startswith(_norm(allowed)):
            return n
    raise SafetyError(f"target outside chaos boundary refused: {path!r}")


def assert_chaos_bucket(bucket: str) -> str:
    if bucket == PROD_BUCKET:
        raise SafetyError(f"production bucket refused: {bucket!r}")
    if bucket != CHAOS_BUCKET:
        raise SafetyError(f"unknown bucket refused: {bucket!r}")
    return bucket


def assert_chaos_prefect(api_url: str) -> str:
    if api_url != CHAOS_PREFECT_API:
        raise SafetyError(f"non-chaos Prefect endpoint refused: {api_url!r}")
    return api_url


def assert_no_shutdown_target(host: str) -> str:
    """Option-B/shutdown paths may only ever address the chaos loopback."""
    h = (host or "").strip().lower().strip("\\")
    if h.startswith("127.0.0.1"):
        return host
    raise SafetyError(f"shutdown-capable path refused for host: {host!r}")
