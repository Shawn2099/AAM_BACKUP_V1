r"""Livebox rig (plan LIVEBOX_TEST_PLAN_2026-11-24.md sections 1.3 - 1.7, work package L4a).

Two layers.

Low level (each piece usable and tested on its own, see tests/test_rig.py):
    rclone_config_text(variant)        local / blackhole / dns remote text (1.7)
    install_seams(lane_root, ...)      NAS wake and shutdown spies and raisers, archive recorder, local rclone
                                       remote, scheduled-time seam (1.3); context manager, idempotent uninstall
    install_google_date(...)           clock-proof seam at the HTTP edge (1.3, 1.7)
    LaneSmtp(evidence_dir)             loopback fake SMTP + mail_index.csv capture (1.6)
    assert_argv_in_lane(argv, root)    S15
    SimClock / MtimeNormaliser         time_machine traveller from an overlay dir, mtime normaliser (1.4)

High level:
    LaneRig.create(...).build()        lane folders, derived config, SMTP, seams, clock, Prefect harness
    with rig: rig.run_flow("backup", at=..., scheduled_at=..., mode="all")

Safety: nothing here touches production. The NAS wake and shutdown calls are spies, the real senders are
raisers that raise RuntimeError, no packet and no shutdown can leave this process. SMTP is loopback only.
The rclone remote is the local backend (or a loopback / .invalid endpoint with a dummy key).

Not imported from audit_lab (labconf imports pytest and replaces core.shutdown at import time): the few
helpers needed (local rclone config, /ZB stripping, the Google date seam) are adapted here.

Known limit (plan 1.3, UNVERIFIED items): the lane-private Prefect harness is `prefect_test_harness()` in the
SAME process; it needs a clean PREFECT_HOME (never ~/.prefect). `LaneRig.prefect_harness()` refuses otherwise.
"""
from __future__ import annotations

import contextlib
import copy
import csv
import datetime as dt
import email
import email.policy
import email.utils
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any, Callable

from tools.livebox import safety
from tools.livebox.derive_config import TEST_FY, derive_config
from tools.livebox.spike_clock import MtimeNormaliser

LIVEBOX_ROOT = Path(r"C:\AAMLivebox")
PRODUCTION_ROOTS = (r"C:\AAMBackup", r"C:\BackupAgent", "E:\\")
REMOTE = "aam_gcs"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HARNESS_ARTEFACT = "task_run_state"          # Prefect harness DB artefact: one retry (w12 _flow_call)
MARKER = ".AAM_TARGET_MOUNTED"

# callers (by function name) whose FlowRunContext.get() answer the scheduled-time seam replaces (plan 1.3)
SCHEDULE_CALLERS = frozenset({"_scheduled_time", "_scheduled_lateness_seconds", "_auto_scheduled_run"})


KILLABLE_IMAGES = frozenset({"robocopy.exe", "rclone.exe"})   # the only images kill_lane_processes may kill


class RigError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------------- rclone remotes
def rclone_config_text(variant: str, *, key_path: str = "dummy-key.json") -> str:
    """rclone config text for remote [aam_gcs]. Every variant keeps a dummy key: nothing can authenticate."""
    if variant == "local":
        return f"[{REMOTE}]\ntype = local\n"
    if variant in ("blackhole", "dns"):
        endpoint = "http://127.0.0.1:9" if variant == "blackhole" else "https://aam-livebox.invalid"
        return (f"[{REMOTE}]\ntype = google cloud storage\nendpoint = {endpoint}\n"
                f"service_account_file = {key_path}\nobject_acl =\nbucket_acl =\nbucket_policy_only = true\n")
    raise ValueError(f"unknown rclone variant {variant!r} (local, blackhole, dns)")


_FAKE_KEY_PEM: list = []   # one throwaway key per process


def _throwaway_pkcs8_pem() -> str:
    """A freshly generated, never-saved RSA-2048 PKCS8 key: test-only, authenticates nowhere."""
    if not _FAKE_KEY_PEM:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _FAKE_KEY_PEM.append(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode("ascii"))
    return _FAKE_KEY_PEM[0]


DUMMY_TOKEN_URI = {"blackhole": "http://127.0.0.1:9/token", "dns": "https://aam-livebox.invalid/token"}


def dummy_service_account_json(variant: str) -> str:
    """Well-formed fake service-account JSON so rclone gets past key parsing and fails on the NETWORK."""
    if variant not in DUMMY_TOKEN_URI:
        raise ValueError(f"unknown dummy key variant {variant!r} (blackhole, dns)")
    return json.dumps({
        "type": "service_account", "project_id": "aam-livebox-fake",
        "private_key_id": "0" * 40, "private_key": _throwaway_pkcs8_pem(),
        "client_email": "livebox-fake@aam-livebox.invalid", "client_id": "000000000000000000000",
        "token_uri": DUMMY_TOKEN_URI[variant],
    }, indent=2)


def alias_config_text(bucket_dir) -> str:
    """Local remote that does not depend on the working directory: an alias to the lane's absolute bucket folder."""
    return (f"[{REMOTE}]\n# an alias to an absolute folder, so no run depends on the cwd\n"
            f"type = alias\nremote = {Path(bucket_dir).resolve()}\n")


def assert_argv_in_lane(argv, lane_root) -> None:
    """S15: every filesystem path in an rclone argv is under the lane root (SafetyRefusal otherwise)."""
    safety.check_argv(list(argv), str(lane_root))


# ---------------------------------------------------------------------------------------------- patch stack
_MISSING = object()


class _Patches:
    """Records every attribute it replaces and restores all of them (reverse order, idempotent)."""

    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []

    def setattr(self, obj: Any, name: str, value: Any) -> Any:
        """Replace an EXISTING attribute (RigError if it is missing: a seam on a name the product does not have
        would silently test nothing). The attribute is restored by restore()."""
        old = getattr(obj, name, _MISSING)
        if old is _MISSING:
            raise RigError(f"seam target {getattr(obj, '__name__', obj)!r} has no attribute {name!r}; refusing to create it")
        setattr(obj, name, value)

        def undo(obj=obj, name=name, old=old):
            setattr(obj, name, old)

        self._undo.append(undo)
        return value

    def callback(self, fn: Callable[[], None]) -> None:
        self._undo.append(fn)

    def restore(self) -> None:
        while self._undo:
            fn = self._undo.pop()
            try:
                fn()
            except Exception:  # noqa: BLE001 - keep restoring the rest
                pass


