r"""Livebox safety refusals S1-S15 (plan LIVEBOX_TEST_PLAN_2026-11-24.md, sections 1.8 and 7.1).

Pure and injectable: nothing here reads the production system (no network, no ssh, no subprocess).
The clock, the process count, the free space, the git facts and the production lock path are all
parameters. One function per S-id; each returns None when the check passes and raises
`SafetyRefusal` (abort the lane) or `SafetyPause` (production busy: wait and re-check) with
`.rule` set to the S-id.

`run_all(ctx) -> list[Result]` runs every check from a `SafetyContext` and never raises for a
refusal; a fact the context does not supply makes its check refuse (fail closed).
"""
from __future__ import annotations

import datetime as dt
import ntpath
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

LIVEBOX_ROOT = r"C:\AAMLivebox"
LANE_MARKER = ".livebox_lane"     # file inside a lane dir, written by LaneRig.build; cleanup refuses without it
SANDBOX_FY_LEAVES = ("FY90-91", "FY91-92", "FY92-93", "FY93-94")
CENTURY_FY_LEAVES = ("FY99-00", "FY00-01")
GB = 1024 ** 3
MIN_FREE_BYTES = 50 * GB
MAX_LANE_ROOT_BYTES = 20 * GB
MAX_LANE_PROCESSES = 3
GAP_MINUTES = 30
PROD_PREFECT_PORT = 4200
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_TIMEZONE = "Asia/Kolkata"


class SafetyError(Exception):
    def __init__(self, rule: str, message: str):
        self.rule = rule
        self.message = message
        super().__init__(f"{rule}: {message}")


class SafetyRefusal(SafetyError):
    """Abort the lane (S1-S9, S12-S15)."""


class SafetyPause(SafetyError):
    """Production is busy (S10, S11): pause and re-check, do not abort."""


@dataclass(frozen=True)
class Result:
    rule: str
    status: str  # "ok" | "refuse" | "pause"
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# --------------------------------------------------------------------------- path helpers

_UNC_LOOPBACK = re.compile(r"^\\\\(?:127\.0\.0\.1|localhost|\[?::1\]?)\\([A-Za-z])\$(?:\\(.*))?$", re.IGNORECASE)


def norm(path: Any) -> str:
    """Normalise for comparison: lower case, `..` resolved, no trailing slash; a loopback admin share
    (\\\\127.0.0.1\\C$\\x) maps to its drive path (C:\\x)."""
    p = str(path).strip().strip('"').replace("/", "\\")
    m = _UNC_LOOPBACK.match(p)
    if m:
        p = f"{m.group(1)}:\\{m.group(2) or ''}"
    p = ntpath.normpath(p)
    if len(p) > 3:
        p = p.rstrip("\\")
    return p.lower()


def is_under(path: Any, root: Any) -> bool:
    """True if `path` equals `root` or lies below it (a sibling with the same prefix does not count)."""
    p, r = norm(path), norm(root)
    if p == r:
        return True
    return p.startswith(r if r.endswith("\\") else r + "\\")


def _leaf(path: str) -> str:
    return ntpath.basename(norm(path).rstrip("\\")).upper()


# --------------------------------------------------------------------------- S1 - S9

def check_box_head(head: str | None, expected_commit: str | None, porcelain: str | None) -> None:
    """S1: box HEAD is the owner-named commit and the tracked tree is clean."""
    if not head or not expected_commit or porcelain is None:
        raise SafetyRefusal("S1", "fail closed: head, expected commit or porcelain not supplied")
    h, e = head.strip().lower(), expected_commit.strip().lower()
    if not (h == e or (len(e) >= 7 and h.startswith(e)) or (len(h) >= 7 and e.startswith(h))):
        raise SafetyRefusal("S1", f"box HEAD {h[:12]} is not the expected commit {e[:12]}")
    if porcelain.strip():
        raise SafetyRefusal("S1", "box working tree has tracked changes (status --porcelain not empty)")


