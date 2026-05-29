# Windows Server 2016 Development & Compatibility Learnings

This document summarizes critical Windows-specific gotchas, architectural patterns, and solutions identified during active testing on Windows Server 2016. Keep these in mind when developing on Linux to ensure the core remains robust and cross-platform.

---

## 1. Subprocess Execution & Headless Spawning
When spawning background tasks (like `rclone` or `robocopy`) from an in-process daemon (like Uvicorn/FastAPI or Prefect) on Windows, spawning processes without explicit flags can cause black console windows to pop up on the desktop.
* **Linux:** Processes spawn silently in the background.
* **Windows Solution:** Always pass the `CREATE_NO_WINDOW` creation flag to headless subprocesses:
  ```python
  import subprocess
  import sys
  
  creationflags = 0
  if sys.platform == "win32":
      creationflags = subprocess.CREATE_NO_WINDOW  # value is 0x08000000
      
  subprocess.Popen(cmd, creationflags=creationflags, ...)
  ```

---

## 2. Robocopy Exit Code Bitmasks (Non-Standard)
Robocopy does not adhere to the POSIX exit-code standard (where `0` is success and any non-zero code is an error). Instead, it uses a bitmask:
* **0 (0x00):** No files copied; source and destination are already in sync.
* **1 (0x01):** One or more files were copied successfully.
* **2 (0x02):** Extra files/directories were found in destination (not present in source).
* **4 (0x04):** Mismatched files were detected and overwritten.
* **8 (0x08):** Some files failed to copy (copy errors occurred).
* **16 (0x10):** Serious/fatal error (e.g., path unreachable or permission denied).

### Key Takeaway for Cross-Platform Devs:
Any exit code from `0` to `7` is a **successful sync** in Robocopy. Standard Linux shell wrappers checking for `returncode == 0` will incorrectly mark successful syncs as failed! Always use a bitmask classifier:
```python
def classify_exit_code(code: int) -> str:
    if code & 16:
        return "FAILED"
    if code & 8:
        return "PARTIAL"
    if 0 <= code <= 7:
        return "COMPLETE"
    return "FAILED"
```

---

## 3. Network & Wake-on-LAN (SMB Port Checks vs. Ping)
Pinging the backup server to verify it has woken up is a common trap:
1. ICMP ping is frequently blocked by local firewalls on Windows Server.
2. The `ping` command utility uses incompatible command flags on Linux (`ping -c 1`) vs. Windows (`ping -n 1`).

### The Robust Solution:
Instead of pinging, check for SMB port (`445`) availability directly using standard TCP socket connections. This is 100% cross-platform, firewalled-friendly (since SMB must be open for the backup to work anyway), and extremely fast:
```python
import socket

def is_smb_port_open(ip: str, port: int = 445, timeout: float = 3.0) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
```

---

## 4. SQLite Constraint Migrations (Legacy DB Files)
If a user already has an existing `manifest.db` database file on disk and you push a code update that changes the DDL (e.g., adding a `UNIQUE` constraint to a column like `run_id` to support `ON CONFLICT(run_id) DO UPDATE`), SQLite's `CREATE TABLE IF NOT EXISTS` **will silently ignore the new table DDL** because the table already exists.
* This leads to immediate database crashes during runtime, throwing:
  `ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint`
  
### The Robust Solution:
Instead of forcing a complex migration or deleting customer databases, explicitly register a `UNIQUE INDEX` in your DDL:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_run_history_run_id ON run_history(run_id);
```
SQLite automatically treats a unique index as a valid unique constraint target for `ON CONFLICT(run_id)` operations, fixing legacy databases dynamically without data loss!

---

## 5. Absolute Path Normalization
Always use Python's `pathlib.Path` to normalize paths. Windows uses backslashes (`\`) and drive letters (e.g., `E:\`), while Linux uses forward slashes (`/`).
* Never manually concatenate paths with `+ "/" +` or hardcode path separators.
* Convert paths to strings using `str(path)` when passing to subprocess tools like `rclone` or `robocopy` to avoid serialization errors.

---

## 6. Execution under Windows Task Scheduler
When running Python scripts via the Windows Task Scheduler (e.g., `start.bat` at system startup):
1. **User context:** Task Scheduler often runs under `SYSTEM` or a special service account. These accounts do not inherit standard user environment paths.
2. **Current Directory:** Always change the working directory explicitly to the script's own folder inside the batch script (`cd /d "%~dp0"`) to avoid executing inside `C:\Windows\System32`.
3. **PATH configuration:** Explicitly prefix paths to standard tools if they are not in the global SYSTEM Path.