def _raiser(what: str) -> Callable:
    def _raise(*_a, **_k):
        raise RuntimeError(f"LIVEBOX: {what} called - refusing (no wake packet or shutdown ever leaves the rig)")

    _raise.livebox_raiser = True
    _raise.__name__ = f"livebox_raiser_{what}"
    return _raise


class _ShutdownSpy:
    """Replaces flow.lan_shutdown_task. Task-like enough for `.fn` and `.with_options(...)`."""

    livebox_spy = True

    def __init__(self, calls: list):
        self.calls = calls

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return {"simulated": True}

    fn = __call__

    def with_options(self, **_k):
        return self


# ---------------------------------------------------------------------------------------------- seams
class Seams:
    """The installed seams. Attributes are plain lists (one entry per call). Context manager; uninstall() is idempotent."""

    def __init__(self, lane_root: str, bucket_dir: str | None, remote_config_text: str | None,
                 strip_zb: bool = False, remote_variant: str | None = None):
        self.lane_root = Path(lane_root)
        self.bucket_dir = Path(bucket_dir) if bucket_dir else None
        self.wake_calls: list = []
        self.shutdown_calls: list = []
        self.archive_calls: list = []
        self.archive_result = True
        self.wake_result = True
        self.wake_error: BaseException | None = None      # set -> the wake spy raises it (WoL failed / NAS not ready)
        self.scheduled_at: dt.datetime | None = None      # None = manual press (auto_scheduled False)
        self.lane_root.mkdir(parents=True, exist_ok=True)
        self.rclone_conf = self.lane_root / "rclone_local.conf"
        self._strip_zb = strip_zb
        self._patches = _Patches()
        self._installed = False
        if remote_config_text is None and (remote_variant or "local") == "local" and self.bucket_dir is not None:
            remote_config_text = alias_config_text(self.bucket_dir)   # absolute: independent of the cwd
        self.set_remote_text(remote_config_text if remote_config_text is not None
                             else rclone_config_text(remote_variant or "local"))

    # -- remote variants (1.7): the patched temp_rclone_config reads the file at every call
    def set_remote_text(self, text: str) -> None:
        self.rclone_conf.write_text(text, encoding="utf-8")

    def set_remote_variant(self, variant: str, key_path: str | None = None) -> None:
        if variant == "local" and self.bucket_dir is not None:
            return self.set_remote_text(alias_config_text(self.bucket_dir))
        self.set_remote_text(rclone_config_text(variant, key_path=key_path or str(self.lane_root / "dummy-key.json")))

    # -- scheduled time seam
    def _fake_flow_run_context_get(self, original: Callable):
        seams = self

        def get(*a, **k):
            try:
                caller = sys._getframe(1).f_code.co_name
            except ValueError:  # pragma: no cover
                caller = ""
            if caller in SCHEDULE_CALLERS:
                at = seams.scheduled_at
                if at is None:
                    return None
                return SimpleNamespace(flow_run=SimpleNamespace(auto_scheduled=True, expected_start_time=at))
            return original(*a, **k)

        return get

    # -- install / uninstall
    def install(self) -> "Seams":
        if self._installed:
            return self
        import core.cloud_preflight as cpre
        import core.cloud_sync as csync
        import core.fy_rollover as fy_rollover
        import core.shutdown as sd
        import core.wol as wol
        import flow

        p = self._patches
        seams = self
        try:
            def wake_spy(*args, **kwargs):
                seams.wake_calls.append({"args": args, "kwargs": kwargs})
                if seams.wake_error is not None:
                    raise seams.wake_error
                return seams.wake_result

            def wake_status_spy(*args, **kwargs):
                seams.wake_calls.append({"args": args, "kwargs": kwargs})
                if seams.wake_error is not None:
                    raise seams.wake_error
                return "woken" if seams.wake_result is True else seams.wake_result

            wake_spy.livebox_spy = True
            wake_status_spy.livebox_spy = True
            p.setattr(flow, "ensure_server_online", wake_spy)
            p.setattr(flow, "ensure_server_online_status", wake_status_spy)
            p.setattr(flow, "lan_shutdown_task", _ShutdownSpy(self.shutdown_calls))
            p.setattr(wol, "_send_magic_packet", _raiser("wol_send_magic_packet"))
            p.setattr(wol, "wol_send", _raiser("wol_send"))
            p.setattr(sd, "shutdown_server", _raiser("shutdown_server"))
            p.setattr(flow, "shutdown_from_config", _raiser("shutdown_from_config"))

            def archive(*args, **kwargs):
                names = ("bucket", "old_fy", "gcs_key_path")
                rec = dict(zip(names, args))
                rec.update(kwargs)
                seams.archive_calls.append(rec)
                return seams.archive_result

            p.setattr(fy_rollover, "run_archive_transition", archive)

            conf = self.rclone_conf

            @contextlib.contextmanager
            def local_cfg(*_a, **_k):
                yield str(conf)

            local_cfg.livebox_seam = True

            for mod in (flow, csync, cpre):
                p.setattr(mod, "temp_rclone_config", local_cfg)

            if self.bucket_dir is not None:
                self.bucket_dir.mkdir(parents=True, exist_ok=True)

            self._patch_flow_run_context(p)
            self._installed = True
        except BaseException:
            p.restore()
            raise
        return self

    def _patch_flow_run_context(self, p: _Patches) -> None:
        try:
            from prefect.context import FlowRunContext
        except Exception:  # noqa: BLE001 - no prefect: nothing to seam
            return
        original = FlowRunContext.get          # bound classmethod
        fake = self._fake_flow_run_context_get(original)
        saved = FlowRunContext.__dict__.get("get", _MISSING)
        type.__setattr__(FlowRunContext, "get", staticmethod(fake))

        def undo():
            if saved is _MISSING:
                with contextlib.suppress(AttributeError):
                    type.__delattr__(FlowRunContext, "get")
            else:
                type.__setattr__(FlowRunContext, "get", saved)

        p.callback(undo)

    def uninstall(self) -> None:
        self._patches.restore()
        self._installed = False

    def __enter__(self) -> "Seams":
        return self.install()

    def __exit__(self, *exc) -> None:
        self.uninstall()