def check_sandbox_paths(paths: Mapping[str, str], lane_root: str) -> None:
    """S2: every sandbox path is under the lane root C:\\AAMLivebox\\<stamp>\\<lane>."""
    root = norm(lane_root)
    live = norm(LIVEBOX_ROOT)
    rel = root[len(live):].strip("\\") if is_under(root, live) else ""
    if not rel or len(rel.split("\\")) != 2:
        # the lane root itself must be C:\AAMLivebox\<stamp>\<lane>
        raise SafetyRefusal("S2", f"lane root {lane_root!r} is not {LIVEBOX_ROOT}\\<stamp>\\<lane>")
    if not paths:
        raise SafetyRefusal("S2", "fail closed: no sandbox paths supplied")
    for name, p in paths.items():
        if not p or not is_under(p, lane_root):
            raise SafetyRefusal("S2", f"{name} = {p!r} is not under the lane root {lane_root!r}")


def check_not_production_paths(paths: Mapping[str, str], production: Iterable[str]) -> None:
    """S3: no sandbox path equals or is under a production root."""
    prod = [x for x in production if x]
    if not prod:
        raise SafetyRefusal("S3", "fail closed: no production roots supplied")
    for name, p in paths.items():
        for root in prod:
            if is_under(p, root):
                raise SafetyRefusal("S3", f"{name} = {p!r} equals or is under production path {root!r}")


def sandbox_leaves_from(first_fy: str, count: int = 4) -> tuple[str, ...]:
    """The lane's FY leaves for a start-derived FY: first_fy and the next count-1 (e.g. FY26-27 .. FY29-30)."""
    a = int(first_fy[2:4])
    return tuple(f"FY{(a + i) % 100:02d}-{(a + i + 1) % 100:02d}" for i in range(count))


def check_fy_leaves(leaves: Iterable[str], production_leaves: Iterable[str], century: bool = False,
                    sandbox_leaves: Iterable[str] | None = None) -> None:
    """S4: sandbox leaves are test years; production is not configured on one of them.

    sandbox_leaves (start-derived, e.g. a 2026 start) replaces the fixed FY90-93 list. Such leaves can share a
    name with production's own FY, so the production-name comparison is skipped for them: S3 (the paths are under
    the lane folder, never a production root) is what keeps a lane away from production."""
    derived = sandbox_leaves is not None
    allowed = {x.upper() for x in (sandbox_leaves if derived else SANDBOX_FY_LEAVES)}
    if century:
        allowed |= {x.upper() for x in CENTURY_FY_LEAVES}
    for leaf in leaves:
        if str(leaf).upper() not in allowed:
            raise SafetyRefusal("S4", f"FY leaf {leaf!r} is not a sandbox year ({sorted(allowed)})")
    every_sandbox = {x.upper() for x in SANDBOX_FY_LEAVES + CENTURY_FY_LEAVES}
    for leaf in ([] if derived else production_leaves):
        if str(leaf).upper() in every_sandbox:
            raise SafetyRefusal("S4", f"production is configured on sandbox FY {leaf!r}")


def check_config_binding(config_path: str | None, lane_config: str | None, flow_file: str | None,
                         box_code_root: str) -> None:
    """S5: CONFIG_PATH is the lane config and flow.__file__ is under the box checkout."""
    if not config_path or not lane_config or not flow_file:
        raise SafetyRefusal("S5", "fail closed: config path, lane config or flow file not supplied")
    if norm(config_path) != norm(lane_config):
        raise SafetyRefusal("S5", f"CONFIG_PATH {config_path!r} is not the lane config {lane_config!r}")
    if not is_under(flow_file, box_code_root):
        raise SafetyRefusal("S5", f"flow file {flow_file!r} is not under {box_code_root!r}")


def _api_parts(url: str) -> tuple[str, int | None, str]:
    u = urlparse(url.strip())
    return (u.hostname or "").lower(), u.port, u.path.rstrip("/")


