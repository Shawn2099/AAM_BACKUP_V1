"""Branch H scenarios (Dashboard UI, ui.py - FastAPI).

In-process TestClient against the real app. PREFECT_TEST_MODE (set by
conftest) makes trigger attempts fail fast on a missing deployment, which is
exactly the safe condition for rate-limit and auth scenarios.
"""
import pytest
from pathlib import Path

from tests.scenario_support import cfg, real_gate, record_op

pytestmark = [real_gate()]

@pytest.fixture(autouse=True)
def _fresh_rate_window():
    # each scenario starts with empty rate buckets + sessions, exactly like
    # a freshly restarted dashboard process
    import ui
    ui._RATE_LIMITS.clear()
    ui._sessions.clear()
    yield
    ui._RATE_LIMITS.clear()
    ui._sessions.clear()


API_KEY = cfg().dashboard.api_key          # production value ("test123")


def _client():
    from fastapi.testclient import TestClient

    import ui
    return TestClient(ui.app)


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


class TestUI01HealthUnauth:
    """UI-01: /health needs no auth; reports source accessibility."""

    def test_UI_01_health(self):
        sid = "UI-01"
        ops = {}
        try:
            r = _client().get("/health")
            body = r.json()
            ops.update({
                "status_code": r.status_code,
                "status_field": body.get("status"),
                "source_accessible": body.get("source_accessible"),
                "auth_required": False,
            })
            assert r.status_code == 200, f"op={ops}"
            assert body["status"] == "healthy", f"op={ops}"
            assert isinstance(body["source_accessible"], bool), f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestUI02AuthBlockRoot:
    """UI-02: no session -> browsers get 303 to /login, API clients 401."""

    def test_UI_02_auth_block(self):
        sid = "UI-02"
        ops = {}
        try:
            c = _client()
            browser = c.get("/", headers={"Accept": "text/html"},
                            follow_redirects=False)
            api = c.get("/", headers={"Accept": "application/json"},
                        follow_redirects=False)

            loc = browser.headers.get("location", "")
            ops.update({
                "browser_code": browser.status_code,
                "browser_location": loc,
                "api_code": api.status_code,
                "api_detail": api.json().get("detail", "")[:60],
            })
            assert browser.status_code == 303 and loc.startswith("/login"), f"op={ops}"
            assert api.status_code == 401, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("browsers get a friendly "
                                                  "redirect; API clients get "
                                                  "machine-readable 401")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestUI03LoginSessionCookie:
    """UI-03: correct key -> 303 to / with a hardened session cookie
    (token_hex(32), HttpOnly, SameSite=lax, 24h). Wrong key bounces back."""

    def test_UI_03_login(self):
        sid = "UI-03"
        ops = {}
        try:
            c = _client()

            wrong = c.post("/login", data={"api_key": "wrong-key"},
                           follow_redirects=False)
            good = c.post("/login", data={"api_key": API_KEY},
                          follow_redirects=False)

            set_cookie = good.headers.get("set-cookie", "")
            token_len = 0
            if "session=" in set_cookie:
                raw = set_cookie.split("session=", 1)[1].split(";", 1)[0]
                token_len = len(raw)

            ops.update({
                "wrong_bounced": wrong.status_code == 303
                                 and "/login" in wrong.headers.get("location", ""),
                "good_code": good.status_code,
                "good_location": good.headers.get("location", ""),
                "cookie_present": "session=" in set_cookie,
                "token_hex32": token_len == 64,
                "httponly": "httponly" in set_cookie.lower(),
                "samesite_lax": "samesite=lax" in set_cookie.lower(),
                "max_age_86400": "max-age=86400" in set_cookie.lower(),
            })
            assert ops["wrong_bounced"], f"op={ops}"
            assert good.status_code == 303, f"op={ops}"
            assert all([ops["cookie_present"], ops["token_hex32"],
                        ops["httponly"], ops["samesite_lax"],
                        ops["max_age_86400"]]), f"op={ops}"

            # cookie actually grants access
            authed = c.get("/", follow_redirects=False)
            ops["authed_root_ok"] = authed.status_code == 200
            assert ops["authed_root_ok"], f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestUI04TriggerRateLimit429:
    """UI-04: six rapid triggers -> the 6th gets 429 'Rate limit exceeded'
    (_RATE_MAX_TRIGGER=5). Attempts under test-mode fail fast as 500s, which
    still consume the bucket exactly like production attempts would."""

    def test_UI_04_rate_limit(self):
        sid = "UI-04"
        ops = {}
        try:
            c = _client()
            codes = []
            details = []
            for _ in range(6):
                r = c.post("/trigger/cloud",
                           headers={"X-API-Key": API_KEY})
                codes.append(r.status_code)
                try:
                    details.append(r.json().get("detail", r.json().get("status", "")))
                except Exception:
                    details.append("?")

            last_detail = str(details[-1])
            ops.update({
                "codes": codes,
                "last_detail": last_detail[:80],
                "catalog_fragment": "Rate limit exceeded",
                "note": "pre-429 attempts surface as 500 trigger_failed here "
                        "(ephemeral Prefect lacks deployments); they still "
                        "consume the window",
            })
            assert codes[-1] == 429, f"op={ops}"
            assert "Rate limit exceeded" in last_detail, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestUI05AlreadyRunningGateWiring:
    """UI-05: an active RUNNING/PENDING run must short-circuit triggers with
    400 already_running BEFORE any deployment call. Live execution of this
    state requires a real backup in flight (unsafe here) - so the gate's code
    order + query behavior are verified directly: ephemeral API reports no
    active run, and the endpoint contract text matches the catalog."""

    def test_UI_05_gate_wiring(self):
        sid = "UI-05"
        ops = {}
        try:
            import asyncio

            import ui

            none_active = asyncio.run(ui._is_running("cloud"))

            src = Path("ui.py").read_text(encoding="utf-8")
            order_ok = (
                src.find('if await _is_running("cloud")') != -1
                and src.find("already_running") != -1
                and src.find("arun_deployment(name=\"aam-backup/backup-cloud\")")
                    > src.find('if await _is_running("cloud")')
            )

            ops.update({
                "is_running_false_on_idle_api": none_active is False,
                "gate_precedes_deployment_call": order_ok,
                "contract_status_text": "400 {status: already_running}",
                "inference": ("query executes against Prefect and returns False "
                              "on an idle system; when True the endpoint "
                              "returns 400 before touching deployments"),
            })
            assert none_active is False, f"op={ops}"
            assert order_ok, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 24: UI-06 .. UI-10
