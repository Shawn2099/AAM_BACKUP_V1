"""Pre-backup health checks — source drive, binaries, disk space, clock, GCS key."""

import http.client
import re
import shutil
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

import pendulum
from loguru import logger

from core.process import resolve_binary
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool
from models.config import HealthConfig, model_default


class HealthError(RuntimeError):
    """Raised when a pre-backup health check fails."""


def check_source_drive(source_path: str, min_free_gb: int = model_default(HealthConfig, "min_free_source_gb")) -> tuple[bool, str]:
    """Verify source drive exists, can be listed, and has free space.

    Whether the source holds any files is NOT judged here: that is guard G1
    (core.guards, M2 2.2) — an empty or folders-only source is BLOCKED for
    a human decision, and the mirror-command builders refuse it as a last
    line of defence.

    Args:
        min_free_gb: Minimum free space required (GB). Override via config.health.min_free_source_gb.

    Returns:
        (True, "") if healthy.
        (False, "reason") if check failed.
    """
    source = Path(source_path)
    if not source.exists():
        return False, f"Source drive not accessible: {source}"

    try:
        next(iter(source.iterdir()), None)  # lists the root; reads one entry only
    except PermissionError:
        return False, f"Source drive permission denied: {source}"
    except OSError as e:
        return False, f"Source drive error: {e}"

    try:
        usage = shutil.disk_usage(str(source))
        free_gb = usage.free / (1024**3)
        if free_gb < min_free_gb:
            return False, (
                f"Source drive critically low on space: {free_gb:.1f} GB free "
                f"(minimum: {min_free_gb} GB)"
            )
        logger.debug(f"Source drive OK: {source} ({free_gb:.1f} GB free)")
    except OSError:
        logger.warning(f"Could not check disk space on {source} - skipping")

    return True, ""


def check_binary_exists(name: str) -> bool:
    """Check if binary is available locally or in PATH."""
    return resolve_binary(name) is not None


def check_gcs_key(key_path: str) -> tuple[bool, str]:
    """Verify GCS service account key file exists."""
    kp = Path(key_path)
    if not kp.exists():
        return False, f"GCS key file not found: {key_path}"
    if kp.stat().st_size == 0:
        return False, f"GCS key file is empty: {key_path}"
    return True, ""


CLOCK_OK = "OK"
CLOCK_SKEWED = "SKEWED"
CLOCK_UNVERIFIED = "UNVERIFIED"

# `12:10:52, +00.9227616s` - the time token is locale-formatted, the offset is not
_W32TM_SAMPLE = re.compile(r",\s*([+-]?\d+[.,]\d+)s\s*$")


@dataclass
class ClockResult:
    """Clock check verdict (plan WP12): never OK unless a source was measured."""

    state: str
    skew_s: float | None
    source: str | None
    reason: str

    def to_metrics(self) -> dict:
        return asdict(self)


def _google_offset(connection_timeout: float) -> tuple[float | None, str]:
    """Seconds between local UTC and Google's HTTP Date header, or (None, why)."""
    try:
        conn = http.client.HTTPSConnection("www.googleapis.com", timeout=connection_timeout)
        try:
            conn.request("HEAD", "/")
            google_date_str = conn.getresponse().getheader("Date")
        finally:
            conn.close()
    except OSError as e:
        return None, f"Google not reachable: {type(e).__name__}: {e}"
    if not google_date_str:
        return None, "Google sent no Date header"
    try:
        google_time = parsedate_to_datetime(google_date_str)
    except (TypeError, ValueError) as e:
        return None, f"unparseable Google Date header {google_date_str!r}: {e}"
    return (pendulum.now("UTC") - google_time).total_seconds(), "google"


def _w32tm_offset(ntp_server: str, timeout_s: float,
                  artefacts: RunArtefacts) -> tuple[float | None, str]:
    """Offset from one `w32tm /stripchart` sample, or (None, why).

    w32tm exits 0 even when the server never answers (`error: 0x800705B4`),
    so only a parsed sample line counts.
    """
    res = run_tool("w32tm", ["w32tm", "/stripchart", f"/computer:{ntp_server}",
                             "/samples:1", "/dataonly"],
                   timeout_s=timeout_s, artefacts=artefacts)
    if not res.completed:
        return None, f"w32tm: {res.describe_failure()}"
    text = res.stdout_text().strip()
    for line in reversed(text.splitlines()):
        m = _W32TM_SAMPLE.search(line)
        if m:
            return float(m.group(1).replace(",", ".")), f"w32tm:{ntp_server}"
    last = text.splitlines()[-1] if text else "no output"
    return None, f"w32tm {ntp_server} (exit {res.exit_code}): {last}"