def check_prefect_api_url(url: str | None, lane_url: str | None) -> None:
    """S6: PREFECT_API_URL is exactly the lane's ephemeral server, never the production :4200."""
    if not url or not lane_url:
        raise SafetyRefusal("S6", "fail closed: PREFECT_API_URL or the lane url is not set")
    host, port, path = _api_parts(url)
    lhost, lport, lpath = _api_parts(lane_url)
    if host not in LOOPBACK_HOSTS:
        raise SafetyRefusal("S6", f"PREFECT_API_URL host {host!r} is not loopback")
    if port == PROD_PREFECT_PORT or lport == PROD_PREFECT_PORT:
        raise SafetyRefusal("S6", f"port {PROD_PREFECT_PORT} is the production server")
    if lhost not in LOOPBACK_HOSTS or port is None or port != lport or path != lpath:
        raise SafetyRefusal("S6", f"PREFECT_API_URL {url!r} is not the lane server {lane_url!r}")


def check_notifications(smtp_host: str | None, smtp_auth: Any, recipients: Iterable[str]) -> None:
    """S7: SMTP is 127.0.0.1, unauthenticated, and every recipient is on a .invalid domain."""
    if (smtp_host or "").strip() != "127.0.0.1":
        raise SafetyRefusal("S7", f"smtp_host {smtp_host!r} is not 127.0.0.1")
    if smtp_auth is not False:
        raise SafetyRefusal("S7", "smtp_auth is not false")
    recs = list(recipients)
    if not recs:
        raise SafetyRefusal("S7", "fail closed: no recipients supplied")
    for r in recs:
        addr = str(r).strip().lower().rstrip(">")
        if "@" not in addr or not addr.rsplit("@", 1)[1].endswith(".invalid"):
            raise SafetyRefusal("S7", f"recipient {r!r} is not on a .invalid domain")


def _endpoint_is_local(endpoint: str) -> bool:
    ep = endpoint.strip()
    host = (urlparse(ep if "://" in ep else "//" + ep).hostname or "").lower()
    return host == "127.0.0.1" or host.endswith(".invalid")


def check_rclone_config(config_path: str | None, lane_rclone_conf: str | None, remote_type: str | None,
                        endpoint: str | None, smoke: bool = False, alias_remote: str | None = None,
                        lane_root: str | None = None) -> None:
    """S8: the lane's rclone config; a GCS remote needs a 127.0.0.1 / .invalid endpoint (not in the gated smoke)."""
    if not config_path or not lane_rclone_conf:
        raise SafetyRefusal("S8", "fail closed: rclone config path not supplied")
    if norm(config_path) != norm(lane_rclone_conf):
        raise SafetyRefusal("S8", f"rclone config {config_path!r} is not the lane's {lane_rclone_conf!r}")
    rtype = (remote_type or "").strip().lower()
    if rtype == "local":
        return
    if rtype == "alias":                       # the lane's absolute local bucket; checked against the lane when known
        if alias_remote is not None or lane_root is not None:
            if not alias_remote or not lane_root or not is_under(alias_remote, lane_root):
                raise SafetyRefusal("S8", f"alias remote {alias_remote!r} is not under the lane root {lane_root!r}")
        return
    if rtype != "google cloud storage":
        raise SafetyRefusal("S8", f"remote type {remote_type!r} is not local, alias or google cloud storage")
    if smoke:
        return
    if not endpoint or not _endpoint_is_local(endpoint):
        raise SafetyRefusal("S8", f"GCS remote endpoint {endpoint!r} is not 127.0.0.1 / .invalid")


def _is_raiser(fn: Any) -> bool:
    return getattr(fn, "livebox_raiser", False) is True


def _is_spy(fn: Any) -> bool:
    return getattr(fn, "livebox_spy", False) is True


