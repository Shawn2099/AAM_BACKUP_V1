"""Card U12 (agent_runs/TEST_DESIGN_RELEASE.md): no secret byte reaches the outside of the server.

The repository is public, so a secret that lands in a log, a support bundle, a mail, the database or
the dashboard API is a credential leak. Three secrets are planted with unique markers:

  SMTPPW-7f3a91   notifications.smtp_password (config)
  KEYMARK-55c2    the GCS service-account private key (the key file, outside the runtime dir)
  PKID-0b8e       the GCS key's private_key_id

One cloud run fails (exit 1, fake rclone), one FAILURE mail goes to a real local SMTP server, the
support bundle is built, and the dashboard settings are read (signed in, and as Advanced). Then every
place the data can sit is searched for the three markers with bytes.find: the runtime folder (file
bytes, the zip and gz members inside them), the mails (decoded MIME parts and zip members), the
support bundle, every text column of the SQLite database, the loguru records, and the HTTP responses.

Oracle: the marker bytes themselves. Positive controls make sure the scan really read the places it
claims to read (a vacuous pass would find nothing).
"""

from __future__ import annotations

import email
import gzip
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pendulum
import yaml
from fastapi.testclient import TestClient
from loguru import logger

import ui
from core import notify as nt
from core import support_bundle as sb
from core.cloud_sync import run_cloud_sync
from core.manifest import ManifestDB
from core.run_artefacts import RunArtefacts
from models.config import CONFIG_PATH, load_config
from test_support import ui_login
from test_support.fake_tools import fake_run_tool
from test_support.smtp_server import smtp_server

SMTP_MARK = "SMTPPW-7f3a91"
KEY_MARK = "KEYMARK-55c2"
KEYID_MARK = "PKID-0b8e"
MARKERS = (SMTP_MARK, KEY_MARK, KEYID_MARK)
RUN_ID = "run-u12-cloud"

RCLONE_LOG = "\n".join([
    json.dumps({"level": "error", "msg": "Failed to copy: connection reset by peer",
                "object": "FY26-27/client.xlsx"}),
    json.dumps({"level": "info", "msg": "stats", "stats": {
        "transfers": 0, "deletes": 0, "deletedDirs": 0, "renames": 0, "errors": 1,
        "fatalError": False}}),
])


def _key_json() -> str:
    return json.dumps({
        "type": "service_account",
        "project_id": "aam-test-sandbox",
        "private_key_id": KEYID_MARK,
        "private_key": f"-----BEGIN PRIVATE KEY-----\n{KEY_MARK}\n-----END PRIVATE KEY-----\n",
        "client_email": "backup@aam-test-sandbox.iam.gserviceaccount.com",
        "client_id": "1234567890",
    }, indent=2)


def _config_file(tmp_path: Path, smtp_port: int) -> Path:
    """The sandbox config (valid defaults and dashboard hashes), pointed at the planted secrets."""
    rt = tmp_path / "rt"
    src = tmp_path / "source" / "FY26-27"
    key = tmp_path / "keys" / "gcs-key.json"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text(_key_json(), encoding="utf-8")
    base = yaml.safe_load(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    base["paths"].update({
        "source_drive": str(src), "runtime_dir": str(rt), "log_directory": str(rt / "logs"),
        "database_path": str(rt / "manifest.db"), "gcs_key_path": str(key),
    })
    base["cloud"] = {"enabled": True, "bucket": "aam-test-sandbox-u12", "project_number": "0"}
    base["notifications"] = {
        "smtp_host": "127.0.0.1", "smtp_port": smtp_port, "smtp_security": "none",
        "smtp_auth": False, "smtp_username": "backup-u12", "smtp_password": SMTP_MARK,
        "sender": "backup@firm.example", "recipients": ["ops@firm.example"],
        "send_on_failure": True, "weekly_enabled": False, "monthly_enabled": False,
        "retry_attempts": 1, "retry_base_seconds": 0.01, "smtp_timeout_seconds": 2,
    }
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(base), encoding="utf-8")
    src.mkdir(parents=True, exist_ok=True)
    (src / "client.xlsx").write_bytes(b"client data")
    return cfg_file


