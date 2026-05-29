# Windows Server 2016 — Production Deployment Guide

Step-by-step guide for deploying AAM Backup Automation V1 on Windows Server 2016.

---

## Prerequisites

| Requirement | Version | Check Command |
|---|---|---|
| Windows Server 2016 | 1607+ | `winver` |
| Python | 3.12+ | `python --version` |
| uv | Latest | `uv --version` |
| rclone | 1.74.2+ | `rclone version` |
| Network access | GCS + LAN share | Manual |

---

## Step 1: Install Python 3.12

Download from https://www.python.org/downloads/

```powershell
# Verify
python --version
# Should show: Python 3.12.x
```

## Step 2: Install uv

```powershell
pip install uv
uv --version
```

## Step 3: Install rclone

Download from https://rclone.org/downloads/

```powershell
# Place in a PATH-accessible location
copy rclone.exe C:\Windows\System32\rclone.exe

# Verify
rclone version
```

## Step 4: Enable TLS 1.2

Windows Server 2016 doesn't enable TLS 1.2 by default. Required for GCS and PyPI.

```powershell
# Run once (persists across reboots)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
```

## Step 5: Clone the Repository

```powershell
cd C:\
git clone https://github.com/Shawn2099/AAM_BACKUP_V1.git
cd AAM_BACKUP_V1
```

## Step 6: Install Dependencies

```powershell
uv sync
```

## Step 7: Create Configuration

```powershell
copy config.example.yaml config.yaml
notepad config.yaml
```

Edit these fields (leave the rest as defaults):

```yaml
firm_name: "Your Firm Name"

paths:
  source_drive: "D:\"
  lan_destination: "\\YOUR_BACKUP_SERVER_IP\backup_share"
  database_path: "C:\BackupAgent\manifest.db"
  log_directory: "C:\BackupAgent\logs"
  temp_directory: "C:\BackupAgent\rclone_temp"
  gcs_key_path: "C:\BackupAgent\gcs-key.json"

wol:
  mac_address: "AA-BB-CC-DD-EE-FF"    # Backup server MAC
  server_ip: "192.168.10.10"           # Backup server IP

cloud:
  bucket: "your-gcs-bucket"
  project_number: "000000000000"       # GCS project number

dashboard:
  api_key: "generate-a-random-key"     # For dashboard login

schedule:
  timezone: "Asia/Kolkata"             # Your timezone
```

## Step 8: Create Directories

```powershell
mkdir C:\BackupAgent
mkdir C:\BackupAgent\logs
mkdir C:\BackupAgent\rclone_temp
```

## Step 9: Copy GCS Key File

The GCS key is gitignored (*.json). Copy it manually:

```powershell
copy gcs-key.json C:\BackupAgent\gcs-key.json
```

## Step 10: Map Network Drive (for LAN backup)

```powershell
net use X: \\YOUR_BACKUP_SERVER_IP\backup_share /persistent:yes
```

**Important:** This must be done from the interactive desktop session, not SSH. The mapped drive inherits the user's network credentials.

## Step 11: Verify System Clock

GCS JWT auth rejects tokens with >10 min clock skew.

```powershell
Get-Date
# Compare with real time. If off:

net start w32time
w32tm /resync
```

## Step 12: Test Manually

```powershell
cd C:\AAM_BACKUP_V1

# Start Prefect server (in one terminal)
start_server.bat

# Start app (in another terminal)
start.bat
```

Open http://localhost:8080 — dashboard should load.
Open http://localhost:4200 — Prefect UI should load.

Test a manual backup trigger from the dashboard.

## Step 13: Set Up Task Scheduler (Production)

Two scheduled tasks for automatic startup:

### Task 1: Prefect Server

1. Open **Task Scheduler** (taskschd.msc)
2. Click **Create Task** (not "Create Basic Task")
3. **General tab:**
   - Name: `AAM Prefect Server`
   - Select: `Run whether user is logged on or not`
   - Check: `Run with highest privileges`
4. **Triggers tab:**
   - New → `At system startup`
   - Delay task for: `10 seconds`
5. **Actions tab:**
   - New → `Start a program`
   - Program: `C:\AAM_BACKUP_V1\start_server.bat`
   - Start in: `C:\AAM_BACKUP_V1`
6. **Settings tab:**
   - Uncheck: `Stop the task if it runs longer than`
   - Check: `If the task fails, restart every: 1 minute`
   - Attempt restart up to: `3 times`
7. Click **OK** → Enter Administrator password

### Task 2: Backup App

1. Click **Create Task** again
2. **General tab:**
   - Name: `AAM Backup App`
   - Select: `Run whether user is logged on or not`
   - Check: `Run with highest privileges`
3. **Triggers tab:**
   - New → `At system startup`
   - Delay task for: `60 seconds` (after Prefect server is ready)
4. **Actions tab:**
   - New → `Start a program`
   - Program: `C:\AAM_BACKUP_V1\start.bat`
   - Start in: `C:\AAM_BACKUP_V1`
5. **Settings tab:**
   - Uncheck: `Stop the task if it runs longer than`
   - Check: `If the task fails, restart every: 1 minute`
   - Attempt restart up to: `3 times`
6. Click **OK** → Enter Administrator password

## Step 14: Reboot and Verify

```powershell
# Reboot the server
shutdown /r /t 60

# After reboot, check both services are running:
# 1. Open http://localhost:4200 — Prefect UI
# 2. Open http://localhost:8080 — Dashboard
# 3. Check Task Scheduler → Task Status → Active Tasks
```

---

## Troubleshooting

### Prefect server not starting

```powershell
# Check if port 4200 is in use
netstat -an | findstr 4200

# Check Prefect data directory
dir %USERPROFILE%\.prefect

# Reset Prefect database if corrupted
uv run prefect server database reset -y
```

### Dashboard not loading

```powershell
# Check if port 8080 is in use
netstat -an | findstr 8080

# Check config.yaml is valid
uv run python -c "from models.config import load_config; load_config('config.yaml')"
```

### Cloud backup failing (GCS auth)

```powershell
# Check clock
Get-Date

# Check rclone can access GCS
rclone lsd aam_gcs: --config C:\BackupAgent\rclone_temp\test.conf

# Check GCS key file exists
dir C:\BackupAgent\gcs-key.json
```

### LAN backup failing (network access)

```powershell
# Check network drive is mapped
net use

# Check SMB port is open
Test-NetConnection -ComputerName YOUR_BACKUP_SERVER_IP -Port 445

# Re-map if needed
net use X: \\YOUR_BACKUP_SERVER_IP\backup_share /persistent:yes
```

### Task Scheduler tasks not running

```powershell
# Check task status
schtasks /query /tn "AAM Prefect Server" /v
schtasks /query /tn "AAM Backup App" /v

# Check task history (if enabled)
# Task Scheduler → Task Scheduler Library → AAM Prefect Server → History
```

---

## File Checklist

After deployment, verify these files exist:

| File | Purpose |
|---|---|
| `C:\AAM_BACKUP_V1\config.yaml` | Configuration (from config.example.yaml) |
| `C:\AAM_BACKUP_V1\start_server.bat` | Starts Prefect server |
| `C:\AAM_BACKUP_V1\start.bat` | Starts dashboard + scheduler |
| `C:\BackupAgent\gcs-key.json` | GCS service account key |
| `C:\BackupAgent\manifest.db` | Created on first run |
| `C:\BackupAgent\logs\` | Log files (auto-created) |
| `C:\BackupAgent\rclone_temp\` | Temp files (auto-created) |