def check_shutdown_raisers(shutdown_fn: Any, wol_fn: Any, lan_shutdown_task: Any, deep: bool | None = None) -> None:
    """S9: shutdown and WoL are the raisers (`livebox_raiser`), the flow's shutdown task is the spy (`livebox_spy`).

    deep (None = automatic: on when the three objects passed are the live attributes of core.shutdown / core.wol)
    also checks what the product code would really call: flow.shutdown_from_config (raiser), flow.ensure_server_online
    (spy or raiser), the `_send_magic_packet` that core.wol.ensure_server_online resolves in its own globals, and the
    `shutdown_server` that core.shutdown.shutdown_from_config resolves in its own globals (raisers)."""
    if not _is_raiser(shutdown_fn):
        raise SafetyRefusal("S9", "core.shutdown.shutdown_server is not the livebox raiser")
    if not _is_raiser(wol_fn):
        raise SafetyRefusal("S9", "the WoL send is not the livebox raiser")
    if not _is_spy(lan_shutdown_task):
        raise SafetyRefusal("S9", "flow.lan_shutdown_task is not the livebox spy")
    sd, wol, flow = (sys.modules.get(n) for n in ("core.shutdown", "core.wol", "flow"))
    if deep is None:
        deep = (sd is not None and wol is not None
                and getattr(sd, "shutdown_server", None) is shutdown_fn
                and getattr(wol, "_send_magic_packet", None) is wol_fn)
    if not deep:
        return
    if flow is not None:
        if not _is_raiser(getattr(flow, "shutdown_from_config", None)):
            raise SafetyRefusal("S9", "flow.shutdown_from_config is not the livebox raiser")
        wake = getattr(flow, "ensure_server_online", None)
        if not (_is_spy(wake) or _is_raiser(wake)):
            raise SafetyRefusal("S9", "flow.ensure_server_online is not a livebox spy or raiser")
    for mod, fn_name, global_name, what in (
            (wol, "ensure_server_online", "_send_magic_packet", "core.wol.ensure_server_online sends with"),
            (sd, "shutdown_from_config", "shutdown_server", "core.shutdown.shutdown_from_config calls")):
        if mod is None:
            continue
        g = getattr(getattr(mod, fn_name, None), "__globals__", None)
        if g is None or not _is_raiser(g.get(global_name)):
            raise SafetyRefusal("S9", f"{what} {global_name}, which is not the livebox raiser")


def check_rclone_seams(*temp_rclone_config_fns: Any) -> None:
    """S8 (seams): every module's `temp_rclone_config` is the lane seam (`livebox_seam`), never the real writer
    of a key file. Callers pass flow.temp_rclone_config, core.cloud_sync.temp_rclone_config and
    core.cloud_preflight.temp_rclone_config."""
    if not temp_rclone_config_fns:
        raise SafetyRefusal("S8", "fail closed: no temp_rclone_config supplied")
    for fn in temp_rclone_config_fns:
        if getattr(fn, "livebox_seam", False) is not True:
            raise SafetyRefusal("S8", "a temp_rclone_config is not the livebox local-remote seam")


# --------------------------------------------------------------------------- S10 - S15

def check_production_lock(lock_path: Any, exists: Callable[[str], bool] = os.path.exists) -> None:
    """S10 (pause): the production backup.lock exists. Only existence is tested; the file is never opened."""
    if lock_path is None:
        raise SafetyRefusal("S10", "fail closed: production lock path not supplied")
    if exists(str(lock_path)):
        raise SafetyPause("S10", f"production lock {str(lock_path)!r} exists: a production job is running")


def production_jobs_due(now: dt.datetime, crons: Mapping[str, str], window_minutes: int = GAP_MINUTES) -> list[tuple[str, dt.datetime]]:
    """(job, fire time) for each cron firing in [now, now + window]. Crons are read in now's own timezone."""
    from cronsim import CronSim

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (inject the clock)")
    end = now + dt.timedelta(minutes=window_minutes)
    due: list[tuple[str, dt.datetime]] = []
    for job, expr in crons.items():
        # start one second early so a job firing exactly now is counted
        nxt = next(iter(CronSim(expr, now - dt.timedelta(seconds=1))))
        if nxt <= end:
            due.append((job, nxt))
    return due


def check_production_schedule(now: dt.datetime, crons: Mapping[str, str], window_minutes: int = GAP_MINUTES) -> None:
    """S11 (pause): a production job starts within the window (default 30 min)."""
    due = production_jobs_due(now, crons, window_minutes)
    if due:
        job, when = min(due, key=lambda x: x[1])
        raise SafetyPause("S11", f"production {job} fires at {when.isoformat()} (within {window_minutes} min)")