# ======================================================================

import shutil
import tempfile
import time
from pathlib import Path

import pytest

from tests.scenario_support import cfg, real_gate, record_op

pytestmark = [real_gate()]

API_KEY = cfg().dashboard.api_key
AUTH = {"X-API-Key": API_KEY}


def _client():
    from fastapi.testclient import TestClient

    import ui
    return TestClient(ui.app)


def _fail_ops(ops, e):
    ops["error"] = f"{type(e).__name__}: {e}"[:250]
    return ops


def _sandbox_yaml_with_db(name: str, db_path: Path) -> tuple[str, Path]:
    """Clone the real config.yaml (full validator coverage) and point ONLY
    paths.database_path at the sandbox file."""
    import json as _json
    import re as _re

    from tests.test_scen_branch_e import _sandbox_config

    cfg_path, _sha = _sandbox_config({
        "name": name,
        "source_drive": r"C:\BackupData\FY26-27",
        "lan_destination": r"\\127.0.0.1\lan_backup\FY26-27",
    })
    txt = Path(cfg_path).read_text(encoding="utf-8")
    db_line = "  database_path: " + _json.dumps(str(db_path))
    if _re.search(r"(?m)^  database_path:", txt):
        txt = _re.sub(r"(?m)^  database_path:.*$", lambda _m: db_line, txt)
    else:
        txt = _re.sub(r"(?m)^(  gcs_key_path:.*$)",
                      lambda m: m.group(1) + "\n" + db_line, txt, count=1)
    Path(cfg_path).write_text(txt, encoding="utf-8")
    return cfg_path, db_path


class _SandboxUI:
    """Point ui's lazy loader at a sandbox yaml; restore everything after."""

    def __init__(self, name: str, db_path: Path):
        self.cfg_path, self.db_path = _sandbox_yaml_with_db(name, db_path)
        self._saved = None

    def __enter__(self):
        import models.config as mc
        import ui

        self._saved = (mc.CONFIG_PATH, ui._config, ui._config_loaded_at,
                       ui._DB_INSTANCE)
        mc.CONFIG_PATH = self.cfg_path
        ui._config = None
        ui._config_loaded_at = 0.0
        ui._DB_INSTANCE = None
        return self

    def __exit__(self, *exc):
        import models.config as mc
        import ui

        mc.CONFIG_PATH, ui._config, ui._config_loaded_at, ui._DB_INSTANCE = \
            self._saved
        return False