def install_seams(lane_root, *, bucket_dir=None, remote_config_text: str | None = None,
                  strip_zb: bool = False) -> Seams:
    """Install every seam of plan 1.3 and return the (already installed) Seams object."""
    return Seams(str(lane_root), str(bucket_dir) if bucket_dir else None, remote_config_text,
                 strip_zb=strip_zb).install()


# ---------------------------------------------------------------------------------------------- Google clock proof
class _Handle:
    def __init__(self, patches: _Patches):
        self._patches = patches

    def uninstall(self) -> None:
        self._patches.restore()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.uninstall()


def install_google_date(skew_seconds: float = 0.0, *, raise_oserror: bool = False) -> _Handle:
    """core.health.check_clock sees a Google Date header from the (simulated) clock, skewed by `skew_seconds`;
    with raise_oserror the connection fails (no internet). The production check logic stays real."""
    import core.health as health

    class _Resp:
        def getheader(self, _name):
            return email.utils.formatdate(time.time() + skew_seconds, usegmt=True)

    class _GoogleDateConn:
        debuglevel = 0                      # attributes http.client classes expose (urllib reads them on the class)
        default_port = 443
        auto_open = 1

        def __init__(self, _host, timeout=None, **_k):
            if raise_oserror:
                raise OSError("LIVEBOX: no internet (simulated)")
            self.timeout = timeout

        def request(self, *_a, **_k):
            pass

        def getresponse(self):
            return _Resp()

        def close(self):
            pass

    p = _Patches()
    p.setattr(health.http.client, "HTTPSConnection", _GoogleDateConn)
    return _Handle(p)


# ---------------------------------------------------------------------------------------------- fake SMTP
class LaneSmtp:
    """Loopback fake SMTP server (test_support.smtp_server) with mail capture: drain() writes .eml files and
    mail_index.csv (sim_time,real_time,rcpts,subject,from,x_headers)."""

    CSV_FIELDS = ["sim_time", "real_time", "rcpts", "subject", "from", "x_headers"]

    def __init__(self, evidence_dir, *, fail_with: str | None = None):
        self.evidence_dir = Path(evidence_dir)
        self.host = "127.0.0.1"
        self.port: int | None = None
        self._fail_with = fail_with
        self._srv = None
        self._thread: threading.Thread | None = None
        self.messages: list[dict] = []
        self._drained = 0
        self._seq = 0
        self.mail_attempts = 0

    # modes: normal / fail_with / stopped
    @property
    def fail_with(self) -> str | None:
        return self._fail_with

    @fail_with.setter
    def fail_with(self, value: str | None) -> None:
        self._fail_with = value
        if self._srv is not None:
            self._srv.fail_with = value

    @property
    def running(self) -> bool:
        return self._srv is not None

    def start(self) -> "LaneSmtp":
        if self._srv is not None:
            return self
        from test_support.smtp_server import _Handler, _Server

        srv = _Server((self.host, self.port or 0), _Handler)
        srv.messages = self.messages
        srv.mail_attempts = 0
        srv.fail_with = self._fail_with
        srv.size_limit = None
        srv.reject_attachments = None
        srv.hangup_over = None
        srv.refuse_rcpt = ()
        srv.hangups = 0
        self.port = srv.server_address[1]
        self._srv = srv
        self._thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        srv, self._srv = self._srv, None
        if srv is None:
            return
        self.mail_attempts += srv.mail_attempts
        srv.shutdown()
        srv.server_close()

    restart = start

    def __enter__(self) -> "LaneSmtp":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def drain(self, sim_time: str) -> int:
        batch = self.messages[self._drained:]
        if not batch:
            return 0
        self._drained += len(batch)
        mail_dir = self.evidence_dir / "mail"
        mail_dir.mkdir(parents=True, exist_ok=True)
        index = self.evidence_dir / "mail_index.csv"
        new_file = not index.exists()
        safe = re.sub(r"[^0-9A-Za-z.-]+", "-", str(sim_time)).strip("-")
        with open(index, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=self.CSV_FIELDS)
            if new_file:
                w.writeheader()
            for m in batch:
                self._seq += 1
                (mail_dir / f"{self._seq:04d}_{safe}.eml").write_bytes(m["data"].encode("utf-8", "replace"))
                parsed = email.message_from_string(m["data"], policy=email.policy.default)
                rcpts = [x.group(1) for r in m["rcpts"] if (x := re.search(r"<([^>]*)>", r))]
                xh = {k: str(v) for k, v in parsed.items() if k.lower().startswith("x-")}
                w.writerow({"sim_time": sim_time, "real_time": dt.datetime.now(dt.timezone.utc).isoformat(),
                            "rcpts": ";".join(rcpts), "subject": str(parsed.get("Subject", "")),
                            "from": str(parsed.get("From", "")), "x_headers": json.dumps(xh) if xh else ""})
        return len(batch)


# ---------------------------------------------------------------------------------------------- simulated clock
def load_time_machine(overlay: str | os.PathLike | None = None):
    """Import time_machine, from the overlay dir first (plan 1.4: no install into the box venv)."""
    if overlay:
        o = str(overlay)
        if o not in sys.path:
            sys.path.insert(0, o)
    try:
        import time_machine
    except ImportError as e:  # pragma: no cover
        raise RigError(f"time_machine not importable (overlay={overlay!r}): {e}") from e
    return time_machine