def check_resources(free_bytes_c: int | None, lane_root_bytes: int | None) -> None:
    """S12: C: has >= 50 GB free and the lane root is <= 20 GB."""
    if free_bytes_c is None or lane_root_bytes is None:
        raise SafetyRefusal("S12", "fail closed: free space or lane size not supplied")
    if free_bytes_c < MIN_FREE_BYTES:
        raise SafetyRefusal("S12", f"C: free {free_bytes_c / GB:.1f} GB is below {MIN_FREE_BYTES // GB} GB")
    if lane_root_bytes > MAX_LANE_ROOT_BYTES:
        raise SafetyRefusal("S12", f"lane root {lane_root_bytes / GB:.1f} GB exceeds {MAX_LANE_ROOT_BYTES // GB} GB")


def check_lane_process_count(count: int | None) -> None:
    """S13: at most 3 lane processes at once."""
    if count is None:
        raise SafetyRefusal("S13", "fail closed: process count not supplied")
    if count > MAX_LANE_PROCESSES:
        raise SafetyRefusal("S13", f"{count} lane processes (max {MAX_LANE_PROCESSES})")


def check_production_slot(slot_state: Mapping[str, Any] | None) -> None:
    """S14: the production Prefect slot `aam-backup` is taken (active >= limit). Refuses; unreadable also refuses."""
    try:
        active, limit = int(slot_state["active"]), int(slot_state["limit"])  # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        raise SafetyRefusal("S14", f"production slot state unreadable: {slot_state!r}") from None
    if active >= limit:
        raise SafetyRefusal("S14", f"production slot aam-backup is taken ({active}/{limit})")


_PATH_TOKEN = re.compile(r"[A-Za-z]:\\[^\s\"'*?<>|]*|\\\\[^\s\"'*?<>|]+")


_FAKE_NAS_HOST = re.compile(r"(?:192\.0\.2\.\d{1,3}|[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.invalid)", re.IGNORECASE)


def _is_fake_nas_unc(p: str) -> bool:
    """H2: a UNC path whose host is one rig.set_nas allows (192.0.2.0/24 or *.invalid): it can reach no real machine."""
    if not p.startswith("\\\\"):
        return False
    host = p[2:].split("\\", 1)[0]
    if not _FAKE_NAS_HOST.fullmatch(host):
        return False
    if host[0].isdigit():
        return all(0 <= int(x) <= 255 for x in host.split("."))
    return True


def check_argv(argv: Sequence[str], lane_root: str) -> None:
    """S15: every filesystem path in a robocopy / rclone argv is under the lane root.
    argv[0] (the tool) is exempt; rclone `remote:path` tokens are not paths (S8 governs the remote)."""
    for tok in list(argv)[1:]:
        for p in _PATH_TOKEN.findall(str(tok)):
            if not is_under(p, lane_root) and not _is_fake_nas_unc(p):
                raise SafetyRefusal("S15", f"argv path {p!r} is outside the lane root {lane_root!r}")


# --------------------------------------------------------------------------- orchestration