class TestUI06TriggerAwaitG15:
    """UI-06: fail path live (missing deployment -> 500 trigger_failed);
    success path anchor read-verified - executing it would start a REAL
    production backup, which this campaign does not do silently."""

    def test_UI_06_trigger_await(self):
        sid = "UI-06"
        ops = {}
        try:
            c = _client()
            r = c.post("/trigger/cloud", headers=AUTH)
            body = r.json() if r.headers.get("content-type", "").startswith(
                "application/json") else {}

            src = Path("ui.py").read_text(encoding="utf-8")
            await_wiring = (
                'flow_run = await arun_deployment('
                'name="aam-backup/backup-cloud")' in src
                and '"status": "triggered"' in src
                and "flow_run.id" in src
            )

            ops.update({
                "status_code": r.status_code,
                "body_status": body.get("status"),
                "await_wiring": await_wiring,
            })
            assert r.status_code == 500, f"op={ops}"
            assert body.get("status") == "trigger_failed", f"op={ops}"
            assert await_wiring, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("G15: failures surface as "
                                                  "500 trigger_failed instead "
                                                  "of fake success")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestUI07ConfigTtlReloadAndDbEvictionF13:
    """UI-07: expired TTL reloads yaml; database_path change evicts the cached
    ManifestDB under the RLock (F13) - get_db() reconnects to the NEW path."""

    def test_UI_07_ttl_reload(self):
        sid = "UI-07"
        ops = {}
        try:
            import ui

            tmp = Path(tempfile.gettempdir()) / "scen_ui07"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)

            db_a = tmp / "dash_a.db"
            db_b = tmp / "dash_b.db"

            with _SandboxUI("ui07_a", db_a):
                cfg1 = ui._cfg()
                db1 = ui.get_db()
                path1 = str(db1.db_path)

                # operator edits config: point it at config-B (new DB path),
                # then the TTL expires and the next request must pick it up
                b_path, _dbb = _sandbox_yaml_with_db("ui07_b", db_b)
                import models.config as _mc
                Path(_mc.CONFIG_PATH).write_text(
                    Path(b_path).read_text(encoding="utf-8"),
                    encoding="utf-8")
                ui._config_loaded_at = 0.0
                cfg2 = ui._cfg()
                db2 = ui.get_db()

            ops.update({
                "first_db": path1,
                "second_db": str(db2.db_path),
                "switched_to_new_file": str(db_b) in str(db2.db_path),
                "inference": ("F13: same-lock reload+evict means no thread can "
                              "query a closed connection across a rollover"),
            })
            assert str(db_b) in str(db2.db_path), f"op={ops}"

            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            shutil.rmtree(Path(tempfile.gettempdir()) / "scen_ui07",
                          ignore_errors=True)


def self_cfg_b(tmp: Path, db_b: Path) -> str:
    return str(Path(tmp) / "ui07_b.yaml")


class TestUI08ReportWeeklyNoDatabase503:
    """UI-08: /report/weekly before any backup -> 503 'No database found.'"""

    def test_UI_08_no_db(self):
        sid = "UI-08"
        ops = {}
        try:
            tmp = Path(tempfile.gettempdir()) / "scen_ui08"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)

            missing_db = tmp / "absent" / "dash.db"
            with _SandboxUI("ui08", missing_db):
                c = _client()
                r = c.get("/report/weekly", headers=AUTH)

            body_text = r.text[:140]
            ops.update({
                "status_code": r.status_code,
                "body_head": body_text,
                "catalog_fragment": "No database found",
            })
            assert r.status_code == 503, f"op={ops}"
            assert "No database found. Run a backup first." in r.text, f"op={ops}"
            record_op(sid, "PASS", ops)
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            shutil.rmtree(Path(tempfile.gettempdir()) / "scen_ui08",
                          ignore_errors=True)


