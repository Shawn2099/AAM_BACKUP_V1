# Windows Server 2016 — Production Deployment Guide

This step-by-step document guides a systems administrator or technician through deploying **AAM Backup Automation V1** on Windows Server 2016. Follow these exact, sequential instructions for a reliable, production-ready, 365-day maintenance-free setup.

---

## Prerequisites & Verification

Before starting, open a PowerShell terminal and verify system specifications:

| Component | Target Requirement | Verification Command |
|---|---|---|
| **OS** | Windows Server 2016 (1607+) | `[Environment]::OSVersion.Version` (Should be `10.0.14393`+) |
| **Architecture** | 64-bit | `[Environment]::Is64BitOperatingSystem` (Should return `True`) |
| **Network Access** | Outbound access to Google Cloud Storage (port 443) and local LAN SMB destination (port 445) | `Test-NetConnection -ComputerName googleapis.com -Port 443` |

---

## Step 1: Install Python 3.12 (64-bit)

1. Download the **Windows installer (64-bit)** for Python 3.12 from the official downloads page:
   `https://www.python.org/downloads/release/python-3128/`
2. **Crucial:** Run the installer as Administrator. Check both options at the bottom of the first setup screen:
   - [x] **Install launcher for all users (recommended)**
   - [x] **Add python.exe to PATH**
3. Select **Customize installation** -> Click **Next** -> Check **Install for all users** -> Click **Install**.
4. Verify the installation in a **new** PowerShell window:
   ```powershell
   python --version
   # Output must be: Python 3.12.x
   ```

---

## Step 2: Install uv (Python Package Manager)

We use `uv` for fast, reproducible virtual environment and dependency management.

```powershell
pip install uv
uv --version
# Verify output shows the uv version info
```

---

## Step 3: Install rclone (GCS Engine)

1. Download the 64-bit Windows zip from: `https://rclone.org/downloads/`
2. Extract the archive and copy `rclone.exe` to a PATH-accessible folder (such as `C:\Windows\System32`) to make it globally executable:
   ```powershell
   copy-item -Path "C:\path\to\extracted\rclone.exe" -Destination "C:\Windows\System32\rclone.exe" -Force
   ```
3. Verify the installation:
   ```powershell
   rclone version
   # Should display: rclone v1.xx.x
   ```

---

## Step 4: Enable TLS 1.2 on Windows Server 2016

Windows Server 2016 does not enable TLS 1.2 by default for standard system libraries. This is required to download packages and communicate securely with Google Cloud.

Open PowerShell **as Administrator** and execute:

```powershell
# Set TLS 1.2 for current and future sessions
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Wow6432Node\Microsoft\.NetFramework\v4.0.30319' -Name 'SchUseStrongCrypto' -Value 1 -Type DWord
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\.NetFramework\v4.0.30319' -Name 'SchUseStrongCrypto' -Value 1 -Type DWord
```

---

## Step 5: Deploy the Repository

Clone the project repository to `C:\AAM_BACKUP_V1` (this matches the default paths configured in our service scripts):

```powershell
cd C:\
git clone https://github.com/Shawn2099/AAM_BACKUP_V1.git
cd AAM_BACKUP_V1
```

---

## Step 6: Install Project Dependencies

Use `uv` to build the isolated, production virtual environment:

```powershell
uv sync
```

---

## Step 7: Configure the Backup System (`config.yaml`)

Initialize your production configuration file from the template:

```powershell
copy config.example.yaml config.yaml
notepad config.yaml
```

Edit the following key sections to match your client's environment. Leave the remaining settings as their reliable defaults:

```yaml
firm_name: "Client Name Ltd"

paths:
  source_drive: "D:\\"                               # Drive containing database files to back up
  lan_destination: "\\\\192.168.10.10\\backup_share"  # Target UNC network share path
  database_path: "C:\\BackupAgent\\manifest.db"      # Manifest database path
  log_directory: "C:\\BackupAgent\\logs"              # Application runtime log directory
  temp_directory: "C:\\BackupAgent\\rclone_temp"      # Temp work directory for rclone
  gcs_key_path: "C:\\BackupAgent\\gcs-key.json"      # GCS Service Account credential key

wol:
  enabled: true
  mac_address: "AA-BB-CC-DD-EE-FF"                    # MAC address of LAN storage server
  server_ip: "192.168.10.10"                         # IP of LAN storage server

cloud:
  enabled: true
  bucket: "client-gcs-backup-bucket"
  project_number: "123456789012"                      # GCP Project Number
  storage_class: "COLDLINE"                          # Standard, COLDLINE, ARCHIVE, etc.

schedule:
  cloud_cron: "0 18 * * *"                           # Cloud backup daily at 6 PM IST
  lan_cron: "0 1 * * *"                              # LAN backup daily at 1 AM IST
  weekly_cron: "0 8 * * MON"                         # Weekly report every Monday 8 AM
  monthly_cron: "0 8 1 * *"                          # Monthly report on 1st of month 8 AM
  timezone: "Asia/Kolkata"                           # IANA timezone for all schedules

notifications:
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  smtp_username: "backup.reports@gmail.com"
  smtp_password: "xxxx xxxx xxxx xxxx"               # App Password
  sender: "backup.reports@gmail.com"
  recipients:                                        # Multi-recipient list support
    - "shawnanish007@gmail.com"
    - "admin@client.com"
  send_on_failure: true
  send_on_success: false
  weekly_enabled: true                               # Enable/disable weekly report emails
  monthly_enabled: true                              # Enable/disable monthly report emails
  weekly_summary_day: "monday"
  weekly_summary_time: "08:00"

dashboard:
  auth_enabled: true                                # Requires API key to access dashboard
  api_key: "choose-a-strong-password-here"           # !! CHANGE THIS to anything you want

maintenance:
  db_retention_days: 90                              # Retain 90 days of run logs in db
```

---

## Step 8: Initialize Required Directories

Create the operational directories as Administrator:

```powershell
new-item -ItemType Directory -Force -Path "C:\BackupAgent"
new-item -ItemType Directory -Force -Path "C:\BackupAgent\logs"
new-item -ItemType Directory -Force -Path "C:\BackupAgent\rclone_temp"
```

---

## Step 9: Install GCS Service Account Key

Copy the `.json` credential key file retrieved from the Google Cloud Console to the secure configuration directory:

```powershell
copy-item -Path "C:\path\to\your-gcs-key.json" -Destination "C:\BackupAgent\gcs-key.json" -Force
```

---

## Step 10: Service Credentials and SMB Permissions (Critical)

> [!IMPORTANT]
> Mapped network drives (e.g. `X:`) are user-specific resources that **cannot** be accessed by Windows Services. The services must connect to the LAN storage server using the direct UNC path (e.g., `\\192.168.10.10\backup_share`).
>
> By default, services run under the `LocalSystem` account, which does not have network credentials to access authenticated SMB shares. 

To configure network share permissions:
1. Open Windows **Services manager** (`services.msc`).
2. If your network share requires active authentication, you must change the log-on account for **AAM Backup Agent**:
   - Right-click **AamBackupAgent** -> Select **Properties**.
   - Select the **Log On** tab.
   - Choose **This account** and fill in the active Administrator username and password (e.g. `DOMAIN\Administrator`, `.\Administrator`, or `.\LocalBackupUser`) that has full read/write permissions on the target network share.
   - Click **Apply** and restart the service.
   - *Note: If the service hangs or gets stuck in a `STOP_PENDING` transition state during this restart, refer to the **Stuck Services (STOP_PENDING)** section in the Troubleshooting guide below to clear the process cleanly.*

---

## Step 11: Validate the System Clock

Google Cloud Storage OAuth token authentication enforces tight security. System clocks with **more than 10 minutes** of drift from UTC time will fail GCS auth checks.

Verify and sync the server clock:

```powershell
# Get local time and sync
net start w32time
w32tm /resync
```

---

## Step 12: Manual Pre-Flight Testing

Before deploying in the background, run the manual scripts to confirm correct configuration:

1. Launch the Prefect server in one PowerShell terminal:
   ```powershell
   cd C:\AAM_BACKUP_V1
   .\start_server.bat
   ```