class SimClock:
    """Process-wide simulated clock: one traveller, move_to(instant) before every event (clock ticks inside)."""

    def __init__(self, overlay=None):
        self._tm = load_time_machine(overlay)
        self._traveller = None
        self._coords = None
        self.now: dt.datetime | None = None

    def start(self, instant: dt.datetime) -> None:
        if self._traveller is not None:
            return self.move_to(instant)
        self._patch_loguru_tz()
        self._traveller = self._tm.travel(instant, tick=True)
        self._coords = self._traveller.start()
        self.now = instant

    def _patch_loguru_tz(self) -> None:
        """Beyond 2038 some Windows CRTs (Server 2016) return a garbage tm_gmtoff / local offset, so loguru's
        aware_now() raises "offset must be a timedelta strictly between -24h and 24h". Pin loguru's tz lookup to
        the fixed offset the machine has right now (before the clock moves); the harness has no DST to simulate."""
        if getattr(self, "_loguru_undo", None) is not None:
            return
        try:
            import loguru._datetime as ld
        except Exception:  # noqa: BLE001
            self._loguru_undo = lambda: None
            return
        try:
            off = dt.datetime.now().astimezone().utcoffset()
            if off is None or abs(off) >= dt.timedelta(hours=24):
                raise ValueError(off)
        except Exception:  # noqa: BLE001
            off = dt.timedelta(hours=5, minutes=30)
        tz = dt.timezone(off)
        saved: dict = {}
        for name in ("_get_tzinfo", "_fallback_tzinfo"):  # loguru 0.8.x only
            if hasattr(ld, name):
                saved[name] = getattr(ld, name)
                setattr(ld, name, lambda _ts, _tz=tz: _tz)
        if hasattr(ld, "localtime"):  # loguru 0.7.x reads tm_gmtoff/tm_zone from localtime() in aware_now()
            real_localtime = ld.localtime
            zone = "IST" if off == dt.timedelta(hours=5, minutes=30) else "LOC"
            fixed_off = int(off.total_seconds())

            class _FixedLocal:
                tm_gmtoff = fixed_off
                tm_zone = zone

                def __init__(self, st):
                    self._st = st

                def __getattr__(self, attr):
                    return getattr(self._st, attr)

                def __getitem__(self, i):
                    return self._st[i]

            saved["localtime"] = real_localtime
            ld.localtime = lambda *a, **k: _FixedLocal(real_localtime(*a, **k))

        def undo():
            for k, v in saved.items():
                setattr(ld, k, v)

        self._loguru_undo = undo

    def move_to(self, instant: dt.datetime) -> None:
        if self._traveller is None:
            return self.start(instant)
        self._coords.move_to(instant, tick=True)
        self.now = instant

    def stop(self) -> None:
        t, self._traveller, self._coords = self._traveller, None, None
        if t is not None:
            t.stop()
        undo, self._loguru_undo = getattr(self, "_loguru_undo", None), None
        if undo is not None:
            undo()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


# ---------------------------------------------------------------------------------------------- the lane rig
def dev_prod_raw() -> dict:
    """A valid synthetic 'production' config dict for dev-PC runs (never the real config.yaml)."""
    from test_support import ui_login

    return {
        "firm_name": "LIVEBOX",
        "paths": {"source_drive": r"E:\FY26-27", "lan_destination": r"\\PRODNAS01\backup$\FY26-27",
                  "runtime_dir": r"C:\BackupAgent", "gcs_key_path": r"C:\BackupAgent\key.json"},
        "lan": {"enabled": True, "retry_count": 1, "retry_wait_seconds": 1, "subprocess_timeout_seconds": 3600,
                "shutdown_after_backup": True, "shutdown_on_all_retries_exhausted": False, "max_attempts": 1,
                "retry_delay_seconds": 60},
        "wol": {"enabled": True, "mac_address": "AA:BB:CC:DD:EE:FF", "server_ip": "10.0.0.5",
                "broadcast_address": "", "wake_timeout_seconds": 60, "ping_interval_seconds": 5,
                "stability_wait_seconds": 0, "wake_retry_count": 1, "wake_retry_interval_seconds": 2},
        "cloud": {"enabled": True, "bucket": "prod-bucket", "project_number": "000000000000",
                  "location": "us-central1", "storage_class": "STANDARD", "bandwidth_limit": "1000M",
                  "retry_count": 1, "subprocess_timeout_seconds": 3600, "max_attempts": 1,
                  "retry_delay_seconds": 60, "verify_timeout_seconds": 120, "preflight_timeout_seconds": 60,
                  "diff_timeout_seconds": 60, "manifest_timeout_seconds": 60, "cloud_size_timeout_seconds": 30,
                  "transfers": 2, "checkers": 2, "buffer_size": "16M", "max_duration_seconds": 0},
        "schedule": {"cloud_cron": "0 18 * * *", "lan_cron": "0 1 * * *", "weekly_cron": "0 8 * * MON",
                     "monthly_cron": "30 8 1 * *", "rollover_cron": "0 6 * * *", "audit_cron": "0 10 * * SUN",
                     "timezone": "Asia/Kolkata"},
        "dashboard": {**ui_login.DASHBOARD, "bind_address": "127.0.0.1", "port": 8080},
        "notifications": {"smtp_host": "", "smtp_port": 587, "smtp_username": "", "smtp_password": "",
                          "sender": "", "recipients": [], "send_on_failure": True, "weekly_enabled": True,
                          "monthly_enabled": True},
        "maintenance": {"db_retention_days": 90, "log_retention_days": 30, "sqlite_busy_timeout_ms": 5000,
                        "sqlite_vacuum_freelist_threshold": 10000, "sqlite_synchronous": "normal",
                        "concurrency_wait_seconds": 30},
        "health": {"max_clock_skew_seconds": 600, "clock_check_timeout_seconds": 5, "min_free_source_gb": 1,
                   "rollover_auth_timeout_seconds": 30, "rollover_archive_timeout_seconds": 60},
        "guards": {"forbid_local_destination": True},
        "rollover": {"overlap_enabled": True},
    }