class TestUI09ManualWeeklyEmail:
    """UI-09: manual email endpoint must return one of its three documented
    outcomes (200 sent / 404 no_data / 500 SMTP error) - recorded verbatim."""

    def test_UI_09_manual_email(self):
        sid = "UI-09"
        ops = {}
        try:
            from core.manifest import ManifestDB

            tmp = Path(tempfile.gettempdir()) / "scen_ui09"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)

            dbp = tmp / "dash.db"
            db = ManifestDB(str(dbp))
            db.insert_run({"run_id": "seed-ui09", "mode": "cloud",
                           "started_at": "2026-08-23T22:00:00",
                           "ended_at": "2026-08-23T22:01:00",
                           "status": "CLOUD_COMPLETE", "exit_code": 0})
            db.close()

            with _SandboxUI("ui09", dbp):
                c = _client()
                r = c.post("/trigger/report/weekly/email", headers=AUTH)

            body = {}
            try:
                body = r.json()
            except Exception:
                pass
            ops.update({
                "status_code": r.status_code,
                "body_keys": sorted(body.keys()),
                "catalog_outcomes": "200 sent / 404 no_data / 500 SMTP error",
            })
            assert r.status_code in (200, 404, 500), f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("endpoint stayed within its "
                                                  "documented outcome set; SMTP "
                                                  "placeholders on this box make "
                                                  "delivery itself out of scope")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
        finally:
            shutil.rmtree(Path(tempfile.gettempdir()) / "scen_ui09",
                          ignore_errors=True)


class TestUI10ConcurrentStatusRLock:
    """UI-10: 20 concurrent /status calls while a refresher constantly expires
    the config TTL -> RLock serializes lifecycle; zero 5xx, zero transport
    errors (F13)."""

    def test_UI_10_concurrent(self):
        sid = "UI-10"
        ops = {}
        try:
            import threading as _th

            import ui

            codes, errors = [], []
            lock = _th.Lock()

            def hit():
                try:
                    from fastapi.testclient import TestClient
                    cl = TestClient(ui.app)
                    for _ in range(4):
                        r = cl.get("/status", headers=AUTH)
                        with lock:
                            codes.append(r.status_code)
                except Exception as ex:
                    with lock:
                        errors.append(f"{type(ex).__name__}: {ex}"[:120])

            stop = _th.Event()

            def refresher():
                while not stop.is_set():
                    ui._config_loaded_at = 0.0   # force reload path continuously
                    time.sleep(0.005)

            def _refresh():
                while not stop.is_set():
                    ui._config_loaded_at = 0.0
                    time.sleep(0.005)

            refresher = _th.Thread(target=_refresh, daemon=True)
            threads = [_th.Thread(target=hit) for _ in range(5)]
            refresher.start()
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            stop.set()
            refresher.join(timeout=2)

            fives = [c for c in codes if c >= 500]
            ops.update({
                "total_requests": len(codes),
                "distinct_codes": sorted(set(codes)),
                "server_errors": len(fives),
                "client_errors": errors[:3],
            })
            assert len(codes) == 20, f"op={ops}"
            assert all(c < 500 for c in codes), f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("RLock serialized refresh+"
                                                  "queries; readers never hit a "
                                                  "closing connection")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


# ======================================================================
# Batch 25: UI-11 .. UI-15 (closes Branch H)
# ======================================================================

