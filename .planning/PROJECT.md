# AAM Backup Automation V1

## What This Is

Dual-destination backup automation for Windows Server 2016 delivering nightly file backups to both LAN (via Robocopy /MIR to a UNC share) and Cloud (via rclone sync to Google Cloud Storage). Orchestrated by Prefect 3 with separate cron schedules, a FastAPI dashboard with manual triggers, SQLite manifest tracking, Loguru logging, and email notifications.

## Core Value

Nightly backups complete reliably on schedule — data is safe in two locations (LAN + GCS) even if the backup process or destination is temporarily offline.

## Requirements

### Validated

- ✓ Cloud backup pipeline (preflight → sync → verify → report → DB) — Phase 1 in IMPLEMENTATION_PLAN
- ✓ LAN backup pipeline (WoL → preflight → sync → manifest → DB → shutdown) — Phase 2 in IMPLEMENTATION_PLAN
- ✓ Wake-on-LAN server management — Phase 2
- ✓ Manifest database with file tracking and run history — Phase 3
- ✓ Pydantic v2 configuration with cross-field validation — Phase 3
- ✓ Email notifications (failure alerts, weekly/monthly summaries) — Phase 4
- ✓ Dashboard UI with manual triggers and run history — Phase 5
- ✓ Windows Server 2016 compatibility fixes (NamedTemporaryFile, UTF-8, clock skew, etc.) — WINDOWS_SERVER_2016_FINDINGS.md
- ✓ Edge case audit with 140+ test scenarios, 4 bugs found and fixed — EDGE_CASE_AUDIT.md

### Active

- [ ] Dashboard authentication (Phase 1 — DONE: API key + session auth added)
- [ ] Move hardcoded retry/timeout params to config.yaml (Phase 2)
- [ ] Add exponential backoff with jitter (Phase 2)
- [ ] Move cron schedules to config.yaml (Phase 2)
- [ ] Make rclone --transfers/--checkers configurable (Phase 2)
- [ ] Deep code review via gsd-code-reviewer (Phase 3)
- [ ] Create test infrastructure with pytest (Phase 4)
- [ ] Add config.yaml to .gitignore (Phase 5)
- [ ] Add health endpoint for monitoring (Phase 5)
- [ ] Run security audit (Phase 5)

### Out of Scope

- Prometheus/OpenTelemetry metrics — deferred, not needed for single-server deployment
- Full secret management (vault) — SMTP creds in config.yaml acceptable for LAN-internal tool
- Multi-tenant or multi-user dashboard auth — single API key is sufficient
- Webhook/API integration for external monitoring — not needed at this stage

## Context

- Target OS: Windows Server 2016 — Python 3.12, Prefect 3, Robocopy, rclone 1.74.2
- Development platform: Linux (Ubuntu) — deployed to Windows via SMB/batch scripts
- Single-server deployment: backup source machine runs everything (Prefect serve + dashboard)
- Team size: solo developer/administrator
- Existing artifacts: IMPLEMENTATION_PLAN.md (full spec), EDGE_CASE_AUDIT.md (140+ tests documented), WINDOWS_SERVER_2016_FINDINGS.md (13 compat issues), REVIEW.md (code review findings)

## Constraints

- **Compatibility**: Must run on Windows Server 2016 (no TLS 1.3, older PowerShell, UNC share quirks)
- **Execution**: All subprocess calls (rclone, robocopy, prefect CLI) must handle PowerShell-specific behavior
- **Network**: LAN backup requires SMB connectivity over UNC paths; cloud backup requires GCS access
- **Security**: Dashboard must be LAN-accessible but not internet-facing
- **Persistence**: SQLite WAL-mode database with manual checkpointing
- **No hot-reload**: Config changes require restart of serve.py process

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Two separate Prefect deployments (not --mode all) | LAN/cloud have different schedules, preconditions, durations | ✓ Good |
| Zero-import between core modules | Prevents circular deps, keeps flow.py as sole orchestrator | ✓ Good |
| WAL-mode SQLite with threading.Lock | Thread-safe without separate DB server | ✓ Good |
| mkstemp instead of NamedTemporaryFile | Windows file handle lock with subprocess (bug found in edge case audit) | ✓ Good |
| Inline HTML in ui.py | No template dir, single-file deployment | ⚠️ Revisit — consider Jinja2 |
| API key auth (not OAuth/LDAP) | Single-server LAN tool, no user management needed | ✓ Good |

---
*Last updated: 2026-05-27 after Phase 1 (Dashboard Auth)*
