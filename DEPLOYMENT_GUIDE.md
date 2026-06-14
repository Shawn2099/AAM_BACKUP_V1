# AAM Backup Automation – Deployment Guide

This guide covers the complete, end-to-end installation process for deploying the AAM Backup Automation system on a Windows Server or PC.

---

## Folder Layout (After Setup)

```
AAM_BACKUP_V1\
├── deploy\
│   ├── bin\
│   │   ├── nssm.exe          ← auto-downloaded by install_services.bat
│   │   └── rclone.exe        ← place here manually (see Prerequisites)
│   ├── keys\
│   │   └── aam-gcs-key.json  ← place your GCS service account key here
│   ├── install_services.bat
│   ├── restart_services.bat
│   ├── uninstall_services.bat
│   ├── test_config.bat
│   └── test_config.py
├── config.yaml
├── collect_config_data.py
└── ...
```

> **Note:** `deploy\bin\` is checked first for all binaries before system PATH.
> You do **not** need to add rclone to Windows system PATH.

---

## Prerequisites

### 1. Install `uv` (Python Package Manager)
Open PowerShell and run:
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
*(uv manages Python 3.12 and all dependencies automatically — no separate Python install needed.)*

### 2. Place `rclone.exe` in `deploy\bin\`
- Download the Windows 64-bit ZIP from [rclone.org/downloads](https://rclone.org/downloads/).
- Extract and copy **only** `rclone.exe` into `AAM_BACKUP_V1\deploy\bin\`.
- The system's `resolve_binary()` checks `deploy\bin\` first — **no system PATH change required**.

### 3. Place the GCS Service Account Key in `deploy\keys\`
- Go to **Google Cloud Console → IAM & Admin → Service Accounts**.
- Select your service account → **Keys** → **Add Key → JSON**.
- Save the downloaded `.json` file as `deploy\keys\aam-gcs-key.json`.
- In `config.yaml`, set:
  ```yaml
  paths:
    gcs_key_path: "C:\\AAM_BACKUP_V1\\deploy\\keys\\aam-gcs-key.json"
  ```

### 4. Verify GCS Bucket Permissions
The service account must have the **Storage Object Admin** role on your GCS bucket:
- GCS Console → **Bucket → Permissions → Grant Access**
- Principal: your service account email
- Role: `Storage Object Admin`

### 5. Install `gcloud` CLI (Required for FY Rollover Archive)
At end-of-year rollover, the system moves the closing FY's GCS data to ARCHIVE storage class using `gcloud`. Without it, rollover still completes but the archive transition is skipped.
- Download from [cloud.google.com/sdk](https://cloud.google.com/sdk/docs/install).
- Run the installer and let it add `gcloud` to system PATH.
- No login needed — the system authenticates using the service account key.

---

## Step 0: Run Server Discovery (Target Environment Check)

Before configuring or installing anything, run the discovery script to ensure the server meets all requirements and to collect critical configuration data (like paths, network interfaces, and MAC addresses).

1. Copy the entire `AAM_BACKUP_V1` folder to your target Windows Server.
2. Navigate to the `deploy\` folder.
3. **Right-click `server_discovery.bat`** → **"Run as Administrator"**.
4. Wait for the script to complete (it takes 1-2 minutes).
5. Open the generated `server_discovery_report.md` file. It contains your IP addresses, Wake-on-LAN adapter status, UAC mode, open ports, storage info, and any critical warnings (like missing permissions, proxy settings, or blocked Google Cloud access).
6. Send the `.md` and `.json` reports to the deployment team, or use the collected info in the following steps to configure the system.

---

## Step 1: Create the Initial FY Folders

> The automation creates FY folders **only during rollover** (April 1st). For the **very first deployment**, you must create them manually.

### On the Source PC (D: drive)
```
D:\
└── FY26-27\          ← create this folder manually
    ├── Accounts\
    ├── HR\
    └── ...           ← place your current year's working data here