class TestUI11StatusContract:
    """UI-11: authed /status returns the full dashboard JSON contract."""

    def test_UI_11_status_contract(self):
        sid = "UI-11"
        ops = {}
        try:
            cfgx = cfg()
            c = _client()
            r = c.get("/status", headers=AUTH)
            body = r.json()

            top_keys = set(body.keys())
            required_top = {"firm", "fy_prefix", "schedule", "cloud", "lan",
                            "health", "recent_runs"}
            ops.update({
                "code": r.status_code,
                "firm": body.get("firm"),
                "fy_prefix": body.get("fy_prefix"),
                "schedule_cloud_human": (body.get("schedule") or {})
                                        .get("cloud_cron"),
                "has_required_top_keys": sorted(required_top - top_keys) == [],
                "cloud_keys": sorted((body.get("cloud") or {}).keys()),
                "lan_keys": sorted((body.get("lan") or {}).keys()),
                "health_source_free_gb_present":
                    "source_free_gb" in (body.get("health") or {}),
                "recent_runs_len": len(body.get("recent_runs") or []),
            })
            assert r.status_code == 200, f"op={ops}"
            assert ops["has_required_top_keys"], f"op={ops}"
            assert "Kolkata" in str(ops["schedule_cloud_human"]), f"op={ops}"
            assert isinstance(body.get("recent_runs"), list), f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("dashboard contract: human-"
                                                  "readable schedules + per-mode "
                                                  "running/last_run/last_success "
                                                  "+ free-space health")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestUI12LoginRateLimit:
    """UI-12: 10 login attempts per window; the 11th gets 429 regardless of
    key correctness (brute-force guard runs BEFORE validation)."""

    def test_UI_12_login_limit(self):
        sid = "UI-12"
        ops = {}
        try:
            c = _client()
            codes = []
            for _ in range(11):
                r = c.post("/login", data={"api_key": "nope"},
                           follow_redirects=False)
                codes.append(r.status_code)
            ops.update({
                "codes": codes,
                "first_ten_all_303": all(code == 303 for code in codes[:10]),
                "eleventh_code": codes[10],
                "catalog_limit": "_RATE_MAX_LOGIN = 10",
            })
            assert ops["first_ten_all_303"], f"op={ops}"
            assert ops["eleventh_code"] == 429, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("window resets after the "
                                                  "TTL; 429 detail says try "
                                                  "again later")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestUI13ReportDownloadLimit:
    """UI-13: report downloads capped at 10/window; 11th -> 429."""

    def test_UI_13_report_limit(self):
        sid = "UI-13"
        ops = {}
        try:
            c = _client()
            codes = []
            for _ in range(11):
                r = c.get("/report/weekly", headers=AUTH)
                codes.append(r.status_code)
            pre = codes[:10]
            ops.update({
                "codes_head": pre[:4] + ["..."] + [codes[9]],
                "pre_threshold_codes": sorted(set(pre)),
                "eleventh_code": codes[10],
                "catalog_limit": "_RATE_MAX_REPORT = 10",
            })
            assert all(c != 429 for c in pre), f"op={ops}"
            assert codes[10] == 429, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("download cap enforced "
                                                  "independently of trigger/"
                                                  "login buckets")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestUI14SessionTtlExpiry:
    """UI-14: a session aged past 24h is deleted on validation and forces
    re-login."""

    def test_UI_14_session_ttl(self):
        sid = "UI-14"
        ops = {}
        try:
            import ui

            token = ui._create_session()
            fresh_ok = ui._validate_session(token)

            # age the token by 25h (past the 24h TTL)
            ui._sessions[token]["created_at"] -= 25 * 3600
            expired_ok = ui._validate_session(token)
            removed = token not in ui._sessions

            ops.update({
                "fresh_token_valid": fresh_ok,
                "expired_rejected": expired_ok is False,
                "token_deleted_from_store": removed,
                "ttl_hours": ui._SESSION_TTL.total_seconds() / 3600,
            })
            assert fresh_ok is True, f"op={ops}"
            assert expired_ok is False and removed, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("expiry deletes the token "
                                                  "(not just rejects it) so a "
                                                  "stolen old cookie is dead")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise


class TestUI15CronToHumanDisplay:
    """UI-15: cron expressions render as operator-friendly text incl ordinal
    suffix fix (F14) for month-day schedules."""

    def test_UI_15_cron_display(self):
        sid = "UI-15"
        ops = {}
        try:
            from core.time_utils import cron_to_human

            cases = {
                ("0 22 * * *", "Asia/Kolkata"): "Daily at 22:00 Kolkata",
                ("0 21 * * *", "Asia/Kolkata"): "Daily at 21:00 Kolkata",
                ("0 8 * * MON", "Asia/Kolkata"): "Every Monday at 08:00 Kolkata",
                ("0 8 1 * *", "Asia/Kolkata"): "1st of month at 08:00 Kolkata",
                ("0 8 11 * *", "Asia/Kolkata"): "11th of month at 08:00 Kolkata",
                ("0 8 13 * *", "Asia/Kolkata"): "13th of month at 08:00 Kolkata",
            }
            results = {f"{cron} | {tz}": cron_to_human(cron, tz)
                       for (cron, tz) in cases}
            mismatches = {key: res for key, res in results.items()
                          if res != cases[(key.split(" | ")[0],
                                           key.split(" | ")[1])]}
            ops.update({
                "results": results,
                "mismatches": mismatches,
                "f14_ordinal_note": "11th/13th use -th (old code said 11st/13rd)",
            })
            assert not mismatches, f"op={ops}"
            record_op(sid, "PASS", {**ops,
                                    "inference": ("schedules render exactly "
                                                  "as operators see them on "
                                                  "the dashboard")})
        except Exception as e:
            record_op(sid, "FAIL", _fail_ops(ops, e))
            raise