def _zip_members(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            yield f"{name} (inside zip)", z.read(name)


def _blobs_of_file(path: Path):
    data = path.read_bytes()
    yield str(path), data
    if path.suffix == ".gz":
        yield f"{path} (gunzipped)", gzip.decompress(data)
    if path.suffix == ".zip":
        yield from ((f"{path} :: {n}", b) for n, b in _zip_members(data))


def _runtime_blobs(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield from _blobs_of_file(path)


def _mail_blobs(raw: str):
    msg = email.message_from_bytes(raw.encode("utf-8"))
    yield "mail as sent", raw.encode("utf-8")
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename() or part.get_content_type()
        payload = part.get_payload(decode=True) or b""
        yield f"mail part {name}", payload
        if name.endswith(".zip"):
            yield from ((f"mail part {name} :: {n}", b) for n, b in _zip_members(payload))


def _db_blobs(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            cur = conn.execute(f'SELECT * FROM "{table}"')
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                for col, value in zip(cols, row):
                    if isinstance(value, str):
                        yield f"db {table}.{col}", value.encode("utf-8")
                    elif isinstance(value, bytes):
                        yield f"db {table}.{col}", value
    finally:
        conn.close()


def _offenders(blobs) -> list[tuple[str, str]]:
    found = []
    for source, blob in blobs:
        for marker in MARKERS:
            if marker.encode("utf-8") in blob:
                found.append((source, marker))
    return found


def test_no_secret_marker_reaches_a_file_a_mail_the_database_a_log_or_the_dashboard(tmp_path, monkeypatch):
    log_records: list[str] = []
    sink = logger.add(lambda message: log_records.append(str(message)), level="DEBUG")
    try:
        with smtp_server() as srv:
            cfg_file = _config_file(tmp_path, srv.server_address[1])
            cfg = load_config(str(cfg_file))
            rt = Path(cfg.paths.runtime_dir)

            # one failed cloud run (rclone faked, exit 1)
            art = RunArtefacts(rt / "run_logs", RUN_ID, day="2026-11-22")
            with fake_run_tool("core.cloud_sync", exit_code=1, log=RCLONE_LOG):
                result = run_cloud_sync(
                    source=str(tmp_path / "source" / "FY26-27"), bucket=cfg.cloud.bucket,
                    fy_prefix="FY26-27", gcs_key_path=cfg.paths.gcs_key_path,
                    project_number="0", storage_class="COLDLINE", artefacts=art,
                )
            assert result["status"] == "CLOUD_FAILED"

            db = ManifestDB(cfg.paths.database_path)
            db.insert_run({"run_id": RUN_ID, "mode": "cloud", "fy": "FY26-27",
                           "started_at": pendulum.now("Asia/Kolkata").isoformat(),
                           "status": result["status"], "exit_code": result["exit_code"],
                           "error_message": result["error"]})
            event = nt.AlertEvent(
                key="backup:cloud:CLOUD_FAILED", kind="FAILURE", mode="cloud",
                title="Cloud backup failed", message=result["error"], status=result["status"],
                exit_code=result["exit_code"], run_id=RUN_ID, artefacts_dir=str(art.dir),
                occurred_at=pendulum.now("Asia/Kolkata").isoformat())

            # one FAILURE mail through the real SMTP path
            delivery = nt.notify(cfg, event, db=db)
            assert delivery.status == nt.SENT, delivery.error
            assert len(srv.messages) == 1
            mail_raw = srv.messages[0]["data"]

            # the support bundle, built directly as well
            bundle = sb.build(cfg, event, db=db)
            assert bundle, "no support attachments built for a FAILURE"

            # the dashboard, signed in and as Advanced
            monkeypatch.setattr(ui, "_cfg", lambda: cfg)
            monkeypatch.setattr(ui, "_settings_config_path", lambda: str(cfg_file))
            monkeypatch.setattr(ui, "get_db", lambda: db)
            signed = TestClient(ui.app, raise_server_exceptions=False,
                                headers={"Accept": "application/json"},
                                cookies={"session": ui_login.SESSION_TOKEN})
            admin = TestClient(ui.app, raise_server_exceptions=False,
                               headers={"Accept": "application/json"},
                               cookies={"session": ui_login.SESSION_TOKEN,
                                        "admin_session": ui_login.ADMIN_TOKEN})
            http_bodies = []
            for client in (signed, admin):
                r = client.get("/api/settings")
                assert r.status_code == 200, r.status_code
                http_bodies.append(("HTTP /api/settings " + ("admin" if client is admin else "signed-in"),
                                    r.content))
        db.close()

        # ── the scan ────────────────────────────────────────────────────────
        runtime_blobs = list(_runtime_blobs(rt))
        mail_blobs = list(_mail_blobs(mail_raw))
        bundle_blobs = [(f"support bundle {a['filename']}", a["content"]) for a in bundle]
        bundle_blobs += [(f"support bundle {a['filename']} :: {n}", b)
                         for a in bundle if a["filename"].endswith(".zip")
                         for n, b in _zip_members(a["content"])]
        db_blobs = list(_db_blobs(Path(cfg.paths.database_path)))
        log_blobs = [("loguru records", "\n".join(log_records).encode("utf-8"))]
        scanned = [*runtime_blobs, *mail_blobs, *bundle_blobs, *db_blobs, *log_blobs, *http_bodies]

        # positive controls: the scan really read the places it claims to read
        assert any(s.endswith("rclone-sync.log") or "rclone" in s for s, _ in runtime_blobs)
        assert any(b"CLOUD_FAILED" in b or b"Cloud backup failed" in b for _, b in mail_blobs)
        assert any(s.startswith("db run_history") and RUN_ID.encode() in b for s, b in db_blobs)
        assert any(b"SETTINGS IN EFFECT" in b for _, b in bundle_blobs)
        assert any(b'"sections"' in b for _, b in http_bodies)

        assert _offenders(scanned) == []
    finally:
        logger.remove(sink)
