# Windows compatibility review: Server 2016 / 2019 / Windows 10 / Windows 11

**Date:** 2026-11-23 · **Branch:** `fy-overlap` · **Mode:** READ-ONLY static review (no edits to tracked files, no tests, no services, no commits, clock untouched).

**Marks used**
- **CONFIRMED**: checked here (parser, reflection, binary/lock inspection, code or doc read) with file:line evidence.
- **RISK**: code or docs show it can fail on the target OS. Not proven.
- **UNVERIFIABLE-HERE**: needs a real 2016/2019/10/11 machine, or rests on outside knowledge that was not re-checked live.

**What was run here (only this box, Windows 11 10.0.26300):**
- PowerShell **5.1.26100** (Desktop edition) parser on every `.ps1` in `deploy/`, root, `tools/`, `audit_lab/`, `agent_runs/scripts/`.
- 5.1 parameter reflection (`Get-Command ... .Parameters`) and one binding-only `Test-Connection` call (no network traffic).
- `rclone.exe version`, `nssm.exe version`, PE header read of the bundled `deploy/bin` binaries.
- `.venv` interpreter introspection and a string scan of the native `.pyd`/`.dll` imports.
- `uv.lock` wheel survey (filenames only).

The 5.1 here is **not** the 2016 build. The trial box runs **5.1.14393** (`SESSION_HANDOFF.md:991`). Parser results carry over, runtime cmdlet behaviour does not.

---

## 0. Verdict summary

| Area | Verdict |
|---|---|
| PS 5.1 syntax (all `.ps1`) | CONFIRMED: 14/14 parse with 0 errors |
| PS 5.1 runtime parameter bug | CONFIRMED BUG: `Test-Connection -TimeoutSeconds` (PS 6+ only), `deploy/01_server_discovery.ps1:156,172,412` |
| PS 7-only syntax / removed cmdlets | CONFIRMED absent (none found) |
| Python 3.12 + wheels | CONFIRMED: every runtime dependency has a Windows wheel or pure/abi3 wheel; no compiler needed |
| VC++ redistributable | CONFIRMED not needed: the interpreter bundles `vcruntime140*.dll`; the extension modules resolve to it |
| TLS on 2016 | CONFIRMED: Python uses bundled OpenSSL (no SChannel dependency); PowerShell web calls set Tls12 |
| rclone / robocopy flags | CONFIRMED on the trial box for robocopy and rclone 1.74.3; rclone 1.76.0 on 2016 UNVERIFIABLE-HERE |
| Locale parsing | Mixed: `sc queryex` fixed (CONFIRMED); robocopy summary labels and `icacls` names are English-only (RISK) |
| Service account rights on a **domain controller** | RISK (HIGH): production target is a DC; trial box was WORKGROUP with a local account |
| NAS access / NAS shutdown credentials | RISK (HIGH): no credential handling in code |
| Dashboard in IE11 (default browser on Server 2016/2019) | RISK (HIGH for usability): ES2017 and fetch in `static/js`, IE11 cannot run it |
| Console encoding (cp1252 on en-US 2016) | RISK (MEDIUM): non-cp1252 characters in log text; no UTF-8 override set |

---

## 1. PowerShell and batch

### 1.1 Syntax (PowerShell 5.1 parser)

Parsed with `[System.Management.Automation.Language.Parser]::ParseFile` under 5.1.26100 Desktop: **0 errors in all 14 `.ps1` files**.

| File | Result |
|---|---|
| `deploy/01_server_discovery.ps1` … `12_configure_gcloud.ps1`, `deploy/_common.ps1`, `deploy/lock_gcs_key.ps1` | 0 errors |
| `tools/install_seed_daily_task.ps1`, `tools/run_audit_lab.ps1`, `tools/server_health/server_health.ps1`, `tools/server_health/remote/cloud_check.ps1` | 0 errors |
| `audit_lab/bootstrap_deployed.ps1`, `audit_lab/setup_service_account.ps1`, `agent_runs/scripts/box.ps1` | 0 errors |
| root (`*.ps1`) | none exist. Root has only `run_tests.bat` and `start_dashboard.bat` |

**CONFIRMED.** Limit: the parser checks syntax, not cmdlet parameter names. That gap is the bug in 1.2.

Batch files (`deploy/*.bat`, root `*.bat`, `tools/server_health/run_daily.bat`) were not parsed; `cmd.exe` has no parser to run here. **CONFIRMED:** the working-tree `.bat` files (`03`: 297/297 lines, `09`: 244/244, `run_tests.bat`: 9/9) all end in CRLF. `.gitattributes` forces `*.bat`, `*.cmd`, `*.ps1` to `eol=crlf` on checkout (the index stores LF, the checkout is CRLF), which is what `cmd` `goto`/labels need. **UNVERIFIABLE-HERE** for `cmd` behaviour on 2016.

### 1.2 Runtime bug: `Test-Connection -TimeoutSeconds` (PS 6+ parameter)

- `deploy/01_server_discovery.ps1:156`, `:172`, `:412` call `Test-Connection ... -TimeoutSeconds 2`.
- Reflection on 5.1 here: `TimeoutSeconds` is **absent** (params: `ComputerName, Count, Delay, Quiet, TimeToLive, Protocol, BufferSize, ...`).
- Binding test: `A parameter cannot be found that matches parameter name 'TimeoutSeconds'`.

**CONFIRMED BUG** on every PowerShell 5.1 (2016, 2019, Win 10, Win 11). The discovery script stops at the first call. It is a one-off report tool, not on the backup path, but it is often the first script run on a new server. Fix: drop the parameter or use `Start-Job`/`Test-NetConnection` (`-Port`) on 5.1.

### 1.3 Cmdlet and parameter matrix (PS 5.1 / 2016)