def check_clock(
    *,
    max_skew_seconds: int,
    connection_timeout: float,
    ntp_server: str,
    w32tm_timeout_s: float,
    artefacts: RunArtefacts,
) -> ClockResult:
    """OK | SKEWED | UNVERIFIED (plan WP12).

    GCS rejects OAuth JWTs when the clock is off by more than ~10 min. Google's
    Date header is tried first; if it cannot be read (Sophos TLS inspection,
    DNS), one w32tm sample from `ntp_server`. Only when neither source answers
    is the clock UNVERIFIED - never silently OK.
    """
    offset, google_why = _google_offset(connection_timeout)
    source = "google"
    if offset is None:
        logger.warning(f"Clock check: {google_why} - trying w32tm against {ntp_server}")
        offset, w32_why = _w32tm_offset(ntp_server, w32tm_timeout_s, artefacts)
        if offset is None:
            reason = f"{google_why}; {w32_why}"
            logger.warning(f"Clock UNVERIFIED: {reason}")
            return ClockResult(CLOCK_UNVERIFIED, None, None, reason)
        source = w32_why
    skew = abs(offset)
    if skew > max_skew_seconds:
        reason = (f"System clock skew detected: {skew:.0f}s difference from {source} "
                  f"(max allowed: {max_skew_seconds}s). Run 'w32tm /resync'.")
        logger.error(reason)
        return ClockResult(CLOCK_SKEWED, round(skew, 3), source, reason)
    logger.debug(f"Clock OK: {skew:.1f}s skew from {source} (limit: {max_skew_seconds}s)")
    return ClockResult(CLOCK_OK, round(skew, 3), source, "")


def pre_backup_health(
    source_path: str,
    mode: str,
    gcs_key_path: str | None = None,
    min_free_source_gb: int = model_default(HealthConfig, "min_free_source_gb"),
    max_clock_skew_seconds: int = model_default(HealthConfig, "max_clock_skew_seconds"),
    clock_check_timeout_seconds: int = model_default(HealthConfig, "clock_check_timeout_seconds"),
    clock_unverified_policy: str = model_default(HealthConfig, "clock_unverified_policy"),
    ntp_server: str = model_default(HealthConfig, "ntp_server"),
    w32tm_timeout_seconds: int = model_default(HealthConfig, "w32tm_timeout_seconds"),
    artefacts: RunArtefacts | None = None,
) -> ClockResult | None:
    """Run all pre-backup health checks. Raises HealthError on failure.

    Args:
        source_path: Source drive root path.
        mode: "cloud", "lan", or "all"
        gcs_key_path: Path to GCS service account key. Required for cloud mode.
        min_free_source_gb: Override via config.health.min_free_source_gb.
        max_clock_skew_seconds: Override via config.health.max_clock_skew_seconds.
        clock_check_timeout_seconds: Override via config.health.clock_check_timeout_seconds.
        clock_unverified_policy / ntp_server / w32tm_timeout_seconds: config.health.*.
        artefacts: where the w32tm fallback output is kept.

    Returns:
        The clock verdict (cloud/all modes; None for lan) for the run row.

    Raises:
        HealthError: If any critical check fails — key missing, clock skewed
            (or unverified under policy "block"), rclone/robocopy not found,
            or source drive inaccessible.
    """
    clock = None
    valid_modes = {"cloud", "lan", "all"}
    if mode not in valid_modes:
        raise HealthError(f"Invalid mode '{mode}' - expected one of: {', '.join(sorted(valid_modes))}")

    ok, reason = check_source_drive(source_path, min_free_gb=min_free_source_gb)
    if not ok:
        raise HealthError(reason)

    if mode in ("cloud", "all"):
        if not check_binary_exists("rclone"):
            raise HealthError("rclone not found in PATH")
        if not gcs_key_path:
            raise HealthError("GCS key path is required for cloud mode")
        if gcs_key_path:
            ok, reason = check_gcs_key(gcs_key_path)
            if not ok:
                raise HealthError(reason)
        clock = check_clock(
            max_skew_seconds=max_clock_skew_seconds,
            connection_timeout=clock_check_timeout_seconds,
            ntp_server=ntp_server,
            w32tm_timeout_s=w32tm_timeout_seconds,
            artefacts=artefacts or RunArtefacts.ephemeral("clock"),
        )
        if clock.state == CLOCK_SKEWED:
            # Clock skew >10 min causes GCS JWT authentication to be rejected
            raise HealthError(f"Clock skew exceeds limit: {clock.reason}")
        if clock.state == CLOCK_UNVERIFIED:
            if clock_unverified_policy == "block":
                raise HealthError(
                    f"Clock could not be verified (health.clock_unverified_policy: block): "
                    f"{clock.reason}. Allow HTTPS to www.googleapis.com or NTP to {ntp_server}.")
            logger.warning(f"Clock could not be verified - continuing "
                           f"(health.clock_unverified_policy: warn): {clock.reason}")

    if mode in ("lan", "all"):
        if not check_binary_exists("robocopy"):
            raise HealthError("robocopy not found in PATH")

    logger.info(f"Pre-backup health check passed (mode={mode})")
    return clock