```

### On the LAN Target (NAS/Server)
Create the matching folder on the network share:
```
\\NAS_IP\share\FY26-27\    ← create this folder on the NAS
```
The service account user (set in Step 5 below) must have **Read/Write** access to this share.

### For Old FY Data (Historical Migration)
Move old years to a separate folder **outside** the automation path so they are never touched:
```
D:\
├── _OLD_FY_DATA\
│   ├── FY23-24\
│   ├── FY24-25\
│   └── FY25-26\
└── FY26-27\          ← automation source_drive
```
Upload old data to GCS manually using rclone (one-time):
```cmd
deploy\bin\rclone.exe copy "D:\_OLD_FY_DATA\FY25-26" gcs:your-bucket/FY25-26 --progress
```
Then manually set those GCS folders to ARCHIVE class via GCS Console.

---

## Step 2: Gather Hardware & Network Details

Run the built-in configuration collector to get your network MAC addresses, IP addresses, and current FY folder name — all pre-formatted for `config.yaml`.

```cmd
uv run python collect_config_data.py
```

Keep this window open. It will display **Copy-Paste Ready YAML snippets** verified by Pydantic.

---

## Step 3: Set up `config.yaml`

Open `config.yaml` and update the following sections using the snippets from Step 2:

| Key | What to set |
|-----|-------------|
| `paths.source_drive` | Local data path, e.g. `D:\\FY26-27` |
| `paths.lan_destination` | UNC path, e.g. `\\\\192.168.1.100\\share\\FY26-27` |
| `paths.gcs_key_path` | Full path to your `.json` key, e.g. `C:\\AAM_BACKUP_V1\\deploy\\keys\\aam-gcs-key.json` |
| `paths.database_path` | SQLite manifest path, e.g. `C:\\BackupAgent\\manifest.db` |
| `wol.mac_address` | MAC address of the NAS/target server |
| `wol.server_ip` | IP address of the NAS/target server |
| `wol.broadcast_address` | **(Optional)** Subnet broadcast IP (e.g., `192.168.1.255`). Leave empty to auto-derive from `server_ip`. |
| `dashboard.bind_address` | LAN IP of this (source) machine |

> **Fiscal Year Rule:** `source_drive` and `lan_destination` must end with the **same FY folder** (e.g. both end with `FY26-27`). A mismatch will be rejected at validation.

---

## Step 4: Validate Your Configuration

Before installing any Windows Services, run the validator:

1. Double-click **`deploy\test_config.bat`**.
2. A console window will open and run your `config.yaml` through the full Pydantic schema.
3. If it prints `✅ SUCCESS`, proceed. If it prints `❌ ERROR`, fix the issue and re-run.

---

## Step 5: Install Windows Services

1. Navigate to the `deploy\` folder.
2. **Right-click `install_services.bat`** → **"Run as Administrator"**.
3. The script automatically:
   - Downloads `nssm.exe` to `deploy\bin\` if missing.
   - Installs **AamPrefectServer** (Prefect API on port 4200).
   - Installs **AamBackupAgent** (Dashboard on port 8080 + backup worker).
   - Installs **AamWatchdog** (service health monitor).
   - Starts all three in the correct dependency order.

---

## Step 6: Critical Post-Install Actions

### 6a. Set the Service Log On User (LAN Backup — REQUIRED)
**CRITICAL:** Windows services default to `Local System`, which **cannot access network UNC paths** (`\\NAS_IP\share`). Robocopy will fail with "Access Denied" if this is not changed.

1. Open Start → type `services.msc` → Enter.
2. Right-click **AamBackupAgent** → **Properties** → **Log On** tab.
3. Select **This account**.
4. Enter the credentials of a Windows/Domain user with **Read/Write access** to the LAN share.
5. Click **OK**.
6. Repeat the exact same steps for **AamWatchdog** to ensure it can access necessary network paths.

### 6b. Open Windows Firewall for Dashboard (if accessing from another PC)
By default, Windows Firewall blocks external access to port 8080. Run in an elevated Command Prompt:
```cmd
netsh advfirewall firewall add rule name="AAM Backup Dashboard" dir=in action=allow protocol=TCP localport=8080
```

### 6c. Configure Timeout for Initial Cloud Sync (IMPORTANT)
If this is the very first time syncing to GCS and you have a large dataset, the default 6-hour timeout might kill the upload prematurely.
1. Open `config.yaml`.
2. Temporarily set `subprocess_timeout_seconds: 259200` (72 hours).
3. Restart the `AamBackupAgent` service (see 6d).
4. After the initial sync finishes successfully, change it back to `21600` (6 hours) and restart the service again for daily operation.

### 6d. Restart Services to Apply Changes
After changing the Log On user or editing `config.yaml`:
- Right-click **`deploy\restart_services.bat`** → **"Run as Administrator"**.

---

## Step 7: Verify Everything is Working

| Check | How |
|-------|-----|
| Dashboard UI | Open `http://<bind_address>:8080` in a browser |
| Prefect UI | Open `http://localhost:4200` in a browser |
| Service status | Open `services.msc` — all 3 services should show **Running** |
| Logs | `C:\BackupAgent\logs\` — check `agent_svc.log` for errors |

---

## Future FY Rollovers (Automatic)

On **April 1st** each year, the system automatically:
1. Runs a final backup of the closing FY to both LAN and GCS.
2. Creates the new FY folder on source and LAN (e.g. `FY27-28\`).
3. Updates `config.yaml` to point to the new folders.
4. Transitions the old GCS folder to ARCHIVE storage class (requires `gcloud` on PATH).

**No manual action is required for future rollovers.**

> **Note on NAS Availability:** If the NAS is offline during rollover, the system handles it gracefully. It will log an `ACTION REQUIRED` error, proceed with the rollover of the source folder and GCS, and update `config.yaml`. You will just need to manually create the new FY folder on the NAS once it comes back online.

---

## Troubleshooting & Built-in Safeties

*   **Silent Failures Blocked:** The system runs a rigid health check before any backup starts. If GCS keys are missing, the configuration is broken, or the system clock is skewed by >10 minutes, the backup **will halt immediately** with a critical error rather than proceeding and risking data corruption.
*   **LAN Sync Anomalies:** Standard sync logs only show "Failed" or "Complete." This system monitors `robocopy` closely — if it returns an exit code between 4 and 7 (indicating file mismatches or uncopied extra files), the backup marks the phase as **PARTIAL** requiring manual review, preventing silent data mismatches.
*   **Dual-Broadcast WoL:** If your NAS sits behind a managed switch or on a separate VLAN, global WoL broadcasts (`255.255.255.255`) are often dropped. The system sends magic packets to both the global broadcast and the auto-derived subnet broadcast (configurable via `wol.broadcast_address`) to ensure maximum delivery reliability.

---

## Uninstallation

1. Right-click **`deploy\uninstall_services.bat`** → **"Run as Administrator"**.
2. This stops and removes all 3 services and kills any orphaned background processes.
3. Delete the project folder manually after uninstallation.