| Item | Where | Finding |
|---|---|---|
| `ConvertFrom-Json -AsHashtable` | none in repo | CONFIRMED not used; absent on 5.1 (reflection) |
| `Invoke-RestMethod -SkipHttpErrorCheck` | none | CONFIRMED not used; absent on 5.1 |
| `Invoke-WebRequest` without `-UseBasicParsing` | every real call has it (`06:444`, `server_health.ps1:101`, `bootstrap_deployed.ps1`) | CONFIRMED: no call lacks it. Without it the IE engine is needed, which fails on Server 2016/2019 with IE ESC |
| `Invoke-WebRequest -TimeoutSec` | `06:444` | CONFIRMED param exists on 5.1 |
| Ternary `? :`, `&&`, `\|\|`, `??`, `?.` | none | CONFIRMED absent (grep + parser) |
| `if` used as expression (`$x = if (...) {...}`) | `_common.ps1:47` | CONFIRMED valid on 5.1 (parser) |
| `Get-SmbShare`, `New-SmbShare` | none | CONFIRMED not used |
| `Test-Connection -TimeoutSeconds` | `01:156,172,412` | **CONFIRMED BUG** (1.2) |
| `Get-WmiObject` (deprecated) | `01:64-66,98,362-364` | CONFIRMED present on 5.1; works on 2016 (RISK low: removed only in PS 7) |
| `Get-CimInstance` | `06:203,382`, `_common.ps1:48`, `11:33` | CONFIRMED present on 5.1; the CIM stack ships on 2012+ (knowledge) |
| `wmic` | removed from `09_restore_from_gcs.bat:75`; only a comment remains | CONFIRMED. Was the Win 11 24H2+ failure (`wmic` removed) |
| `curl.exe` | none | CONFIRMED not used. (Knowledge: not present on Server 2016; present on 2019 1809+) |
| `tar.exe` | none | CONFIRMED not used |
| `Expand-Archive` | none; `03_setup_system.bat:209` uses bundled `7za.exe` | CONFIRMED (no 5.1 `Expand-Archive` size limits hit) |
| `Get-NetFirewallRule`, `Remove-NetFirewallRule` | `06:475-477` | CONFIRMED present on 5.1 here; NetSecurity module ships 2012+ (knowledge) |
| `Get-MpPreference`, `Add-MpPreference` (Defender) | `03:184`, `04:235` | RISK (MEDIUM): cmdlets exist only when the Defender feature is present. `03:184` uses `-ErrorAction SilentlyContinue` (silent), `04:235` uses `SilentlyContinue` too. UNVERIFIABLE-HERE on a 2016 DC |
| `Register-ScheduledTask`, `Get-ScheduledTaskInfo` | `04:379-443`, `tools/install_seed_daily_task.ps1` | CONFIRMED present on 2012+ (knowledge). Needs "Log on as a batch job" for the account (`04:415` names it). RISK on a DC if GPO denies it |
| `Get-LocalUser`, `New-LocalUser` | comment only (`_common.ps1:43`) | CONFIRMED not called; the code handles a DC (`_common.ps1:50-53`) |
| `Get-Service` | `01`, `04`, `06`, `12`, `server_health` | CONFIRMED present; no locale-dependent output parsing |
| `Start-Process -Credential` | `04:372` comment | CONFIRMED avoided (`0xC0000142` from non-desktop sessions, per `SESSION_HANDOFF.md:908`) |
| `sc.exe failure ... reset= ... actions= restart/...` | `06:322-323,392-393,431-432` | CONFIRMED valid sc.exe syntax on 2016 (`2>$null` only drops stderr) |
| `tzutil /g`, `tzutil /s` | `03:287-288` (text only) | CONFIRMED exists on 2016 (knowledge) |
| `setx /M` | `03:108` | CONFIRMED exists; 1024-char limit not hit |
| `icacls` with `*S-1-5-...` SIDs | `03:277` | CONFIRMED supported on 2016 (knowledge); `_common.ps1:67` notes `.\name` must become `COMPUTERNAME\name` for icacls (CONFIRMED on the trial box) |
| `reg add HKLM\...` | `03:127,163-167` | CONFIRMED (standard) |
| `net session` as admin test | `03`, `07`, `08`, `09`, `update_gcloud.bat` | CONFIRMED standard; `net session` needs the Server service (default on) |
| `w32tm` | `01:423`, `04:218` | CONFIRMED present on 2016; output localized (see 1.4) |

### 1.4 Locale-dependent parsing

- **`sc queryex` (CONFIRMED FIXED).** `core/services.py:54-80` parses the numeric `STATE` code and not the English label (the code comment: labels are `ZUSTAND` / `ETAT` in other languages). `deploy` scripts only use `sc` exit codes (`08_uninstall_services.bat:41-63`). The item left in `SESSION_HANDOFF.md`/`RELEASE_BREAK_PART2:63` ("queued: localized sc queryex") is therefore done in code. Not run on a non-English box.
- **robocopy summary labels (RISK, MEDIUM).** `core/lan_sync.py:71-86` match English labels `^\s*Files\s*:` and `^\s*Ended\s*:` (`_FILES_ROW`, `_ENDED_LINE`). The comment (`lan_sync.py:79-84`) claims locale-independence, which holds only for column order. On a German/French 2016, the summary labels differ, so `log_complete` is False and a successful run can be classified as suspect/incomplete. Fail-closed, but wrong. **UNVERIFIABLE-HERE** (needs a non-English robocopy run).
- **`icacls` principal names (RISK, MEDIUM, fail-open).** `deploy/lock_gcs_key.ps1:48-52` looks for the English strings `BUILTIN\Users`, `Everyone`, `NT AUTHORITY\Authenticated Users`. icacls prints localized names on a localized OS, so the "can still reach" check can miss and report success. **UNVERIFIABLE-HERE.**
- **`whoami /priv` (CONFIRMED OK).** `core/permissions.py:8-14` reads column 1 (the privilege name, locale-independent) and does not parse descriptions.
- **`w32tm` (CONFIRMED OK in code).** `core/health.py:143-161` parses the numeric sample, replaces a decimal comma (`float(...replace(",", "."))`), and treats `error: 0x800705B4` as no sample. Trial box: rc 0 with that error text (`SESSION_HANDOFF.md:953-955`).
- **Dates (CONFIRMED OK).** `Get-Date -Format` uses numeric patterns only (`01:52`, `09:76`, `run_audit_lab.ps1:14`). Python timestamps use `strftime("%Y-%m-%dT%H:%M:%S%z")` (`restore.py:511`, `upload.py:292`).

### 1.5 Encoding, long paths, UNC, 8.3 names