@dataclass
class FlowOutcome:
    name: str
    at: dt.datetime | None
    value: Any = None
    exc: BaseException | None = None
    real_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exc is None


class LaneRig:
    """One lane: folders, derived config, fake SMTP, seams, optional simulated clock, Prefect test harness."""

    FLOWS = {"backup": "backup", "rollover": "rollover_check_flow", "audit": "integrity_audit_flow",
             "weekly": "weekly_report_flow", "monthly": "monthly_report_flow"}

    def __init__(self, lane_dir: Path, *, prod_raw: dict | None = None, retention_days: int | None = None,
                 has_backup_privilege: bool | None = False, overlay=None, use_clock: bool = False,
                 test_fy: str = TEST_FY):
        self.test_fy = test_fy
        self.lane_dir = Path(lane_dir)
        for prod in PRODUCTION_ROOTS:
            if safety.is_under(self.lane_dir, prod):
                raise RigError(f"lane dir {self.lane_dir} is under a production root {prod}")
        self.stamp, self.lane = self.lane_dir.parent.name, self.lane_dir.name
        self.prod_raw = copy.deepcopy(prod_raw) if prod_raw is not None else dev_prod_raw()
        self.retention_days = retention_days
        self.has_backup_privilege = has_backup_privilege
        self.overlay = overlay
        self.use_clock = use_clock
        self.config_path = self.lane_dir / "config.livebox.yaml"
        self.evidence_dir = self.lane_dir / "evidence"
        self.smtp: LaneSmtp | None = None
        self.seams: Seams | None = None
        self.clock: SimClock | None = None
        self.google: _Handle | None = None
        self.normalisers: list[MtimeNormaliser] = []
        self._stack: contextlib.ExitStack | None = None
        self._built = False
        self.config: dict = {}
        self.inputs = None                      # evidence.InputsLog, created by build() when that module exists
        self.prefect_api_url: str | None = None # the lane harness's own api url (set by prefect_harness; S6)
        self.kill_armed: str | None = None
        self._orig_lan_dest: str | None = None
        self._denied: dict[str, Path] = {}      # "src" / "config" -> path with our icacls deny in place

    @classmethod
    def create(cls, stamp: str, lane: str, *, root: str | os.PathLike | None = None, **kw) -> "LaneRig":
        return cls(Path(root or LIVEBOX_ROOT) / stamp / lane, **kw)

    # -- paths
    @property
    def source_dir(self) -> Path:
        return self.lane_dir / "src" / self.test_fy

    @property
    def nas_dir(self) -> Path:
        return self.lane_dir / "nas" / "backup" / self.test_fy

    @property
    def runtime_dir(self) -> Path:
        return self.lane_dir / "runtime"

    @property
    def bucket_root(self) -> Path:
        return self.lane_dir / "bucket"

    @property
    def bucket_name(self) -> str:
        return f"aam-livebox-local-{self.stamp}"

    def bucket_prefix(self, fy: str | None = None) -> Path:
        fy = fy or self.test_fy
        return self.bucket_root / self.bucket_name / fy

    def ensure_prefix(self, fy: str) -> Path:
        """GCS lists an empty prefix as []; the local backend fails on a missing folder (lab K8): create it."""
        p = self.bucket_prefix(fy)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # -- build
    def build(self) -> "LaneRig":
        if self._built:
            return self
        for d in (self.source_dir, self.nas_dir, self.runtime_dir, self.bucket_root / self.bucket_name,
                  self.lane_dir / "tmp", self.lane_dir / "prefect_home", self.evidence_dir):
            d.mkdir(parents=True, exist_ok=True)
        (self.nas_dir / MARKER).touch()
        (self.lane_dir / safety.LANE_MARKER).write_text("livebox lane folder (made by LaneRig.build)", encoding="utf-8")
        self.ensure_prefix(self.test_fy)
        (self.runtime_dir / "dummy-key.json").write_text(
            dummy_service_account_json("blackhole"), encoding="utf-8")
        self.smtp = LaneSmtp(self.evidence_dir).start()
        self._write_config()
        self._make_inputs()
        self._write_config_snapshot()
        self._built = True
        return self

    def _write_config(self) -> None:
        import yaml
        from models.config import AppConfig

        raw = derive_config(self.prod_raw, self.lane_dir, smtp_port=self.smtp.port,
                            has_backup_privilege=self.has_backup_privilege, retention_days=self.retention_days,
                            test_fy=self.test_fy)
        AppConfig(**raw)                                   # re-validate with the real model
        with self.config_path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False, allow_unicode=True)
        self.config = raw

    def _write_config_snapshot(self) -> None:
        """config_snapshot.json {"sha256", "config"} (secrets redacted) in the evidence dir; never breaks a build."""
        try:
            from tools.livebox import evidence

            red, digest = evidence.config_snapshot(self.config)
            (self.evidence_dir / "config_snapshot.json").write_text(
                json.dumps({"sha256": digest, "config": red}, indent=1, default=str), encoding="utf-8")
        except Exception:  # noqa: BLE001 - evidence must never break a run
            pass

    def _make_inputs(self) -> None:
        """inputs.jsonl log (tools.livebox.evidence); absent module or class: no recording, nothing else changes."""
        try:
            from tools.livebox import evidence
            cls = getattr(evidence, "InputsLog", None)
        except Exception:  # noqa: BLE001 - evidence is optional here
            return
        if cls is None or self.inputs is not None:
            return
        self.inputs = cls(self.evidence_dir / "inputs.jsonl",
                          lambda: self.clock.now if self.clock is not None else None)

    def _record(self, kind: str, name: str, args=None, **extra) -> None:
        if self.inputs is not None:
            try:
                self.inputs.record(kind, name, args, **extra)
            except Exception:  # noqa: BLE001 - evidence must never break a run
                pass

    def _save_config(self, mutate: Callable[[dict], None], *, validate: bool = True) -> None:
        """Apply `mutate` to a copy of the lane config, validate with the real model, then write the YAML."""
        import yaml
        from models.config import AppConfig

        new = None
        if self.config_path.is_file():                     # never work from the stale in-memory copy
            loaded = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                new = loaded
        if new is None:
            new = copy.deepcopy(self.config)
        mutate(new)
        if validate:
            AppConfig(**new)
        with self.config_path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(new, fh, sort_keys=False, allow_unicode=True)
        self.config = new

    def load_config(self):
        from models.config import load_config

        return load_config(str(self.config_path))

    # -- install / remove
    def install(self) -> "LaneRig":
        if self._stack is not None:
            return self
        self.build()
        stack = contextlib.ExitStack()
        try:
            self.seams = install_seams(self.lane_dir, bucket_dir=self.bucket_root,
                                       strip_zb=not self.has_backup_privilege)
            stack.callback(self.seams.uninstall)
            self.google = install_google_date()
            stack.callback(self.google.uninstall)
            if self.use_clock:
                self.clock = SimClock(self.overlay)
                stack.callback(self.clock.stop)
            self.normalisers = [MtimeNormaliser(self.runtime_dir / "run_logs"),
                                MtimeNormaliser(self.runtime_dir / "logs")]
        except BaseException:
            stack.close()
            raise
        self._stack = stack
        return self

    def revert_denies(self) -> None:
        """Strip every deny this rig applied (src and config); idempotent."""
        for key in list(self._denied):
            with contextlib.suppress(Exception):
                self._icacls_deny(key, False)
        self._denied.clear()

    def remove(self) -> None:
        self.revert_denies()
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()
        if self.smtp is not None:
            self.smtp.stop()

    def __enter__(self) -> "LaneRig":
        return self.install()

    def __exit__(self, *exc) -> None:
        self.remove()

    # -- scenario knobs
    def set_clock_proof(self, mode: str = "agree", skew_seconds: float = 0.0) -> None:
        """agree | skew | nointernet (Google Date seam)."""
        self._record("clock_proof", "set_clock_proof", [mode, skew_seconds])
        if self.google is not None:
            self.google.uninstall()
        self.google = install_google_date(skew_seconds if mode == "skew" else 0.0,
                                          raise_oserror=(mode == "nointernet"))

    def set_cloud(self, variant: str) -> None:
        """local | blackhole | dns (rclone remote) | gone (bucket folder renamed away: remote path gone)."""
        self._record("knob", "set_cloud", [variant])
        gone = self.bucket_root / (self.bucket_name + ".gone")
        live = self.bucket_root / self.bucket_name
        if variant == "gone":
            if live.exists():
                live.rename(gone)
            return
        if gone.exists() and not live.exists():
            gone.rename(live)
        if variant in DUMMY_TOKEN_URI:
            (self.runtime_dir / "dummy-key.json").write_text(dummy_service_account_json(variant), encoding="utf-8")
        self.seams.set_remote_variant(variant, str(self.runtime_dir / "dummy-key.json"))

    def set_wake_fail(self, flag: bool) -> None:
        """True: the NAS wake step raises core.wol.WolTimeout (WoL sent, NAS never became ready); False: wake works."""
        self._record("knob", "set_wake_fail", [bool(flag)])
        if self.seams is None:
            raise RigError("rig is not installed (no wake seam)")
        if flag:
            from core.wol import WolTimeout

            self.seams.wake_error = WolTimeout("livebox: NAS not ready after wake (injected)")
        else:
            self.seams.wake_error = None

    def set_mail(self, mode: str, reply: str = "451 try later") -> None:
        """normal | fail | stopped."""
        self._record("knob", "set_mail", [mode, reply])
        if mode == "stopped":
            self.smtp.stop()
            return
        self.smtp.start()
        self.smtp.fail_with = reply if mode == "fail" else None


    # ------------------------------------------------------------------------------------------ lane-local knobs
    # Every knob works only inside self.lane_dir and is reversible; nothing outside the lane is ever touched.
    def _lane_path(self, p) -> Path:
        """Resolve p (absolute, or relative to the lane dir); RigError unless the result (junctions followed)
        is inside the lane dir."""
        lane = Path(os.path.realpath(self.lane_dir))
        cand = Path(p)
        if not cand.is_absolute():
            cand = self.lane_dir / cand
        real = Path(os.path.realpath(cand))
        if not safety.is_under(real, lane):
            raise RigError(f"{p!r} resolves to {real}, outside the lane dir {lane}")
        return real

    @staticmethod
    def _plain_name(name: str, what: str) -> str:
        if (not isinstance(name, str) or name in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9_.\- ]+", name)):
            raise RigError(f"{what} {name!r} must be a plain folder name (no separators, no '..')")
        return name

    def _rewrite(self, mutate: Callable[[dict], None], *, validate: bool = True) -> None:
        self._save_config(mutate, validate=validate)

    @staticmethod
    def _check_nas_host(host) -> str:
        """Only TEST-NET-1 (192.0.2.0/24) addresses and names ending in .invalid are allowed as the fake NAS."""
        h = host if isinstance(host, str) else ""
        m = re.fullmatch(r"192\.0\.2\.(\d{1,3})", h)
        if m and int(m.group(1)) <= 255:
            return h
        if re.fullmatch(r"[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.invalid", h, re.IGNORECASE):
            return h
        raise RigError(f"bad NAS host {host!r}: only 192.0.2.0/24 or *.invalid is allowed")

    def set_nas(self, host: str | None) -> None:
        """Point the HOST part of paths.lan_destination at host (None restores the lane's original host).
        The rest of the UNC path comes from the config on disk, so a product FY switch survives."""
        self._record("knob", "set_nas", [host])
        if host is not None:
            self._check_nas_host(host)
        if self._orig_lan_dest is None:
            self._orig_lan_dest = self.config["paths"]["lan_destination"]
        new_host = self._orig_lan_dest.lstrip("\\").split("\\", 1)[0] if host is None else host

        def mutate(c: dict) -> None:
            rest = c["paths"]["lan_destination"].lstrip("\\").split("\\", 1)[1]
            c["paths"]["lan_destination"] = "\\\\" + new_host + "\\" + rest

        self._rewrite(mutate, validate=False)      # only the (checked) host changes; a half-switched FY on disk is not ours

    def make_junction(self, name: str, target) -> Path:
        """Junction lane\\src\\<name> -> target (relative to the lane dir, or absolute inside it)."""
        self._record("knob", "make_junction", [name, str(target)])
        self._plain_name(name, "junction name")
        link = self._lane_path("src") / name
        tgt = self._lane_path(target)
        if os.path.lexists(link):
            if link.is_dir() and not os.path.islink(link) and not os.path.isjunction(link) and not any(link.iterdir()):
                link.rmdir()                          # an EMPTY real folder (the untouched source year) is replaced
            else:
                raise RigError(f"{link} already exists and is not an empty folder")
        tgt.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tgt)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RigError(f"mklink /J failed: {r.stdout.strip()} {r.stderr.strip()}")
        return link

    def delete_source_folder(self, fy: str) -> None:
        """Remove lane\\src\\<fy>. A junction is unlinked only: its target survives."""
        self._record("knob", "delete_source_folder", [fy])
        self._plain_name(fy, "FY folder")
        d = self._lane_path("src") / fy
        if os.path.isjunction(d):
            os.rmdir(d)
        elif os.path.islink(d):
            os.unlink(d)
        elif d.is_dir():
            shutil.rmtree(d)

    # -- icacls (deny + remove exactly that deny; only inside the lane)
    def _acl_user(self) -> str:
        user = os.environ.get("USERNAME") or ""
        if not user:
            raise RigError("cannot determine the current user for icacls")
        dom = os.environ.get("USERDOMAIN")
        return f"{dom}\\{user}" if dom else user

    def _icacls_deny(self, key: str, flag: bool) -> None:
        if key == "src":
            path, rights = self._lane_path("src"), "(AD,WD)"
        else:
            path, rights = self._lane_path(self.config_path), "(WD,AD,WA,WEA)"
        user = self._acl_user()
        if flag:
            cmd = ["icacls", str(path), "/deny", f"{user}:{rights}"]
        else:
            cmd = ["icacls", str(path), "/remove:d", user]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RigError(f"icacls failed ({' '.join(cmd[1:3])}): {r.stdout.strip()} {r.stderr.strip()}")
        if flag:
            self._denied[key] = path
        else:
            self._denied.pop(key, None)

    def deny_source_parent_write(self, flag: bool) -> None:
        """Deny create-file / create-folder on lane\\src for the current user (False removes that deny)."""
        self._record("knob", "deny_source_parent_write", [flag])
        self._icacls_deny("src", bool(flag))

    def deny_config_write(self, flag: bool) -> None:
        """Deny write + delete (not read) on the lane config file (False removes that deny)."""
        self._record("knob", "deny_config_write", [flag])
        self._icacls_deny("config", bool(flag))

    def fail_archive(self, flag: bool) -> None:
        self._record("knob", "fail_archive", [flag])
        if self.seams is None:
            raise RigError("rig is not installed (no archive seam)")
        self.seams.archive_result = not flag

    # -- process kills
    def kill_child_after(self, step: str) -> None:
        self._record("knob", "kill_child_after", [step])
        self.kill_armed = step

    def fire_step(self, step: str) -> list[int]:
        """If armed for `step`: kill the lane processes once and disarm; otherwise do nothing."""
        if self.kill_armed is None or self.kill_armed != step:
            return []
        self.kill_armed = None
        return self.kill_lane_processes()

    def kill_lane_processes(self) -> list[int]:
        """taskkill /T /F of processes whose command line contains the lane dir; never this process or its parents."""
        import psutil

        self._record("knob", "kill_lane_processes", [])
        lane = str(self.lane_dir).lower()
        pat = re.compile(re.escape(lane) + r"(?![\w\-])")
        me = psutil.Process()
        protected = {me.pid} | {p.pid for p in me.parents()}
        pids: list[int] = []
        def on_lane(proc) -> bool:
            info = getattr(proc, "info", None)
            parts = (info or {}).get("cmdline") if info is not None else proc.cmdline()
            cmd = " ".join(parts or []).lower().replace("/", "\\")
            return bool(pat.search(cmd))

        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if (p.info.get("name") or "").lower() not in KILLABLE_IMAGES or p.info["pid"] in protected:
                    continue
                if not on_lane(p):
                    parent = p.parent()          # a child of a lane process is also a lane process
                    if parent is None or not on_lane(parent):
                        continue
                pids.append(p.info["pid"])
            except (psutil.Error, OSError):
                continue
        killed: list[int] = []
        for pid in pids:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, text=True)
            for _ in range(30):
                if not psutil.pid_exists(pid):
                    break
                time.sleep(0.1)
            if not psutil.pid_exists(pid):
                killed.append(pid)        # gone (an earlier /T may already have taken it down with its parent)
        return killed

    def tamper_nas_file(self, fy: str) -> Path:
        """Flip bytes of one NAS file of that FY: same size, same mtime (ns), different content."""
        self._record("knob", "tamper_nas_file", [fy])
        self._plain_name(fy, "FY folder")
        root = self._lane_path(self.lane_dir / "nas" / "backup" / fy)
        for f in sorted(x for x in root.rglob("*") if x.is_file() and x.name != MARKER):
            real = self._lane_path(f)
            st = real.stat()
            if st.st_size == 0:
                continue
            data = bytearray(real.read_bytes())
            data[0] ^= 0xFF
            real.write_bytes(bytes(data))
            os.utime(real, ns=(st.st_atime_ns, st.st_mtime_ns))
            return f
        raise RigError(f"no non-empty NAS file under {root} to tamper with")

    # -- dashboard calls
    def _dashboard_client(self):
        from fastapi.testclient import TestClient
        from test_support import ui_login

        import ui

        ui_login.register(ui)
        return TestClient(ui.app, headers=ui_login.ADMIN_HEADERS)

    def approve(self, phrase: str, *, client=None) -> dict:
        """Approve the first open approval request with `phrase`; append one row to approvals.jsonl."""
        c = client if client is not None else self._dashboard_client()
        listed = c.get("/approvals").json().get("approvals") or []
        req = listed[0] if listed else None
        if req is None:
            row = {"phrase": phrase, "http_status": None, "applied": False, "fy": None, "leg": None, "status": None}
        else:
            resp = c.post(f"/approvals/{req['approval_id']}/approve", json={"phrase": phrase})
            row = {"phrase": phrase, "http_status": resp.status_code, "applied": resp.status_code == 200,
                   "fy": req.get("fy"), "leg": req.get("leg"), "status": req.get("status")}
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        with open(self.evidence_dir / "approvals.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        self._record("knob", "approve", [phrase], http_status=row["http_status"])
        return row

    def fire_trigger(self, leg: str, *, client=None) -> int | None:
        c = client if client is not None else self._dashboard_client()
        status = c.post(f"/trigger/{leg}").status_code
        self._record("knob", "fire_trigger", [leg], http_status=status)
        return status

    # -- config switches
    def set_mode(self, mode: str) -> None:
        """cloud | lan | both | off -> cloud.enabled / lan.enabled."""
        if mode not in ("cloud", "lan", "both", "off"):
            raise ValueError(f"unknown mode {mode!r} (cloud, lan, both, off)")
        self._record("knob", "set_mode", [mode])

        def m(c: dict) -> None:
            c["cloud"]["enabled"] = mode in ("cloud", "both")
            c["lan"]["enabled"] = mode in ("lan", "both")

        # not validated: "off" is refused by the product model on purpose (the harness still needs it), and only
        # these two flags change on top of the config that is on disk now
        self._rewrite(m, validate=False)

    def set_lan_manual(self, flag: bool) -> None:
        """flag True = the NAS leg runs only when started by hand (lan.scheduled false)."""
        self._record("knob", "set_lan_manual", [flag])
        self._rewrite(lambda c: c["lan"].__setitem__("scheduled", not flag))

    # -- clock
    def clock_jump(self, delta: dt.timedelta) -> dt.datetime:
        if self.clock is None or self.clock.now is None:
            raise RigError("no simulated clock (create the rig with use_clock=True)")
        new = self.clock.now + delta
        self._record("clock_jump", "clock_jump", [str(delta)])
        self.clock.move_to(new)
        return new

    def clock_set(self, instant: dt.datetime) -> dt.datetime:
        if self.clock is None:
            raise RigError("no simulated clock (create the rig with use_clock=True)")
        self._record("clock_set", "clock_set", [instant.isoformat()])
        self.clock.move_to(instant)
        return instant

    # -- evidence helpers
    def normalise_mtimes(self, sim: dt.datetime) -> int:
        """Stamp every NEW file / folder under runtime\\run_logs and runtime\\logs with the simulated instant."""
        return sum(n.normalise(sim.timestamp()) for n in self.normalisers)

    def drain_mail(self, sim: dt.datetime | str) -> int:
        return self.smtp.drain(sim if isinstance(sim, str) else sim.isoformat())

    # -- Prefect
    @contextlib.contextmanager
    def prefect_harness(self):
        """Lane-private ephemeral Prefect server with the lane's own aam-backup concurrency limit (plan 1.3).
        Needs a clean PREFECT_HOME that is not ~/.prefect (set it BEFORE prefect is first imported)."""
        home = os.environ.get("PREFECT_HOME")
        if not home or Path(home).resolve() == (Path.home() / ".prefect").resolve():
            raise RigError("PREFECT_HOME must point at a clean folder (not ~/.prefect); set it before importing prefect")
        os.environ.pop("PREFECT_TEST_MODE", None)            # the REAL concurrency slot
        from prefect.testing.utilities import prefect_test_harness

        with prefect_test_harness():
            from prefect.settings import PREFECT_API_URL

            self.prefect_api_url = PREFECT_API_URL.value()      # this harness's own server (S6 compares to it)
            try:
                from launch import _ensure_concurrency_limit

                _ensure_concurrency_limit()
                yield self
            finally:
                self.prefect_api_url = None

    # -- one real flow call at a simulated instant
    def run_flow(self, name: str, *, at: dt.datetime | None = None, scheduled_at: dt.datetime | None = None,
                 attempts: int = 2, **kwargs) -> FlowOutcome:
        """Call the production flow once. `at` moves the simulated clock first; `scheduled_at` makes the
        flow see an auto-scheduled run with that expected start (manual press: leave None). Raised errors
        are captured (a BLOCKED / FAILED run raises by design); KeyboardInterrupt / SystemExit propagate."""
        if self._stack is None:
            raise RigError("rig is not installed (use `with rig:` or rig.install())")
        import flow

        attr = self.FLOWS.get(name)
        if attr is None:
            raise ValueError(f"unknown flow {name!r}; one of {sorted(self.FLOWS)}")
        fn = getattr(flow, attr)
        self._record("flow", name, {"flow": name, "at": at.isoformat() if at else None,
                                    "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
                                    "kwargs": kwargs})
        if at is not None and self.clock is not None:
            self.clock.move_to(at)
        self.seams.scheduled_at = scheduled_at
        out = FlowOutcome(name=name, at=at)
        t0 = time.perf_counter()
        try:
            for attempt in range(1, attempts + 1):
                try:
                    out.value = fn(config_path=str(self.config_path), **kwargs)
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt < attempts and HARNESS_ARTEFACT in f"{type(e).__name__}: {e}":
                        time.sleep(1.5)
                        continue
                    raise
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            out.exc = e
        finally:
            self.seams.scheduled_at = None
            out.real_seconds = time.perf_counter() - t0
        sim = at or (self.clock.now if self.clock else None)
        if sim is not None:
            self.normalise_mtimes(sim)
        return out
