# Install on a clean Windows Server 2016 (first install)

For the person at the server. Follow it top to bottom; every step says what you should see. If a step does not show
that, **stop**. Steps 1-6 change almost nothing; step 7 changes system settings; the services are only installed in step 11. To UPDATE an existing install use
`DEPLOY_RUNBOOK.md` instead. Plan and reasons: `docs/PLAN_FRESH_INSTALL_2026-10-23.md`.

| Placeholder | Meaning | Example |
|---|---|---|
| `C:\AAMBackup` | where the code lives | |
| `C:\BackupAgent` | runtime folder (logs, database), created by the checks | |
| `C:\BackupAgent\config\config.yaml` | the settings file: NOT in the code folder (that one is locked) | |
| `aambackup` | the backup service account | your choice |
| `D:\FY26-27` | the CURRENT financial-year folder to back up | |

**Needed:** internet on the server during step 8 (downloads `uv`, Python 3.12 and the Google Cloud SDK), the NAS
switched on from step 10 on, and about 2 hours the first time.

---

## A. Before the day (not on the server)

### A1. Google Cloud (on any PC with the Google Cloud CLI, or in the web console)
Replace `YOUR-BUCKET`, `YOUR-PROJECT`, `YOUR-SA` with the client's names. The bucket must use uniform bucket-level access.

```cmd
gcloud auth login
gcloud config set project YOUR-PROJECT
gcloud storage buckets create gs://YOUR-BUCKET --location=asia-south1 --default-storage-class=STANDARD --uniform-bucket-level-access
gcloud storage buckets update gs://YOUR-BUCKET --versioning
gcloud storage buckets update gs://YOUR-BUCKET --clear-soft-delete
gcloud storage buckets update gs://YOUR-BUCKET --lifecycle-file=gcs_lifecycle.json
gcloud iam service-accounts create YOUR-SA --display-name="Backup agent"
gcloud storage buckets add-iam-policy-binding gs://YOUR-BUCKET --member="serviceAccount:YOUR-SA@YOUR-PROJECT.iam.gserviceaccount.com" --role="roles/storage.objectAdmin"
gcloud iam service-accounts keys create aam-gcs-key.json --iam-account="YOUR-SA@YOUR-PROJECT.iam.gserviceaccount.com"
```
(`gcs_lifecycle.json` is `deploy\gcs_lifecycle.json` from the code; copy it next to where you run the commands.) Versioning must be ON and the lifecycle file applied (without them, deleted files are lost for good). Note down the
bucket name, the project NUMBER (Console > project info) and the region. Bring only `aam-gcs-key.json` (USB); do not
leave other copies of it anywhere.

### A2. NAS
1. Create a user with the **same name and the same password** as the server's backup account (`aambackup`, step 5), with
   *Modify* rights on the backup share. (The server is in a WORKGROUP: Windows logs on to the NAS with this name + password.)
2. On the share create the current year folder (`FY26-27`) and **inside it an empty file named `.AAM_TARGET_MOUNTED`**
   (do it here, logged in at the NAS: the first backup refuses an empty year folder without this marker).
3. Allow that user to shut the NAS down remotely and open its firewall for it. **Do not test shutdown now** (see step 15).
4. Wake-on-LAN: enable it in the NAS BIOS and network card. Write down the NAS **MAC address** and **IP address**.
5. Keep account lock-out OFF on the NAS until the install is green.

### A3. Mail
Have ready: SMTP host, port, security (STARTTLS / SSL), the mail user name, its password, the sender address, the
addresses for **alerts** (problems) and for **reports** (weekly / monthly).

### A4. The code
On the development PC: `git bundle create aam.bundle mvp-hardening` (or `git archive --format=zip -o aam.zip mvp-hardening`)
and copy it to a USB stick together with the **Git for Windows installer** (the bundle route needs git on the server).
Do not use the GitHub copy: `origin/mvp-hardening` is old.

---

## B. On the server

### 1. Open an Administrator PowerShell
Start menu > type `PowerShell` > right-click > *Run as administrator*. Every command below is typed there. A script
that says "must run in an ADMINISTRATOR PowerShell" was started in the wrong window.

### 2. Time zone and clock (a clean Server 2016 is on Pacific time!)
```powershell
tzutil /s "India Standard Time"
Set-Service W32Time -StartupType Automatic
Start-Service W32Time
w32tm /resync
tzutil /g
```
Should print `India Standard Time`. (Another country: set that zone and the same one in `schedule.timezone`, step 9.)

### 3. Nothing else may be running here
Task Manager shows no `robocopy.exe` / `rclone.exe`. No Windows Update restart is pending.

### 4. The source folder
`D:\FY26-27` (the CURRENT financial year, April-March) must exist and contain **at least one file**. Leave the older year
folders (`D:\FY24-25`, `D:\archives\FY10-11`) where they are: the system backs up only the current year, the old ones are
uploaded once by the separate bulk-upload tool (step 16).