- **Script encoding (CONFIRMED, LOW).** None of the 14 `.ps1` files has a UTF-8 BOM. PowerShell 5.1 reads a BOM-less file as the system ANSI code page (cp1252 on en-US 2016). Non-ASCII bytes appear only in comments and one `Write-Host` string: `deploy/04_check_readiness.ps1:346` (comment), `deploy/06_install_services.ps1:2,234` (`§`, inside a double-quoted string: harmless mojibake only), `audit_lab/*.ps1` (comments and box-drawing comments). No non-ASCII character sits inside a string that is parsed as code. The batch files have em-dashes in `echo` and `:::` lines (`08:3,140`, `09:124`); `cmd` uses the OEM code page, so the text prints garbled and nothing breaks.
- **Console and log encoding (RISK, MEDIUM).** cp1252 cannot encode `→` (U+2192): CONFIRMED by a `str.encode('cp1252')` test here. Em-dash (U+2014) encodes fine. Log messages use `→` (`flow.py:877,1066,1330`, `core/cloud_sync.py:377`, `core/lan_preflight.py:160`). The loguru stderr sink (`core/logging.py:32`) and the watchdog stdout sink (`watchdog.py:178`) write to the NSSM-captured stream. Neither forces UTF-8 (`PYTHONUTF8` / `PYTHONIOENCODING` do not appear in the agent `AppEnvironmentExtra`, `06:372`). Loguru catches sink errors by default, so the risk is lost or garbled lines, not a crash. `launch.py` `print()` calls use only em-dashes (cp1252-safe) today. **UNVERIFIABLE-HERE** on 2016 en-US.
- **File logs (CONFIRMED OK).** Loguru file sinks use `encoding="utf-8"` (`core/logging.py:42`). Robocopy `/UNILOG` writes UTF-16 and is read with `encoding="utf-16"` (`lan_sync.py:154`, `ARCHITECTURE.md:186`).
- **`write_text` without encoding (CONFIRMED LOW).** `core/upload.py:314` writes `json.dumps(..., default=str)` with default `ensure_ascii=True`, so the output is ASCII and the cp1252 default is harmless.
- **Long paths (CONFIRMED set, UNVERIFIABLE-HERE effect).** `deploy/03_setup_system.bat:127` writes `LongPathsEnabled=1`; `deploy/04_check_readiness.ps1:148-149` checks it; `deploy/01:277-286` reports it. Python 3.12 long-path support also needs the interpreter's manifest (knowledge). rclone uses `\\?\` internally on Windows (knowledge). **UNVERIFIABLE-HERE** for a 260+ char NAS path.
- **UNC (CONFIRMED handled).** `core/shutdown.py:76` builds `\\<ip>`; `core/restore.py:81` strips a `\\?\UNC\` prefix; `core/lan_manifest.py:39` notes the same issue.
- **8.3 short names (CONFIRMED none).** No `PROGRA~1`, `DOCUME~1` or similar in `deploy/`, `core/`, `*.py`, `*.bat`.

### 1.6 Service account, rights and profile (the 2016 DC question)

Target facts: `SESSION_HANDOFF.md:89` says the install target is **AAMBDC001**, Server 2016 Datacenter, **domain controller** (`caaam.com`). The trial box (`SESSION_HANDOFF.md:985`) is **Server 2016 Standard, WORKGROUP**, with a local `.\aambackup` account that worked (`SESSION_HANDOFF.md:254`, 2026-10-20).

- **Domain controller accounts (CONFIRMED handled in code, RISK on the box).** `deploy/_common.ps1:41-64` detects a DC (`DomainRole >= 4`), refuses `.\name`, and accepts `DOMAIN\name`. A DC has no local accounts, so the install must use a domain account. **RISK (HIGH):** the domain GPO decides "Log on as a service" and "Log on as a batch job", and can deny them. Untested on a DC.
- **"Log on as a service" (CONFIRMED mechanism, RISK on DC).** `deploy/06_install_services.ps1:374-376` relies on NSSM granting the right when it sets `ObjectName`. Read-back check at `06:382` refuses a wrong account. (Trial-box proof: all three services ran as `.\aambackup`, `SESSION_HANDOFF.md:254`.) **UNVERIFIABLE-HERE** on a DC with GPO.
- **"Log on as a batch job" (CONFIRMED check, RISK).** The readiness probe is a scheduled task run as the account (`deploy/04_check_readiness.ps1:372-393`). It needs `SeBatchLogonRight`; `04:415` says so. If GPO removes it, the probe reports it, but install does not fix it.
- **`SeBackupPrivilege` ("Back up files and directories").** Needed only for `lan.use_backup_mode: true` (`/ZB`, `core/lan_sync.py:418`). Checked by `core/permissions.py:83-102` via `whoami /priv` (the privilege name is locale-independent). **RISK:** a DC GPO may not grant it to the account. The check fails closed with AAM-LAN-05.
- **Runtime folder ACLs (CONFIRMED).** `deploy/_common.ps1:72-96` grants Modify with `icacls ... /T /C /Q`. Uses `COMPUTERNAME\name`, not `.\name` (`_common.ps1:67`). Trial-box lesson: missing `/T` left existing logs unreadable (`SESSION_HANDOFF.md:911-912`).
- **HKCU (CONFIRMED not used).** No `winreg`, `HKCU`, or `HKEY_CURRENT_USER` in `core/`, `flow.py`, `ui.py`. gcloud config is pinned to `runtime/gcloud` via `config_dir` (`flow.py:2914`). `CLOUDSDK_PYTHON` is set machine-wide (`deploy/12_configure_gcloud.ps1:27-32`).
- **TEMP for the service account (RISK, MEDIUM).** Python `tempfile` (`core/lan_sync.py:370`, `core/cloud_reporter.py:180`, `core/config_file.py:88`, `core/run_artefacts.py:139`, `core/rclone_config.py:49`) and gcloud rely on the account's `%TEMP%`. For a service account that is `C:\Users\<acct>\AppData\Local\Temp`, which exists only once the profile is created. `tempfile` falls back to `C:\TEMP`, `C:\TMP`, `\TEMP`, `\TMP`, then the cwd, but a failure is not caught here. The readiness probe (scheduled-task logon) is the only step that creates the profile. **UNVERIFIABLE-HERE.**
- **DPAPI (CONFIRMED design, UNVERIFIABLE-HERE per OS).** `core/secrets.py:48-67` uses `CryptProtectData` with `CRYPTPROTECT_LOCAL_MACHINE` (the `_LOCAL_MACHINE` flag) plus `_UI_FORBIDDEN`. Machine scope means any account on that machine can decrypt (the service account can). The blob is tied to that machine, so a move to another server breaks it. On a DC the machine key works the same way (knowledge).
- **LocalSystem.** Trial box ran all services as LocalSystem before the fix (`SESSION_HANDOFF.md:985`). Production should not.

### 1.7 NSSM

- Bundled `deploy/bin/nssm.exe`: **NSSM 2.24-101-g897c7ad, 64-bit** (`VersionInfo`, and `nssm version`). PE minimum OS 5.2 (not a floor for 2016).
- NSSM 2.24 is the last official release (2014; knowledge). It ran the three services on **Server 2016 Standard** as a local account (`SESSION_HANDOFF.md:254`). **CONFIRMED on 2016 WORKGROUP.** Not run on a DC.
- Known open behaviour (RELEASE_BREAK PART6:11-23): the Prefect service stop is capped near 45 s by NSSM thread and console timeouts. Agent stop uses a 180 s Console window (`06:364`). `AppKillProcessTree 1` is set (`06:371`, `:411`). NSSM/SCM "not finishing after the kill" is an unreproduced cause (PART6:21). **RISK (MEDIUM)** for the 2016 service stop and restart ordering. **UNVERIFIABLE-HERE.**
- Failure restart: `sc.exe failure` at `06:322-323,392-393,431-432` (reset 86400 s). **CONFIRMED** syntax.

### 1.8 Other deploy-level items

- **Active Hours (CONFIRMED in code, UNVERIFIABLE-HERE).** `03_setup_system.bat:163-167` now writes 20:00-08:00 (a 12 h window, the handoff's stated maximum for WS2016). `SESSION_HANDOFF.md:599` recorded the old 18 h value as invalid, so this was fixed in code. Not re-read on the box.
- **TLS for downloads (CONFIRMED).** `03_setup_system.bat:80` (uv installer) and `:240` (gcloud SDK) set `SecurityProtocol = Tls12` before `Invoke-WebRequest`. Windows PowerShell 5.1 on 2016 defaults to older protocols, so this matters.
- **Windows Update (CONFIRMED in code, RISK).** `03` sets Active Hours and checks the service (`04:170`). `SESSION_HANDOFF.md:599` says WU was stopped on the trial box.

---

## 2. Python runtime and dependencies

### 2.1 Pinned Python and interpreter

- `pyproject.toml`: `requires-python = ">=3.12"`. `uv.lock` pins 3.12 for the runtime set.
- `.venv` here: **CPython 3.12.15** (`MSC v.1944`), **OpenSSL 3.5.9**, **sqlite 3.53.1**, uv 0.12.23 (`.venv/pyvenv.cfg`).
- Interpreter source on the box: `C:\Program Files\Python312` (all users) or uv into `%ProgramData%\uv\python` (`03_setup_system.bat:104-109`, `_common.ps1:36-37`). Not a user profile (`04:277` refuses that).
- **Python 3.12 Windows floor:** owner note and python.org docs say Windows 8.1 / Server 2012 R2 or newer (knowledge, not re-fetched). **Server 2016 meets it.** UNVERIFIABLE-HERE on the box.
- Windows 8.1 needed the UCRT update (KB2999226) for Python 3.5+. 2016 has the UCRT in-box (knowledge). Not needed on 2016.

### 2.2 VC++ redistributable (CONFIRMED not needed as a separate install)

Scan of the venv interpreter and native modules (string scan of import names, not a full PE parse):
- `python.exe` and `python312.dll` import `vcruntime140.dll`. The base folder also ships `vcruntime140.dll` and `vcruntime140_1.dll`.
- **84** `.pyd` files import `vcruntime140.dll`, including `pydantic_core`, `psutil` (`_psutil_windows.pyd` imports `vcruntime140.dll` and `ws2_32.dll`), `cryptography` (`_rust.pyd`, with `crypt32`, `bcrypt`), `orjson`, `regex`, `sqlalchemy` cyextensions, `websockets`, `yaml`, `markupsafe`.
- The bundled copy sits next to `python.exe`, so the loader resolves `vcruntime140.dll` from the already-loaded module. No separate VC++ runtime is needed.
- **RISK (LOW):** if the interpreter folder is cleaned, the runtime is lost. The install must keep the whole Python folder.
- Caveat: this is a string scan, not a full import-table parse. **UNVERIFIABLE-HERE** on 2016.

### 2.3 Every dependency: wheel coverage (uv.lock)

Survey of filenames in `uv.lock` (no install):
- **Runtime packages with a Windows wheel for cp312 (or abi3/pure):** `pydantic-core 2.50.5` (cp312 win_amd64), `pywin32 312` (cp312), `psutil 7.2.2` (win_amd64 plus abi3 set), `cryptography 50.0.2` (win_amd64, cp311-abi3), `orjson 3.13.0` (cp312), `pyyaml 6.0.3` (cp312), `pendulum 3.3.0` (cp312), `sqlalchemy 2.0.58` (cp312, pure fallback), `greenlet 3.5.6` (cp312), `markupsafe`, `regex`, `rpds-py`, `cffi`, `charset-normalizer`, `websockets 16.1.1`, `whenever 0.11.0`, `win32-setctime`.
- **Pure or py3-none-any wheels:** `prefect 3.9.4`, `fastapi 0.150.0`, `starlette 1.10.0`, `uvicorn 0.54.0`, `httpx 0.28.1`, `httpx2 2.16.2`, `anyio 4.16.0`, `loguru 0.8.0`, `tenacity 9.2.1`, `wakeonlan 3.3.0`, `cronsim 2.8`, `cron-descriptor 1.4.5`, `humanize`, `pydantic 2.14.4`, `ruamel-yaml 0.19.1`, `python-multipart 0.0.34`, `filelock`, `certifi`, `sqlalchemy` (pure), `aiosqlite`, `alembic`, `jinja2`, `h11`, `httpcore`.
- **Dev-only (not on the box):** `ruff`, `hypothesis`, `pytest*`, `time-machine` (dev group).
- **Nothing in the runtime set lacks a Windows wheel**, so no MSVC build is needed on the box (CONFIRMED from the lock).
- **OS floors (UNVERIFIABLE-HERE):** Win wheel tags carry no OS version. Per-package floors (psutil, cryptography, pydantic-core, Rust-built wheels) come from each project, not the tag. 2016 is newer than Windows 8.1 and Rust 1.78+ targets Windows 10 / Server 2016 (knowledge). Expect them to run; verify with the smoke test.

### 2.4 Prefect 3.9.4 and SQLite

- Prefect is pure Python. Its DB is SQLite via `aiosqlite` (`sqlalchemy<2.1`). **CONFIRMED** the SQLite library is the one built into the interpreter (3.53.1 here), so no OS SQLite is needed.
- Prefect's Windows-specific deps: `pywin32 312` (cp312 win wheel). The venv imports `win32*` modules.
- UNVERIFIABLE-HERE: Prefect server start on 2016 (the trial box ran it, `SESSION_HANDOFF.md:254`).

### 2.5 TLS and certificate stores (CONFIRMED, LOW risk)

- Python's `ssl` uses **OpenSSL 3.5.9**, bundled (printed on this box). **Not SChannel.** So 2016 needs no OS TLS registry setting for Python.
- `core/notify.py:173-180`: `SMTP_SSL` (port 465) or `SMTP` + `starttls` with `ssl.create_default_context()`. Default context: TLS 1.2 minimum in Python 3.10+ (knowledge).
- Certificates: Python on Windows loads the Windows root store by default (knowledge). A TLS-inspecting proxy (the `check_clock` docstring in `core/health.py` names Sophos) needs its CA in the Windows store. **RISK (LOW)**.
- rclone (Go crypto/tls) uses TLS 1.2+ and no SChannel (knowledge). The trial box ran rclone 1.74.3 (`SESSION_HANDOFF.md:984-986`); a first real-GCS run on 1.76 is not proven (`SESSION_HANDOFF.md:53`).
- PowerShell TLS: 1.2 forced where it downloads (see 1.8). **CONFIRMED.**

### 2.6 IPv6

- Code uses `socket.AF_INET` for the WoL port probe (`core/wol.py:33`), the discovery probe (`core/netinfo.py:31,42`), and SMB port probes (`core/smb_ready.py:48`, `core/lan_preflight.py:43` `create_connection`, which may try IPv6 first on a dual-stack host).
- Dashboard binds `127.0.0.1` (`models/config.py:1024`, `launch.py:30-36`); Prefect API is `http://127.0.0.1:4200/api` (`launch.py:21`). Literal IPv4, so no `localhost`→`::1` ambiguity. **CONFIRMED.**
- RISK (LOW): on a DC with IPv6 enabled, `create_connection(host)` for NAS names may try AAAA first (normal fallback). UNVERIFIABLE-HERE.

