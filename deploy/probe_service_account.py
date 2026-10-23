"""Readiness probe, run AS the backup service account (plan WP16, 4.5).

04_check_readiness.ps1 starts this with the service account's credentials
(Start-Process -Credential), so every check below runs with exactly the
token and network identity the AamBackupAgent service will have. It reuses
the same functions as the nightly preflight (core.permissions), so the
readiness gate and the backup can never disagree about a right.

Root cause it exists for (plan §10 item 2): the services ran as LocalSystem
in a WORKGROUP, which has no usable credentials on the NAS - 21 nights of
"Access is denied" while manual runs as Administrator worked.

Usage:  .venv\\Scripts\\python.exe deploy\\probe_service_account.py <config.yaml> <result file>
Writes one line per check ("OK|FAIL|SKIP <check>: <detail>") to the result
file and exits 0 only when nothing failed. Never changes anything except
creating and deleting its own probe files.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def run_checks(config_path: str) -> list[tuple[str, str, str]]:
    from core import permissions
    from core.run_artefacts import RunArtefacts
    from models.config import load_config

    results: list[tuple[str, str, str]] = []

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            results.append(("OK", name, detail or "ok"))
        except Exception as e:  # noqa: BLE001 - every failure is reported, none raised
            results.append(("FAIL", name, f"{type(e).__name__}: {e}"))

    import getpass
    results.append(("OK", "identity", f"running as {os.environ.get('USERDOMAIN', '?')}\\"
                                      f"{getpass.getuser()}"))
    try:
        config = load_config(config_path)
    except Exception as e:  # noqa: BLE001
        results.append(("FAIL", "config", f"config does not load: {e}"))
        return results
    results.append(("OK", "config", f"{config_path} is valid (schedule fits the run budgets)"))

    from core import secret_setup
    check("mail password", lambda: secret_setup.check_smtp_password(config.notifications))

    def runtime_write() -> str:
        runtime = Path(config.paths.runtime_dir)
        probe = runtime / f".aam_account_probe_{uuid.uuid4().hex}"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        return f"can write {runtime}"
    check("runtime folder", runtime_write)

    def config_dir_write() -> str:
        # FY rollover writes config.yaml.bak-* and a temp file next to config.yaml
        probe = Path(config_path).resolve().parent / f".aam_account_probe_{uuid.uuid4().hex}"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        return f"can write next to {config_path} (FY rollover)"
    check("config folder", config_dir_write)

    check("source read", lambda: f"read {permissions.probe_source_read(config.paths.source_drive)}")

    if config.lan.enabled:
        check("NAS write", lambda: (
            permissions.probe_destination_write(config.paths.lan_destination,
                                                f"readiness-{uuid.uuid4().hex[:8]}"),
            f"create/write/read/delete at {config.paths.lan_destination}")[1])
        if config.lan.use_backup_mode:
            def privilege() -> str:
                artefacts = RunArtefacts.ephemeral("readiness")
                permissions.check_backup_privilege(
                    artefacts, timeout_s=config.lan.privilege_check_timeout_seconds)
                return "SeBackupPrivilege held (robocopy /ZB)"
            check("backup privilege", privilege)
        else:
            results.append(("SKIP", "backup privilege", "lan.use_backup_mode is false"))
    else:
        results.append(("SKIP", "NAS write", "lan.enabled is false"))

    if config.cloud.enabled:
        def cloud() -> str:
            from core import cloud_preflight, fy_rollover
            from core.rclone_config import temp_rclone_config
            fy = fy_rollover.configured_fy(config.paths.source_drive, config.paths.lan_destination)
            artefacts = RunArtefacts.ephemeral("readiness-cloud")
            # read: the sync's own bucket probe (credentials, bucket exists, network)
            read = cloud_preflight.run_cloud_dry_run(
                source=config.paths.source_drive, bucket=config.cloud.bucket, fy_prefix=fy,
                gcs_key_path=config.paths.gcs_key_path, project_number=config.cloud.project_number,
                storage_class=config.cloud.storage_class, location=config.cloud.location,
                timeout=config.cloud.preflight_timeout_seconds, retries=config.cloud.probe_retries,
                retries_sleep_seconds=config.cloud.probe_retries_sleep_seconds, artefacts=artefacts)
            if not read["ok"]:
                raise RuntimeError(f"cannot read gs://{config.cloud.bucket}/{fy}: {read['error']}")
            # write: the same create + delete probe the nightly run makes, outside every FY prefix
            with temp_rclone_config(config.paths.gcs_key_path, config.cloud.location,
                                    config.cloud.project_number, config.cloud.storage_class) as rclone_conf:
                permissions.probe_cloud_write(
                    config.cloud.bucket, f"readiness-{uuid.uuid4().hex[:8]}", rclone_conf,
                    config.cloud.preflight_timeout_seconds, artefacts,
                    error_tail_lines=config.cloud.error_tail_lines)
            return f"read and create/delete proven on gs://{config.cloud.bucket}"
        check("cloud bucket", cloud)
    else:
        results.append(("SKIP", "cloud bucket", "cloud.enabled is false"))

    results.append(("SKIP", "NAS remote shutdown",
                    "cannot be tested without shutting the NAS down - verified by the "
                    "NAS shutdown check after the first real LAN backup (WP10)"))
    return results


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    config_path, result_file = argv[1], argv[2]
    try:
        results = run_checks(config_path)
    except Exception as e:  # noqa: BLE001 - e.g. the project cannot even be imported
        results = [("FAIL", "python", f"project code could not run: {type(e).__name__}: {e}")]
    # one line per check: 04_check_readiness.ps1 parses the file line by line
    # (pydantic / OS errors span several lines)
    lines = [f"{status} {name}: {' '.join(str(detail).split())}" for status, name, detail in results]
    Path(result_file).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 1 if any(status == "FAIL" for status, _, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
