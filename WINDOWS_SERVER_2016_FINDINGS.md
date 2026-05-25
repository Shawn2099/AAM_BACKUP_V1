# Windows Server 2016 — Deployment Findings

**Date**: 2026-05-25 / 2026-05-26
**Server**: innovizta-server-1 (10.10.186.231)
**OS**: Windows Server 2016 Standard 1607 (build 14393)
**Python**: 3.12.3 (installed via uv)

---

## Issue 1: Config YAML encoding — cp1252 can't decode UTF-8 box chars

**Symptom**: `UnicodeDecodeError: 'charmap' codec can't decode byte 0x90`

**Cause**: Windows Server 2016 default system encoding is cp1252 (Western European). The config.yaml had Unicode box-drawing characters (U+2550 ═) which cp1252 cannot decode. Even though YAML is valid UTF-8, Python's `open()` defaults to the system encoding.

**Fix**: Two changes applied:
1. Removed all box-drawing `═══` headers from config.yaml — replaced with ASCII `---`
2. Config loader in `models/config.py` now explicitly opens with `encoding="utf-8"`:
   ```python
   with open(path, encoding="utf-8") as f:
   ```

**Rule**: All text files read on Windows Server 2016 must use `encoding="utf-8"` explicitly. Never rely on the default system encoding when opening files.

---

## Issue 2: Rclone OAuth JWT rejection — system clock skew >10 minutes

**Symptom**: `oauth2: cannot fetch token: 400 Bad Request — Invalid JWT: Token must be a short-lived token`

**Cause**: The server system clock was running 1 hour 19 minutes ahead of real time. Google's OAuth JWT validation requires the token's `iat` (issued-at) to be within a reasonable timeframe — typically ±10 minutes of real time. Clock skew >10 minutes causes all GCS operations to fail with auth errors.

**Fix**: Manually corrected the system clock. If NTP is available:
```powershell
net start w32time
w32tm /resync
```

**Rule**: Before any GCS/rclone operation on Windows Server 2016, verify the system clock. Run `Get-Date` and compare to actual time. Clock skew >10 minutes will silently break all cloud operations.

---

## Issue 3: Rclone version — use latest (1.74.2)

**Symptom**: System had rclone v1.66.0 at `C:\Windows\System32\rclone.exe`. This version was released in 2024 and predates several GCS backend improvements.

**Fix**: Upgrade to rclone v1.74.2 (released 2026-05-22). Download from https://rclone.org/downloads/ and place in `C:\Windows\System32\` (already in PATH).

---

## Issue 4: Prefect API server must be running separately

**Symptom**: `RuntimeError: Failed to reach API at http://127.0.0.1:4200/api/`

**Cause**: Prefect 3.x requires its API server to be running before flows can execute. The `@flow` decorator connects to the Prefect API on initialization.

**Workaround**: For testing without Prefect, run modules directly (see `test_cloud.py`). For production:
```powershell
prefect server start
```

Then in another terminal:
```powershell
uv run python deploy/serve.py
```

**Rule**: Prefect API server must be started before any flow execution. This is a separate process from the serve/deploy process.

---

## Issue 5: PowerShell over SSH — no `&&` chaining

**Symptom**: `The token '&&' is not a valid statement separator in this version.`

**Cause**: PowerShell uses `;` as the command separator, not `&&`. This is a PowerShell language constraint, not a Windows Server 2016 issue specifically.

**Fix**: Always use `;` when chaining PowerShell commands over SSH. Example:
```powershell
cd C:\path; python test.py; echo done
```

---

## Issue 6: PowerShell stderr redirection not supported over SSH

**Symptom**: `Missing file specification after redirection operator` when using `2>&1` or `2>$null`.

**Cause**: PowerShell over SSH transport corrupts stderr redirection syntax. Use `-ErrorAction SilentlyContinue | Select-Object ...` instead.

**Fix**:
```powershell
# Instead of: command 2>$null
Get-ChildItem -ErrorAction SilentlyContinue | Select-Object Name
```

---

## Issue 7: GitHub push protection blocks service account keys

**Symptom**: `GH013: Repository rule violations found — Push cannot contain secrets`

**Cause**: GitHub's secret scanning detected the GCS service account JSON key and blocked the push.

**Fix**: Service account key file must be manually copied to the server. The config.yaml references the path, but the file itself is never committed to git. Transfer via one-time SCP/copy.

---

## Verified Working

| Component | Version | Status |
|-----------|---------|--------|
| Python | 3.12.3 | ✅ |
| uv | 0.11.16 | ✅ |
| Prefect server | 3.7.2 | ✅ (needs manual start) |
| Rclone (upgraded) | 1.74.2 | ✅ |
| Robocopy | Built-in | ✅ |
| GCS auth (rclone) | — | ✅ (after clock fix) |
| Cloud preflight | — | ✅ |
| Cloud sync | — | ✅ (8 files, 137 bytes) |
| Cloud verify | — | ✅ (verified=True) |
| Cloud report | — | ✅ |
| FY auto-rollover | FY26-27 | ✅ |
| 24/7 uptime (no restart) | — | ✅ |

---

## Deployment Checklist (for production)

- [ ] Copy GCS key to `C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\aam-demo-gcs-d9427ae2cacc.json`
- [ ] Verify system clock: `Get-Date` matches real time within ±5 min
- [ ] Rclone v1.74.2+ installed at `C:\Windows\System32\rclone.exe`
- [ ] `prefect server start` running in persistent terminal/service
- [ ] `uv run python deploy/serve.py` running (registers 3 deployments)
- [ ] Test cloud: `uv run python test_cloud.py`
- [ ] Test LAN: wake target server first, then `uv run python test_lan.py`