### 2.7 Clock and NTP

- Clock check: Google `Date` header first, then `w32tm /stripchart` against `health.ntp_server` (`core/health.py:138-161`, `ARCHITECTURE.md` CL7). **CONFIRMED.**
- Trial-box `w32tm` behaviour (`SESSION_HANDOFF.md:953-955`): rc 0 with `error: 0x800705B4` when the server does not answer; rc 2147953401 for an unknown host.
- DC note (knowledge): on a domain controller the PDC emulator is the authoritative time source for the domain; `w32tm /stripchart` to an external NTP can disagree with domain time. Owner should decide which server is `health.ntp_server`. **RISK (LOW)**.
- W32Time check: `04_check_readiness.ps1:218`. Trial-box lesson: W32Time stopped on the Win11 dev box (`RELEASE_BREAK_PART2`/`SESSION_HANDOFF`), so the check matters.

---

## 3. rclone, robocopy, WoL, shutdown, SMB

### 3.1 robocopy (CONFIRMED on the trial box)

- Trial box: **robocopy 10.0.14393** (`SESSION_HANDOFF.md:985`). That is 2016's build. LAN leg was proven on it 2026-10-20 (`SESSION_HANDOFF.md:254`).
- Flags used (`core/lan_sync.py:416-422`): `/MIR /Z /ZB(opt) /XJ /MT:n /R:n /W:n /V /TS /FP /BYTES /NJH /NDL /NP /XF /XD` and `/UNILOG:` (`lan_sync.py:477`), plus `/L /MIR /XJ` for preflight (`lan_preflight.py:151-158`). All exist on robocopy 10.0.14393. **CONFIRMED** on the trial box for the LAN leg. `/ZB` needs `SeBackupPrivilege` (see 1.6).
- `/UNILOG` writes UTF-16 with BOM and is read back as UTF-16 (`lan_sync.py:154`). **CONFIRMED** on the trial box.
- Exit bitmask and job summary decisions: `core/lan_sync.py` (`decide_lan_result`). Labels are English (1.4 RISK).
- Win 11 24H2+ removed `wmic` only (not robocopy). `09` fixed (`09:75`).