2. Launch the application in a second PowerShell terminal:
   ```powershell
   cd C:\AAM_BACKUP_V1
   .\start.bat
   ```
3. Open your browser:
   - Navigate to `http://localhost:8080` (Dashboard UI)
   - Navigate to `http://localhost:4200` (Prefect UI)
4. On the dashboard, trigger a manual dry-run/backup. Verify that the operations complete successfully.

---

## Step 13: Install Production Services via NSSM

For 24/7 reliability, automated self-healing, and service restart on boot, the application runs as three Windows Services managed by NSSM:

1. **AamPrefectServer:** Prefect server backend (API + DB).
2. **AamBackupAgent:** Dashboard UI and cron schedules.
3. **AamWatchdog:** Self-healing service sentinel that monitors API health.

> [!NOTE]
> **No external download required:** NSSM (v2.24-101) is pre-bundled inside the codebase under `deploy/bin/nssm.exe` so the installation can be completed 100% offline.

### 1. Configure Project Path (Optional)
Open `C:\AAM_BACKUP_V1\deploy\install_services.bat` in a text editor. Ensure that the `PROJECT_DIR` variable accurately matches your path:
- `PROJECT_DIR`: Path to the cloned project folder (default: `C:\AAM_BACKUP_V1` or `C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1`)

### 2. Run the Service Installer
Execute the installation batch file from an elevated **Administrator PowerShell** window:

```powershell
cd C:\AAM_BACKUP_V1
.\deploy\install_services.bat
```

This installer script will automatically:
- Create the system directories and redirect log outputs.
- Register all 3 services in the Windows Service Control Manager (SCM).
- Configure stdout/stderr log redirection with 10MB auto-rotation.
- Set up automatic crash recovery (services auto-restart on crash).
- Start all three services in the correct sequence automatically.

---

## Step 14: Reboot and Verify

1. **Reboot the Windows Server:**
   ```powershell
   shutdown /r /t 0
   ```
2. **Verify Services Status:**
   - Press `Win + R`, type `services.msc`, and press Enter.
   - Verify that **AAM Prefect Server**, **AAM Backup Agent**, and **AAM Backup Watchdog** are all listed and showing **Running** as status.
3. **Verify Web Dashboards:**
   - Open your browser and navigate to the dashboard: `http://localhost:8080` (or the configured bind address and port).
   - Click the **"Email Weekly Report"** button to verify that email notifications are working perfectly for all configured recipients.

---

## Troubleshooting & Maintenance

### 📝 Checking Logs
All service standard output and error streams are captured and auto-rotated in `C:\BackupAgent\logs\`:
- `agent_svc.log`: Main dashboard UI and task scheduling logs.
- `prefect_svc.log`: Prefect API server logs.
- `watchdog_svc.log`: Service monitoring and self-healing log.

### 🚫 Port Conflict (4200 or 8080)
If the dashboard or Prefect server fails to start, verify if another process is occupying the port:

```powershell
netstat -ano | findstr :8080
netstat -ano | findstr :4200
```

### 🔑 Resetting Corrupted Prefect DB
If the database gets corrupted during a server power outage, reset the Prefect database:

```powershell
cd C:\AAM_BACKUP_V1
uv run prefect server database reset -y
```

### 🛑 Stuck Services (STOP_PENDING)
When changing logon credentials, restarting the server, or stopping the services, a service (typically **AamBackupAgent**) might occasionally get stuck in a `STOP_PENDING` state. This happens if the underlying Python worker or subprocesses take too long to close active connections or exit cleanly.

To resolve this and force a clean restart:
1. **Identify the Stuck Service Process PID:**
   Open PowerShell or Command Prompt as Administrator and query the extended status of the backup agent:
   ```powershell
   sc queryex AamBackupAgent
   ```
   Find and note the `PID` value in the printed output.
2. **Terminate the Stuck Wrapper Process:**
   Forcefully terminate the stuck NSSM process using its PID (e.g., if the PID is `1320`):
   ```powershell
   taskkill /F /PID 1320
   ```
3. **Verify and Restart:**
   Verify that the service has transitioned to `STOPPED` and then start it normally:
   ```powershell
   sc query AamBackupAgent
   net start AamBackupAgent
   ```