### 5. The backup account
```powershell
$p = Read-Host -AsSecureString "Password for the backup account"
New-LocalUser -Name aambackup -Password $p -PasswordNeverExpires -UserMayNotChangePassword
net localgroup "Backup Operators" aambackup /add
```
Use the **same password as on the NAS (A2)**. *Backup Operators* gives the account both the "back up files" right and
"log on as a batch job" that the checks need.

### 6. Install Git and get the code
Install Git for Windows from the USB stick, open a **new** Administrator PowerShell, then:
```powershell
git clone -b mvp-hardening E:\aam.bundle C:\AAMBackup
cd C:\AAMBackup
git log -1 --oneline
```
(`E:\aam.bundle` = where the USB stick is.) *Zip instead of git:* extract to `C:\AAMBackup`, then
`Get-ChildItem -Recurse C:\AAMBackup | Unblock-File`.

### 7. Run the system setup (internet needed)
```powershell
cmd /c C:\AAMBackup\deploy\03_setup_system.bat
```
It installs `uv` and Python 3.12 (for all users), turns on long paths, sets Windows Update Active Hours, adds Defender
exclusions, unpacks the Google Cloud SDK, **locks the code folder** (only Administrators and SYSTEM may change it) and
prints the Windows time zone. It ends with "Press any key". Wanted: `SYSTEM SETUP COMPLETE`, no `[ERROR]` and no "was unexpected at this time". Then **close the window and open a NEW
Administrator PowerShell** (the new settings are only visible in new windows).

### 8. Put the Google key in place
```powershell
copy E:\aam-gcs-key.json C:\AAMBackup\deploy\keys\aam-gcs-key.json
```

### 9. Create `config.yaml` (outside the code folder)
```powershell
cd C:\AAMBackup
mkdir C:\BackupAgent\config
copy deploy\config.install.yaml C:\BackupAgent\config\config.yaml
notepad C:\BackupAgent\config\config.yaml
```
(The code folder is read-only for the backup account on purpose; the settings file needs a folder the account may write to. If Notepad shows everything on one line, use WordPad.)
Fill in every line marked `<-- FILL IN`: firm name, **source folder** `D:\FY26-27`, **NAS folder** `\\<NAS-IP>\<share>\FY26-27`
(same year folder), the bucket, project number, region, mail settings and both address lists. Paths are in single quotes,
type them as Windows shows them. Also replace the example NAS address `192.0.2.10` in BOTH `lan_destination` and `wol.server_ip` (the checks refuse the example). Set `smtp_auth: false` only for an internal mail relay that needs no login. Leave `shutdown_after_backup: false` and `wol.enabled: false` for now. Do **not** type
passwords into the file. The source and the NAS folder must end in the **same** year folder.

### 10. Readiness check
The NAS must be switched on. First run (it builds the Python environment; takes a few minutes):
```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\04_check_readiness.ps1 -ServiceAccount .\aambackup
```
It first asks (if your mail server needs a login) for the **mail password** (typed twice, hidden; stored encrypted), then a window asks for the `aambackup` password (the probe runs AS that account). At the end it sends **one test mail** to both address lists. Wanted: the last
line `>>> SUCCESS` (green), or `PASSED WITH n WARNING(S)`. Yellow `[WARN]` lines are things that are NOT proven; read each one. Every `[FAIL]` names the fix:

| `[FAIL]` says | Fix |
|---|---|
| `Time zone` | step 2 |
| `Google key` | step 8, or `paths.gcs_key_path` in the config |
| `Financial year` | `source_drive` / `lan_destination` must be the CURRENT year's folder (April-March) |
| `Wake-on-LAN` | `wol.server_ip` must be the same NAS address as in `lan_destination`; no example values |
| `Config validation` | the message shows which line of `config.yaml` is wrong |
| `Dashboard link in emails` | `dashboard.bind_address` must be `0.0.0.0` (or the server's office address) |
| `as .\aambackup ... Access is denied` | A2.1: same user name + password on the NAS, Modify on the share |
| `as .\aambackup ... backup privilege` | step 5 (Backup Operators), then run the check again |
| `as .\aambackup ... exit 267011` | step 5: the account lacks "log on as a batch job" (Backup Operators has it) |
| `Test mail` | the mail settings or the password. To type the password again: `.venv\Scripts\python.exe deploy\set_mail_password.py --config C:\BackupAgent\config\config.yaml`; then run the check again (add `-SkipTestMail` to not send another mail while fixing something else) |
| `W32Time Service` | `Set-Service W32Time -StartupType Automatic; Start-Service W32Time` |
| `Rights for .\aambackup` / `as .\aambackup ... config folder` | `C:\BackupAgent\config` must exist (step 9); run the check again, it grants the rights |
| `Base Python location ... user profile` | delete `C:\AAMBackup\.venv`, run `cmd /c deploy\03_setup_system.bat`, open a new window, run this check again |

### 10b. Let the yearly archive step find Python
```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\07_configure_gcloud.ps1
```
(Without it the 1 April move of the closed year to the cheaper storage class silently does nothing.)

### 11. Install the services
```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\06_install_services.ps1 -ServiceAccount .\aambackup
```
It asks for the **two dashboard passwords** first (the ordinary one for everybody, and the Advanced one for the owner/IT; they must differ, at least 10 characters each, typed twice, hidden), then for the `aambackup` password. It re-checks the config
first and stops **without touching the services** if it is invalid. Wanted: `INSTALLATION COMPLETE (3 services running,
Prefect API and dashboard answered)`. If it prints `INSTALL FAILED`, read the lines under it and the logs in
`C:\BackupAgent\logs`, fix, run it again.

### 12. Check it from an office PC
The server has only the old Internet Explorer, which cannot show the dashboard. On an office PC with Chrome or Edge open
`http://<server address>:8080`, sign in. A red banner "no backup yet" is true and normal now. Also open Settings and press
**Send test email**.

### 13. First backups (daytime, owner present)
From the dashboard on the office PC use the backup buttons for the cloud and the NAS backup. (The Prefect screen on the server does not work in the old Internet Explorer.) Or, on the server:
```powershell
$env:PREFECT_API_URL='http://127.0.0.1:4200/api'; $env:PREFECT_HOME='C:\BackupAgent\.prefect'
.venv\Scripts\prefect.exe deployment run 'backup/backup-cloud'
.venv\Scripts\prefect.exe deployment run 'backup/backup-lan'
```
A long first cloud upload holds the backup slot: the 21:00 NAS run of that day then ends `LAN_SLOT_TIMEOUT`. Start it after the NAS run, or accept one missed NAS night.
- `backup-cloud`: expect `CLOUD_PARTIAL` ("continues next run") or `CLOUD_COMPLETE`. That proves the key, bucket and rights.
  A big first upload spreads over nights, or run it once with `-p budget_hours=48` added to the command over a weekend.
- `backup-lan`: expect `LAN_COMPLETE`. It needs the `.AAM_TARGET_MOUNTED` marker from A2.2; without it you get
  `LAN_BLOCKED` and an approval card on the dashboard.

### 14. Windows Update
Active Hours are set 20:00-08:00 (reboots can only happen in the day). Decide the reboot window with the client.

### 15. Wake-on-LAN and NAS shutdown (ONLY with the owner standing at the NAS)
Fill in `wol.mac_address`, `wol.server_ip` (the same address as the NAS folder) and set `wol.enabled: true`, run the check
(step 10, with `-SkipTestMail`) again, switch the NAS off by hand, run `backup-lan` and watch it wake. Only when that works set
`lan.shutdown_after_backup: true` (Settings > Advanced or `config.yaml`) and test the real shutdown with the owner at the NAS.

### 16. Older years (once, separate tool)
`D:\FY24-25`, `D:\archives\FY10-11` etc. are NOT part of the nightly backups. They are uploaded once, to their own cloud
folders, by the bulk-upload tool (`docs/PLAN_BULK_UPLOAD`, to be built). Never copy them into the current year's folder.

### 17. Acceptance (tick when seen)
- [ ] `04_check_readiness.ps1` all green as `aambackup`; test mail arrived on both lists
- [ ] `INSTALLATION COMPLETE`; dashboard opens from an office PC
- [ ] `backup-cloud` PARTIAL/COMPLETE; `backup-lan` LAN_COMPLETE
- [ ] Forgotten dashboard password: `powershell -ExecutionPolicy Bypass -File deploy\11_reset_dashboard_passwords.ps1` works (as administrator)
- [ ] Restore test: `deploy\09_restore_from_gcs.bat` restores a sample folder and the files open
- [ ] Reboot the server: the three services come back by themselves (`services.msc`)
- [ ] 7 nights in a row with truthful results
- [ ] NAS wake-up + shutdown test done with the owner at the NAS

---

## If something goes wrong
- A script printed red text: fix that line, run the same script again (they can be repeated safely).
- `INSTALL FAILED` or a service does not start: `C:\BackupAgent\logs\agent_svc.log`, `prefect_svc.log`, `watchdog_svc.log`.
- Start over completely: `cmd /c deploy\08_uninstall_services.bat`, delete `C:\BackupAgent` and `C:\AAMBackup\.venv`, begin at step 7.
- Passwords forgotten: `powershell -ExecutionPolicy Bypass -File deploy\11_reset_dashboard_passwords.ps1` (as administrator).