### 3.2 rclone (bundled 1.76.0, trial box 1.74.3)

- Bundled `deploy/bin/rclone.exe`: **v1.76.0** (`rclone version`, `VersionInfo`). PE optional header MinOS **10.0** (read here). Go 1.21+ supports Windows 10 / Server 2016 and newer (knowledge). So 1.76 should run on 2016.
- Trial box ran **v1.74.3** (`SESSION_HANDOFF.md:984`). The handoff notes "rclone 1.76 proven on local remotes only" and the first real GCS run on 1.76 "NOT yet" proven (`SESSION_HANDOFF.md:53`). **RISK (MEDIUM)**: 1.76 on 2016 with real GCS is UNVERIFIABLE-HERE.
- Flags (`core/cloud_sync.py:269-304`): `--config --fast-list --gcs-no-check-bucket --gcs-storage-class --error-on-no-transfer --modify-window --bwlimit --transfers --checkers --retries --retries-sleep --check-first --buffer-size --use-json-log --log-level --stats --max-duration --cutoff-mode SOFT --max-delete`. All are rclone 1.7x flags (knowledge; 1.74.3 proved most on the box, `cloud_sync.py:168` notes the v1.74.3 format). Confirm in smoke test step 6.
- `--use-json-log` and `--log-file` (`cloud_sync.py:289,375`): UTF-8 JSON logs (knowledge). Fine on 2016.

### 3.3 Wake-on-LAN on a multi-homed host (RISK, MEDIUM)

- `core/wol.py:10` imports `wakeonlan.send_magic_packet`; `core/wol.py:69` sends to `255.255.255.255:9`, `:76` sends to the subnet broadcast. No interface or source-IP binding. The OS routing table decides which NIC sends the broadcast.
- Subnet broadcast is derived as the `/24` from `server_ip` (`models/config.py:285-297`) unless `broadcast_address` is set. A DC with two NICs or VLANs can send out the wrong one. Use an explicit `wol.broadcast_address` (the code says so).
- **UNVERIFIABLE-HERE** on 2016.

### 3.4 Remote shutdown of the NAS (RISK, HIGH)

- `core/shutdown.py:76-77`: `shutdown /s /m \\<ip> /t <delay> /f`. **No credentials, no `net use`, no IPC$ session in code.** The call uses the service account's identity.
- Trial test (`SESSION_HANDOFF.md:943-950`): without an admin IPC$ session the call failed with exit 5 "Access is denied"; with `net use \\<ip>\IPC$ /user:Administrator *` it worked. That `net use` was a manual test, not product code.
- `shutdown /m` needs the target to be a Windows machine with the Remote Shutdown RPC. A Linux/Synology NAS cannot take it. Unknown NAS type in the handoff.
- **RISK (HIGH)**: production NAS shutdown is not implemented with credentials. Owner decision needed (store credentials, or a NAS-side shutdown). Do not test against the real NAS without the owner (`MEMORY.md`: NAS shutdown test deferred).

### 3.5 SMB to the NAS

