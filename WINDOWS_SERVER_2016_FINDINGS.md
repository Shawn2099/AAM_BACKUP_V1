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

## Issue 8: `NamedTemporaryFile` keeps handle open — robocopy can't write to `/LOG:file`

**Symptom**: Robocopy `/LOG:C:\Temp\robo.log` exits with code 16 (fatal) but no visible error in stderr. The log file is 0 bytes.

**Cause**: `tempfile.NamedTemporaryFile(mode="w", delete=False)` opens a file handle for writing and keeps it open for the duration of the `with` block. On Windows, robocopy cannot write to a file that has an open handle. The `/LOG` flag silently fails.

**Fix**: Use `tempfile.mkstemp()` instead, then immediately close the file descriptor before passing the path to robocopy:

```python
log_fd, log_path_str = tempfile.mkstemp(suffix=".log", prefix="robocopy_sync_")
os.close(log_fd)  # Release handle so robocopy can write to it
log_path = Path(log_path_str)
```

**Rule**: Never use `NamedTemporaryFile` for paths passed to external processes on Windows. Use `mkstemp` + immediate `os.close(fd)`.

---

## Issue 9: Robocopy flag `/BYTES` does not exist

**Symptom**: Robocopy exits with code 16 (syntax error) when `/BYTES` is present in the command.

**Cause**: The `/BYTES` flag is not a valid robocopy flag. Sizes are displayed by default with `/NP` (no progress). This was a documentation error carried forward from old code.

**Fix**: Remove `/BYTES` from the robocopy command flags. Use `/V /TS /FP /NJH /NJS /NDL /NP` for clean verbose output.

---

## Issue 10: Network share operations fail over Windows SSH sessions

**Symptom**: `net use X: \\server\share` fails with "System error 67: The network name cannot be found." Robocopy to UNC paths exits with "ERROR 5: Access is denied."

**Cause**: SSH on Windows runs as SYSTEM (or a restricted token), which lacks the interactive user's network credentials. UNC path access and `net use` drive mapping operate in different security contexts — the Administrator's desktop session has the credentials, but the SSH service session does not.

**Fix**: Network share mapping and LAN backup must be run from the interactive desktop session, not via SSH. For Prefect deployments:
- Start `prefect server start` and `python serve.py` from the desktop session directly (not SSH)
- Prefect's scheduler inherits the desktop session's network credentials
- The mapped drive (`net use X: \\server\share`) persists across reboots with `/persistent:yes`

**Rule**: All UNC path and network drive operations must run from the interactive desktop. SSH is for diagnostics and cloud-only operations.

---

## Issue 11: Prefect 3.7 `Cron()` constructor uses positional-only arguments

**Symptom**: `TypeError: Cron() got some positional-only arguments passed as keyword arguments: 'cron'`

**Cause**: In Prefect 3.7, the `Cron` schedule constructor from `prefect.schedules` accepts `cron` and `timezone` as **positional-only** arguments. The dict format `{"cron": "...", "timezone": "..."}` used in older Prefect versions no longer works.

**Fix**: Use positional arguments:
```python
from prefect.schedules import Cron
Cron("0 18 * * *", "Asia/Kolkata")  # NOT Cron(cron="...", timezone="...")
```

---

## Issue 12: `serve.py` must be at project root for module resolution

**Symptom**: `ModuleNotFoundError: No module named 'flow'` when running `python deploy/serve.py`.

**Cause**: Python resolves imports relative to the script's directory. When `serve.py` is in `deploy/`, it can't find `flow.py` in the parent directory without `sys.path` manipulation.

**Fix**: Move `serve.py` to the project root directory and run from there: `python serve.py`.

---

## Issue 13: TLS 1.2 must be enabled explicitly on Windows Server 2016 for HTTPS downloads

**Symptom**: `Invoke-WebRequest` fails with "Could not create SSL/TLS secure channel" when downloading from HTTPS URLs.

**Cause**: Windows Server 2016 does not enable TLS 1.2 by default for `Invoke-WebRequest` and .NET HTTP clients. Modern servers require TLS 1.2.

**Fix**: Enable TLS 1.2 before making HTTPS requests in PowerShell:
```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri 'https://example.com/file.zip' -OutFile 'file.zip'
```

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
| Cloud sync | — | ✅ (8 files, CLOUD_COMPLETE) |
| Cloud verify | — | ✅ (verified=True, exit 0) |
| Cloud report | — | ✅ (8 files, 137 bytes) |
| LAN WoL | — | ✅ (magic packet + SMB wait) |
| LAN preflight | — | ✅ (robocopy /L dry-run) |
| LAN sync | — | ✅ (8 files, LAN_COMPLETE, exit 0) |
| LAN manifest | — | ✅ (+0 -0 *0 =8 unchanged) |
| LAN shutdown | — | ✅ (shutdown /s /t 300 initiated) |
| ManifestDB | — | ✅ (8 entries, WAL checkpoint) |
| FY auto-rollover | FY26-27 | ✅ |
| Prefect deployments | 3 registered | ✅ |

---

## Deployment Checklist (for production)

- [x] Copy GCS key to `C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\aam-demo-gcs-d9427ae2cacc.json`
- [x] Verify system clock: `Get-Date` matches real time within ±5 min
- [x] Rclone v1.74.2+ installed at `C:\Windows\System32\rclone.exe`
- [ ] `prefect server start` running in persistent terminal
- [ ] `uv run python serve.py` running from desktop session (registers 3 deployments)
- [x] Test cloud: `uv run python test_cloud.py` — PASSED
- [x] Test LAN: `uv run python test_lan.py` from desktop session — PASSED
- [x] Drive mapping: `net use X: \\10.10.186.231\lan_backup /persistent:yes`
- [x] UTF-8 encoding enforced in all file I/O