@dataclass
class SafetyContext:
    """Facts for run_all. Anything left None makes its check refuse (fail closed)."""

    lane_root: str | None = None
    # S1
    expected_commit: str | None = None
    box_head: str | None = None
    porcelain: str | None = None
    # S2 / S3 / S4
    sandbox_paths: Mapping[str, str] | None = None
    production_roots: Iterable[str] | None = None
    fy_leaves: Iterable[str] | None = None
    production_leaves: Iterable[str] | None = None
    century: bool = False
    # S5
    config_path: str | None = None
    lane_config: str | None = None
    flow_file: str | None = None
    box_code_root: str = r"C:\AAMBackup"
    # S6
    prefect_api_url: str | None = None
    lane_api_url: str | None = None
    # S7
    smtp_host: str | None = None
    smtp_auth: Any = None
    recipients: Iterable[str] | None = None
    # S8
    rclone_config_path: str | None = None
    lane_rclone_conf: str | None = None
    remote_type: str | None = None
    remote_endpoint: str | None = None
    smoke: bool = False
    # S9
    shutdown_fn: Any = None
    wol_fn: Any = None
    lan_shutdown_task: Any = None
    # S10 / S11 / S14 (production facts, injected; nothing is read here)
    production_lock_path: Any = None
    exists: Callable[[str], bool] = os.path.exists
    now: Callable[[], dt.datetime] | None = None
    production_crons: Mapping[str, str] | None = None
    window_minutes: int = GAP_MINUTES
    slot_state: Mapping[str, Any] | None = None
    # S12 / S13
    free_bytes_c: int | None = None
    lane_root_bytes: int | None = None
    lane_process_count: int | None = None
    # S15
    argvs: Sequence[Sequence[str]] = field(default_factory=tuple)


def _need(v: Any, what: str, rule: str) -> Any:
    if v is None:
        raise SafetyRefusal(rule, f"fail closed: {what} not supplied")
    return v


def _checks(c: SafetyContext) -> dict[str, Callable[[], None]]:
    def s11() -> None:
        check_production_schedule(_need(c.now, "clock", "S11")(), _need(c.production_crons, "production crons", "S11"),
                                  c.window_minutes)

    def s15() -> None:
        root = _need(c.lane_root, "lane root", "S15")
        for argv in c.argvs:
            check_argv(argv, root)

    return {
        "S1": lambda: check_box_head(c.box_head, c.expected_commit, c.porcelain),
        "S2": lambda: check_sandbox_paths(_need(c.sandbox_paths, "sandbox paths", "S2"), _need(c.lane_root, "lane root", "S2")),
        "S3": lambda: check_not_production_paths(_need(c.sandbox_paths, "sandbox paths", "S3"),
                                                 _need(c.production_roots, "production roots", "S3")),
        "S4": lambda: check_fy_leaves(_need(c.fy_leaves, "fy leaves", "S4"), _need(c.production_leaves, "production leaves", "S4"),
                                      c.century),
        "S5": lambda: check_config_binding(c.config_path, c.lane_config, c.flow_file, c.box_code_root),
        "S6": lambda: check_prefect_api_url(c.prefect_api_url, c.lane_api_url),
        "S7": lambda: check_notifications(c.smtp_host, c.smtp_auth, _need(c.recipients, "recipients", "S7")),
        "S8": lambda: check_rclone_config(c.rclone_config_path, c.lane_rclone_conf, c.remote_type, c.remote_endpoint, c.smoke),
        "S9": lambda: check_shutdown_raisers(c.shutdown_fn, c.wol_fn, c.lan_shutdown_task),
        "S10": lambda: check_production_lock(c.production_lock_path, c.exists),
        "S11": s11,
        "S12": lambda: check_resources(c.free_bytes_c, c.lane_root_bytes),
        "S13": lambda: check_lane_process_count(c.lane_process_count),
        "S14": lambda: check_production_slot(c.slot_state),
        "S15": s15,
    }


def run_all(ctx: SafetyContext) -> list[Result]:
    """Run S1..S15 in order; return one Result each. An unexpected error counts as a refusal."""
    out: list[Result] = []
    for rule, fn in _checks(ctx).items():
        try:
            fn()
            out.append(Result(rule, "ok"))
        except SafetyPause as e:
            out.append(Result(rule, "pause", e.message))
        except SafetyRefusal as e:
            out.append(Result(rule, "refuse", e.message))
        except Exception as e:  # fail closed
            out.append(Result(rule, "refuse", f"check crashed: {type(e).__name__}: {e}"))
    return out


def raise_on_abort(results: Iterable[Result]) -> None:
    """Raise SafetyRefusal for the first refusal; pauses are returned to the caller via `pauses`."""
    for r in results:
        if r.status == "refuse":
            raise SafetyRefusal(r.rule, r.message)


def pauses(results: Iterable[Result]) -> list[Result]:
    return [r for r in results if r.status == "pause"]