- **No `net use` in product code** (grep: none in `core/`, `flow.py`, `deploy/`). The LAN leg (robocopy) and the probes use the service account's current identity. For a workgroup NAS the account must match an account the NAS accepts, or credentials must be cached. **RISK (HIGH)**.
- DC: the account is `DOMAIN\aambackup`. A non-domain NAS cannot validate it. Needs owner decision.
- SMB1: not used by the product. Server 2016 SMB1 default: UNVERIFIABLE-HERE. Windows 11 24H2+ and Server 2025 remove SMB1 by default (knowledge).
- SMB signing: Server 2016 / 2019 / Win 10: signing is not required for ordinary clients by default (knowledge, see `agent_runs/COMPAT_WINDOWS.md` section 3). Win 11 24H2+ Pro: signing required, guest access blocked (knowledge; `COMPAT_WINDOWS.md` section 3). **RISK**: a guest-only NAS fails on Win 11 24H2 Pro. Not a 2016/2019 issue.
- Readiness check does not yet log `Get-SmbClientConfiguration` (`COMPAT_WINDOWS.md`, fix to consider). **RISK (LOW)**.

---

## 4. Dashboard (`ui.py`, `templates/`, `static/`)

### 4.1 Browser reality on Server 2016 / 2019

- Server 2016 and 2019 ship **Internet Explorer 11** as the default browser and do not install Edge by default (knowledge). **UNVERIFIABLE-HERE.**
- `docs/INSTALL_FRESH.md:179` already says: "The server has only the old Internet Explorer, which cannot show the dashboard." **CONFIRMED** in docs.
- IE Enhanced Security Configuration is on by default for Administrators on Windows Server (knowledge). It blocks scripts from non-trusted sites, including `http://127.0.0.1`. **RISK (HIGH for on-server use)**.

### 4.2 ES features in `static/js` (CONFIRMED)

| Feature | IE11 | Where |
|---|---|---|
| `async function` / `await` (ES2017) | **syntax error: whole script fails** | `dashboard.js:11,118,207`; `settings.js` (35 `await`, 15 `async`) |
| Arrow functions `=>` (ES2015) | no | `dashboard.js:7,8`; `settings.js:49,50,64,67,68,71` |
| `fetch` (no polyfill) | no | `dashboard.js:25,121,209`; `settings.js` (14 calls); `report.js` |
| `Object.values` / `Object.entries` (ES2017) | no | `settings.js` (4 and 1 uses) |
| `String.prototype.padStart` (ES2017) | no | `settings.js:64` |
| `Element.closest` | no | `dashboard.js`, `settings.js` (2) |
| `ChildNode.remove()` | no | `dashboard.js:8`; `settings.js:50` |
| `Array.prototype.includes` | no | `dashboard.js` (1) |
| Spread in calls/objects | no (IE11 has no spread) | `settings.js` (`...d`, 4) |
| `Promise` | IE11 has no native Promise | `settings.js` (1) |
| `localStorage` | IE11 yes | `dashboard.js` (2) |

- Effect on IE11: `dashboard.js` does not parse, so no status, no buttons. **CONFIRMED on the source.** Not run in IE here.
- Edge Legacy (EdgeHTML 18) runs async/await and fetch, but lacks some of the others (`closest` fine). Modern Edge and Chrome are fine.

### 4.3 CSS

- `static/css/dashboard.css`: CSS custom properties `var(--...)` throughout (e.g. `dashboard.css:24`, `settings.css`); IE11 has none. Without them the colours fall back to nothing.
- CSS grid: `dashboard.css:108`, `:129`, `:143` (`display: grid`). IE11 has only `-ms-grid` (partial).
- `gap` in flexbox/grid (`dashboard.css:62,66,68,78,108,129,132,143,149,155,158,164`): not in IE11 or EdgeHTML 15/16, so spacing collapses there.
- `rem` units: fine in IE9+.
- The page also uses `<meta name="viewport">` and `color-scheme` (`templates/dashboard.html:5-6`). Harmless in old browsers.

### 4.4 Serving and binding

- `ui.py` is FastAPI. `launch.py:30-36` runs uvicorn with `host=bind_address` (default `127.0.0.1`, `models/config.py:1024`). Owner on the server uses `http://127.0.0.1:<port>`; another PC needs a non-loopback bind and `dashboard.public_url`.
- Dashboard port: `docs/DEPLOY_RUNBOOK.md:155` uses `localhost:8080`. Confirm `dashboard.port` on the box.
- Windows 10/11 Edge/Chrome: fine (no finding).

---

## 5. Known 2016 / 2019 items already in the docs

From `docs/ARCHITECTURE.md`, `SESSION_HANDOFF.md`, `docs/RELEASE_BREAK_SESSION_2026-11-22*.md`, `agent_runs/COMPAT_WINDOWS.md`:

1. **Target is a domain controller** (`SESSION_HANDOFF.md:89`). Owner must confirm DC and the account (DOMAIN\aambackup). Logon-right GPO unknown.
2. **Trial box was WORKGROUP / local account** (`SESSION_HANDOFF.md:985`). Not the DC configuration.
3. **Clean-VM dry run still open** (`SESSION_HANDOFF.md:100`): Authenticated Users in BUILTIN\Users, SDK zip layout and bundled Python (`CLOUDSDK_PYTHON`), uv Python readable by aambackup, W32Time start type, Get-Credential window, filtered token vs SeBackupPrivilege.
4. **Active Hours** (`SESSION_HANDOFF.md:599`): 18 h was invalid on WS2016; code now writes 20:00-08:00 (`03:163-167`). Verify on box.
5. **sc queryex localization**: fixed in code (`core/services.py:54-80`); RELEASE_BREAK PART2:63 "queued" is outdated.
6. **`wmic`**: removed from `09` (`09:75`); remaining `wmic` mention is a comment.
7. **`-split '\'` PS 5.1 bug** (`deploy/_common.ps1:45`, now `-split '\\'`): found by the live install (`RELEASE_BREAK_PART2:55-56`). Applies to any 5.1 box.
8. **Windows reachable only via shutdown.exe with an IPC$ session** (`SESSION_HANDOFF.md:943-950`). No product code for it (3.4).
9. **w32tm / sc.exe / shutdown.exe output facts** on the trial box (`SESSION_HANDOFF.md:953-960`): error text on stdout/stderr, exit codes as listed.
10. **NSSM stop timing** (`RELEASE_BREAK PART6:11-23`): Prefect stop near 45 s; agent 180 s console window; not reproduced.
11. **Deploy real bugs found on box** (`SESSION_HANDOFF.md:906-912`): `Start-Process -Credential` 0xC0000142 from SSH; uv hard-links into admin cache (`--link-mode copy`); runtime grant needs `/T`.
12. **NAS and LAN outcomes unverified** on 2016/2019: "NOT verifiable here: real Windows Server 2016/2019 runs, real NAS power-off" (`RELEASE_BREAK PART4:35`; `PART5:24`).
13. **Python 3.12 / uv / rclone 2016 floors** (`RELEASE_BREAK PART2:62`): knowledge, not re-checked.
14. **NSSM OS support, per-wheel OS floors, SMB1 defaults**: "NOT provable here" (`RELEASE_BREAK PART2:64`). Unchanged by this review.

