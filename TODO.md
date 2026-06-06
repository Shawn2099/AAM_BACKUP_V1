# AAM Backup Automation - Project Tasks & Fault Resolution

## Completed Tasks

- [x] ~~**Health Checks**: Update `core/health.py` to raise `HealthError` for critical failures (missing GCS keys, >10 min clock skew) instead of just warning, halting the backup immediately.~~
- [x] ~~**Rollover NAS Handling**: Wrap LAN folder creation in `core/fy_rollover.py` with `try/except`. Log `ACTION REQUIRED` and proceed if NAS is offline, preventing system crash on April 1st.~~
- [x] ~~**Robocopy Exit Codes**: Segment `robocopy` exit codes 4–7 in `core/lan_sync.py` to `LAN_PARTIAL` to catch mismatches/uncopied extra files.~~
- [x] ~~**Watchdog Reliability**: Refactor `watchdog.py` to use `psutil` instead of `tasklist` subprocesses, avoiding locale-dependent CLI issues and reliably tracking PID locks.~~
- [x] ~~**WoL Network Broadcasting**: Update `core/wol.py` and config models to support dual-broadcasting magic packets to both the global broadcast (`255.255.255.255`) and the subnet-specific broadcast (auto-derived or explicitly set via `wol.broadcast_address`).~~
- [x] ~~**Documentation & Configuration Templates**: Update `DEPLOYMENT_GUIDE.md` and `config.example.yaml` to cover all new reliability behaviors and manual configuration parameters.~~

## Pending / To Do

- [ ] **Cloud Sync Rclone Flags Review**: Review `--no-traverse` flag behavior. Currently removed because it prevents rclone from checking the destination for files that have been deleted on the source, meaning deletions aren't mirrored. Investigate the optimal mix of flags for GCS sync.
- [ ] **Artifact Verification**: Monitor `flow.py` artifact publication on a real run to ensure that `files_copied` and `bytes_copied` properly pass through the pipeline to the logs and reporting artifacts.
- [ ] **Real-world Test & Logs Monitoring**: Observe a full sync cycle and check `C:\BackupAgent\logs\agent_svc.log` and `watchdog_svc.log` to confirm the new process tracking and exit code handling operates accurately under real conditions.