---

## 6. Ranked risk list

| # | Risk | Severity | Where | Mark | Action |
|---|---|---|---|---|---|
| 1 | Production is a **domain controller**; logon rights (service, batch, SeBackupPrivilege) may be denied by GPO; trial box was WORKGROUP | HIGH | `SESSION_HANDOFF:89`, `_common.ps1:41-64`, `06:374-382`, `04:372-415`, `permissions.py:83-102` | RISK | Owner confirms DC + account; check GPO before install; smoke steps 9-12 |
| 2 | **NAS access and shutdown have no credential handling**; account identity must be accepted by a non-domain NAS | HIGH | `shutdown.py:76-77`, `lan_sync.py`, `SESSION_HANDOFF:943-950` | RISK | Owner decides credential storage; no NAS power-off test without owner |
| 3 | **Dashboard unusable in IE11** (default on 2016/2019), ESC on server | HIGH (usability) | `static/js/*.js` (async, arrow, fetch), `dashboard.css` vars and grid, `INSTALL_FRESH:179` | CONFIRMED (source), UNVERIFIABLE-HERE (IE run) | Document "use Chrome/Edge on another PC"; or add an IE-safe page; smoke step 20 |
| 4 | **rclone 1.76.0 not run on 2016 with GCS**; trial box ran 1.74.3 | MEDIUM-HIGH | `deploy/bin/rclone.exe` (1.76.0, MinOS 10.0), `SESSION_HANDOFF:53,984` | UNVERIFIABLE-HERE | Cloud dry run on the DC before go-live; smoke step 5-6 |
| 5 | **Locale: robocopy summary labels English-only** (`Files :`, `Ended :`) | MEDIUM (only non-English OS) | `lan_sync.py:71-86` | RISK | Match by column position + the `Ended` line in any language, or refuse non-English OS at readiness |
| 6 | **Locale: icacls principal names English-only** in lock check (fail-open) | MEDIUM | `lock_gcs_key.ps1:48-52` | RISK | Use SIDs (`*S-1-5-32-545`) in the check instead of names |
| 7 | **Console/log cp1252** on en-US 2016: `→` cannot be encoded; no `PYTHONUTF8` | MEDIUM | `flow.py:877`, `core/logging.py:32`, `watchdog.py:178`, `06:372` | CONFIRMED (encode test), UNVERIFIABLE-HERE | Set `PYTHONUTF8=1` in `AppEnvironmentExtra`, or ASCII-only `→` in logs |
| 8 | **`Test-Connection -TimeoutSeconds`** in discovery script: breaks on every PS 5.1 | MEDIUM (tool, not backup path) | `01_server_discovery.ps1:156,172,412` | CONFIRMED BUG | Remove parameter; use `Test-NetConnection -Port` or `-Quiet` alone |
| 9 | **NSSM stop/restart timing** (Prefect 45 s cap, agent 180 s) | MEDIUM | `06:364,371,411`, `PART6:11-23` | RISK | Test the stop chain on 2016 (smoke step 16) |
| 10 | **TEMP / profile for the service account** (tempfile, gcloud) | MEDIUM | `lan_sync.py:370`, `run_artefacts.py:139`, `config_file.py:88`, `rclone_config.py:49` | UNVERIFIABLE-HERE | Confirm `%TEMP%` exists for the account (smoke step 9) |
| 11 | **WoL on multi-homed DC**: no NIC binding; /24 derivation | MEDIUM | `wol.py:69,76`, `models/config.py:285-297` | UNVERIFIABLE-HERE | Set explicit `wol.broadcast_address` and test |
| 12 | **Defender cmdlets** (`Add-MpPreference`, `Get-MpPreference`) silently skipped if Defender absent | MEDIUM | `03:184`, `04:235` | UNVERIFIABLE-HERE | Smoke step 17 |
| 13 | **Python bundled VC runtime**: must keep the whole interpreter folder | LOW | `python312.dll`, `vcruntime140*.dll` (scan) | CONFIRMED | Keep interpreter folder intact in readiness |
| 14 | **Script encoding (BOM-less `.ps1`, cp1252)** | LOW | `06:234` (`§`), `04:346` comment | CONFIRMED | Keep ASCII in scripts or add UTF-8 BOM |
| 15 | **Win 11 24H2 Pro SMB guest/signing** (not a server) | LOW (dev/test) | `COMPAT_WINDOWS.md` section 3 | knowledge | Check if Win 11 Pro is ever used on the NAS path |
| 16 | **Long-path effect** on deep NAS trees | LOW | `03:127`, `04:148` | UNVERIFIABLE-HERE | Smoke step 7 + a deep test tree |
| 17 | **Python TLS/IPv6** | LOW | `notify.py:173-180`, `wol.py:33`, `netinfo.py:31` | CONFIRMED (design) | none needed |

---

## 7. One-page smoke test (run on a real Server 2016 DC right after install)

Run in an **elevated** PowerShell on the server. Replace `<NAS>`, `<share>`, `<acct>` with the real values. Paths assume the trial-box layout (`C:\AAMBackup` code, `C:\BackupAgent` runtime). Do **not** run any shutdown against the NAS. Do not point steps 14-16 at real backup data (use `C:\BackupAgent\smoke`). Expected output is what a pass looks like.

```powershell
# 1. OS and PowerShell 5.1 build
(Get-CimInstance Win32_OperatingSystem).Caption; (Get-CimInstance Win32_OperatingSystem).Version; $PSVersionTable.PSVersion
# Expect: Windows Server 2016 Datacenter (or Standard); 10.0.14393.x; 5.1.14393.x

# 2. Role (DC = 4 or 5)
(Get-CimInstance Win32_ComputerSystem).DomainRole
# Expect: 4 or 5 on the DC (record it; 0-3 means member/standalone, a different install path)

# 3. Interpreter, SSL and SQLite (no OS dependency)
& C:\AAMBackup\.venv\Scripts\python.exe -c "import sys,ssl,sqlite3; print(sys.version.split()[0], ssl.OPENSSL_VERSION, sqlite3.sqlite_version)"
# Expect: 3.12.x  OpenSSL 3.x.x  3.4x.x or newer

# 4. Native wheels import (no VC++ install needed)
& C:\AAMBackup\.venv\Scripts\python.exe -c "import pydantic_core, psutil, cryptography, orjson, yaml, pendulum, sqlalchemy, win32api; print('imports ok')"
# Expect: imports ok

# 5. Bundled binaries
& C:\AAMBackup\deploy\bin\rclone.exe version | Select-Object -First 1; & C:\AAMBackup\deploy\bin\nssm.exe version
# Expect: rclone v1.76.0 ; NSSM 2.24-101-g897c7ad 64-bit

# 6. rclone flags accepted (no backup data touched; --dry-run on a local dir)
$d = 'C:\BackupAgent\smoke\src'; New-Item -ItemType Directory -Force $d | Out-Null; 'x' | Set-Content "$d\a.txt"
& C:\AAMBackup\deploy\bin\rclone.exe sync $d C:\BackupAgent\smoke\dst --dry-run --fast-list --check-first --use-json-log --error-on-no-transfer --max-delete 5 2>&1 | Select-Object -Last 2
# Expect: a JSON log line with "skipped":"copy" or similar; exit code 0 (echo $LASTEXITCODE)

# 7. Long paths
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem').LongPathsEnabled
# Expect: 1

# 8. Clock (W32Time running, sample against the configured NTP)
Get-Service W32Time | Select-Object Name,Status,StartType; w32tm /stripchart /computer:time.windows.com /dataonly /samples:1; $LASTEXITCODE
# Expect: Running / Automatic; a line like "hh:mm:ss, +0.9xxxxxxs" and exit 0 (error 0x800705B4 = no answer, record it)

# 9. Service account exists, profile/TEMP present
Get-CimInstance Win32_Service -Filter "Name='AamBackupAgent'" | Select-Object Name,StartName,State
Test-Path "C:\Users\<acct>\AppData\Local\Temp"
# Expect: StartName = CAAAM\<acct> (or .\<acct>), State Running; Test-Path True (if False, the profile was never created: record it)

# 10. Service logon and backup rights (read-only export)
secedit /export /cfg "$env:TEMP\aam_rights.inf" /areas USER_RIGHTS | Out-Null; Select-String -Path "$env:TEMP\aam_rights.inf" -Pattern 'SeServiceLogonRight|SeBatchLogonRight|SeBackupPrivilege'; Remove-Item "$env:TEMP\aam_rights.inf"
# Expect: the account (or a group it belongs to) on SeServiceLogonRight, SeBatchLogonRight, SeBackupPrivilege

# 11. Services and dependency
sc.exe queryex AamPrefectServer | Select-String STATE; sc.exe queryex AamBackupAgent | Select-String STATE; sc.exe queryex AamWatchdog | Select-String STATE; sc.exe qc AamBackupAgent | Select-String DEPENDENCIES
# Expect: STATE 4 RUNNING (x3); DEPENDENCIES : AamPrefectServer

# 12. Readiness probe as the service account (the real gate; prompts for the account password)
powershell -NoProfile -ExecutionPolicy Bypass -File C:\AAMBackup\deploy\04_check_readiness.ps1 -ServiceAccount CAAAM\<acct>
# Expect: every line [OK]; any [FAIL] names the right that is missing (batch logon, backup right, ACL)

# 13. Health endpoints
Invoke-WebRequest http://127.0.0.1:4200/api/health -UseBasicParsing | Select-Object StatusCode
Invoke-WebRequest http://127.0.0.1:8080/health -UseBasicParsing | Select-Object StatusCode
# Expect: 200 and 200 (8080 = dashboard.port; a 503 "needs attention" on the dashboard is normal before the first backup)

# 14. robocopy version and UNILOG encoding (list-only, nothing copied)
(Get-Item C:\Windows\System32\robocopy.exe).VersionInfo.FileVersion
$s='C:\BackupAgent\smoke\src'; $t='C:\BackupAgent\smoke\dst'; robocopy $s $t /L /MIR /NJH /UNILOG:C:\BackupAgent\smoke\rc.log | Out-Null; $LASTEXITCODE; Get-Content -Encoding Unicode C:\BackupAgent\smoke\rc.log | Select-Object -Last 12
# Expect: 10.0.14393.x; exit 0 or 1; a "Files :" row and an "Ended :" line in the UTF-16 log

# 15. Console encoding (what the services print to)
& C:\AAMBackup\.venv\Scripts\python.exe -c "import sys; print(sys.stdout.encoding)" | Out-String
# Expect: cp1252 on en-US (piped, as NSSM captures it; record it; this is why → can be lost in service logs; see risk 7)

# 16. NAS reachability as the account (no write, no shutdown)
Test-NetConnection -ComputerName <NAS> -Port 445 | Select-Object TcpTestSucceeded; Get-SmbClientConfiguration | Select-Object RequireSecuritySignature,EnableSecuritySignature
# Expect: TcpTestSucceeded True; record the signing settings (Win 2016 default: not required)

# 17. Defender exclusions (if Defender present)
if (Get-Command Get-MpPreference -ErrorAction SilentlyContinue) { (Get-MpPreference).ExclusionPath -contains 'C:\AAMBackup' } else { 'Defender cmdlets absent' }
# Expect: True (or the record "Defender cmdlets absent")

# 18. LAN leg: NAS shutdown needs credentials (do NOT run /s here)
shutdown.exe /? | Select-Object -First 1
# Expect: help text. Real NAS shutdown stays off until the owner decides credentials (risk 2)

# 19. IE11 vs modern browser on the server (dashboard)
(Get-Item "$env:ProgramFiles\Internet Explorer\iexplore.exe").VersionInfo.FileVersion; Test-Path "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
# Expect: 11.x ; False (Edge absent on 2016). Use Chrome/Edge from an office PC for the dashboard (risk 3)

# 20. Stop/start chain, timed (only when the owner says the stack is idle)
Measure-Command { Stop-Service AamWatchdog; Stop-Service AamBackupAgent; Stop-Service AamPrefectServer } | Select-Object TotalSeconds
# Expect: under ~60 s total; if a service is still "Stopping" after 60 s, record it (risk 9)
```

**Record for the owner after the smoke test:** the DC role value, the StartName, the TEMP path result, the `sys.stdout.encoding`, the robocopy and rclone versions, every `[FAIL]` line from step 12, and any "Defender cmdlets absent" line.

---

## 8. Limits of this review

- Nothing ran on Server 2016, 2019, Windows 10 or a DC. Every "UNVERIFIABLE-HERE" is open until the smoke test above runs on the real box.
- The PowerShell parser checks syntax only. Runtime behaviour on 5.1.14393 was not exercised, except the one binding error shown in 1.2 (reproduced on 5.1.26100; the same binding rule applies on 14393).
- The VC runtime finding is a string scan of import names, not a full PE import-table parse.
- Knowledge-based facts are marked "(knowledge)" (Python and Go OS floors, Server 2016 default browser, IE ESC default, Edge absence, NSSM release date, SMB defaults). They were not re-fetched.
- No file in the repo was edited, and no test or service was run.
