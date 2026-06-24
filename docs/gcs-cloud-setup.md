# GCS Cloud Setup — From Scratch to Production

Complete provisioning guide for the Google Cloud Storage backend used by AAM Backup Automation.

**Project:** `aam-backup-2026`
**Bucket:** `aam-cloudbackup` (fallback: `aam-cloudbackup-vault`)
**Region:** `asia-south1` (Mumbai)
**Service Account:** `aam-cloudbackup@aam-backup-2026.iam.gserviceaccount.com`

---

## Storage Lifecycle Requirements

These are the design requirements the GCS architecture satisfies:

| Requirement | How it's met |
|-------------|-------------|
| Current versions stay in STANDARD | rclone writes with `storage_class: STANDARD` (`rclone_config.py:42`) |
| Only 1 non-current version retained | Lifecycle rule `numNewerVersions: 2` deletes versions with 2+ newer |
| Non-current in COLDLINE for 90 days only | Noncurrent STANDARD → COLDLINE after 1 day; deleted after 90 days noncurrent |
| After FY ends → old FY → ARCHIVE | `fy_rollover.py` runs `gcloud storage objects update --storage-class=ARCHIVE --recursive` |
| ARCHIVE fallback (zero maintenance) | Lifecycle rule `age: 365` auto-archives anything the gcloud command missed |
| Incomplete uploads cleaned up | Lifecycle rule `AbortIncompleteMultipartUpload` at 7 days |
| Backup points to new FY folder | `fy_rollover.py` updates `config.yaml` source_drive/lan_destination; rclone syncs to new prefix |
| All data in same bucket | Single bucket `gs://aam-cloudbackup/` with FY prefixes |
| IAM rules in place | Service account (Object Admin) + human Admins/Viewers |

---

## Object Lifecycle — Visual Flow

```
                            DAILY BACKUP (rclone sync)
                            ═══════════════════════════

  Source PC                              GCS Bucket
  ─────────                              ──────────
  D:\FY26-27\Accounts\file.xlsx   ──→   gs://aam-cloudbackup/FY26-27/Accounts/file.xlsx
                                         │
                                         │  storage_class = STANDARD
                                         │  versioning = enabled
                                         ▼
                              ┌──────────────────────────┐
                              │      CURRENT VERSION      │
                              │      (STANDARD class)     │
                              └──────────┬───────────────┘
                                         │
                          Overwrite or delete
                                         │
                                         ▼
                              ┌──────────────────────────┐
                              │  1st NONCURRENT VERSION   │
                              │  (STANDARD for 1 day)     │
                              │  → COLDLINE after 1 day   │
                              │  → DELETED after 90 days  │
                              └──────────────────────────┘


                            FY ROLLOVER (April 1)
                            ═════════════════════

  1. Final backup: source → gs://aam-cloudbackup/FY25-26/ (last sync to old prefix)
  2. Archive transition: gcloud rewrites all FY25-26/* to ARCHIVE class (metadata-only)
  3. Create new FY folders: D:\FY26-27\ and \\NAS\share\FY26-27\
  4. Update config.yaml: source_drive and lan_destination point to FY26-27
  5. Next daily backup: rclone syncs to gs://aam-cloudbackup/FY26-27/ (STANDARD)

  Result in bucket:
    gs://aam-cloudbackup/
    ├── FY25-26/          ← ARCHIVE class (old year, will expire per lifecycle)
    └── FY26-27/          ← STANDARD class (current year, active backups)

  Safety net (zero maintenance):
    If gcloud fails or is skipped, lifecycle rule age:365
    auto-archives any object older than 365 days to ARCHIVE.
```

---

## Lifecycle Rules Explained

File: `deploy/gcs_lifecycle.json`

```json
{
  "rule": [
    {
      "action": {"type": "SetStorageClass", "storageClass": "COLDLINE"},
      "condition": {"daysSinceNoncurrentTime": 1, "matchesStorageClass": ["STANDARD"]}
    },
    {
      "action": {"type": "Delete"},
      "condition": {"numNewerVersions": 2}
    },
    {
      "action": {"type": "Delete"},
      "condition": {"daysSinceNoncurrentTime": 90}
    },
    {
      "action": {"type": "SetStorageClass", "storageClass": "ARCHIVE"},
      "condition": {"age": 365}
    },
    {
      "action": {"type": "AbortIncompleteMultipartUpload"},
      "condition": {"age": 7}
    }
  ]
}
```

### Rule 1: Noncurrent STANDARD → COLDLINE (after 1 day)

When a file is overwritten or deleted, GCS creates a noncurrent version. If that noncurrent version is still in STANDARD class and has been noncurrent for 1+ days, GCS automatically demotes it to COLDLINE (cheaper storage).

**Why:** Keeps the noncurrent version accessible but at lower cost. COLDLINE has a 90-day minimum storage charge, so deleting before 90 days would incur a penalty — the rules are tuned to match.

### Rule 2: Keep only 1 noncurrent version (`numNewerVersions: 2`)

GCS counts how many versions are newer than each object. If 2+ versions are newer, the oldest is deleted.

| Version | Newer versions | Action |
|---------|---------------|--------|
| Current (v3) | 0 | Kept |
| Noncurrent v2 | 1 (v3) | Kept |
| Noncurrent v1 | 2 (v3, v2) | **Deleted** |

**Result:** Exactly 1 noncurrent version retained at any time.

### Rule 3: Delete noncurrent after 90 days

Any object that has been noncurrent for 90+ days is deleted, regardless of storage class. This prevents indefinite accumulation of old versions.

### Rule 4: ARCHIVE fallback for objects older than 365 days

**Purpose:** Zero-maintenance safety net. If the gcloud archive transition at FY rollover fails or is skipped, this rule automatically moves any object older than 365 days to ARCHIVE.

**How it works:**
- Primary mechanism: `fy_rollover.py` runs `gcloud storage objects update --storage-class=ARCHIVE --recursive` at rollover — immediate, precise, per-FY-prefix
- Fallback: This lifecycle rule catches anything the gcloud command missed — no manual intervention needed
- No `matchesPrefix` required — covers all FY prefixes automatically without annual updates
- Current FY data is always < 365 days old (rollover happens annually) — rule never triggers on active data
- Noncurrent versions are already deleted by Rule 3 (90 days) before reaching 365 days — no conflict

**If gcloud already ran:** Lifecycle rule attempts ARCHIVE on already-ARCHIVE objects → no-op, no double-charging.

### Rule 5: Abort incomplete multipart uploads (after 7 days)

If rclone crashes or a sync is interrupted mid-upload, incomplete multipart uploads can linger and incur storage costs. This rule cleans them up after 7 days.

**Why 7 days:** Generous window for transient network issues to resolve, but short enough to prevent cost buildup from stuck uploads.

### Combined Timeline for a Single File

```
Day 0:   file.xlsx saved (v1)              → STANDARD, current
Day 5:   file.xlsx overwritten (v2)        → v1 becomes noncurrent (STANDARD)
Day 6:   lifecycle moves v1 to COLDLINE    → noncurrent 1 day, demoted
Day 10:  file.xlsx overwritten (v3)        → v2 becomes noncurrent (STANDARD)
Day 11:  lifecycle moves v2 to COLDLINE    → noncurrent 1 day, demoted
         v1 deleted by numNewerVersions: 2  → only 1 noncurrent retained
Day 96:  v2 deleted by 90-day rule         → noncurrent 90 days reached
```

### FY Rollover + Lifecycle Interaction

When the FY rollover runs `gcloud storage objects update --storage-class=ARCHIVE`:

- All objects under `gs://bucket/FY25-26/` are rewritten to ARCHIVE class (metadata-only, no re-upload)
- The lifecycle rules still apply to these objects
- The 90-day noncurrent deletion rule continues to work on ARCHIVE objects
- No double-charging: if the gcloud command already moved objects to ARCHIVE, Rule 4 is a no-op
- If the gcloud command fails or is skipped, Rule 4 archives the data after 365 days — zero manual intervention

---

## Prerequisites

Before running any CLI commands:

1. **Billing** — Log into the new Google Cloud account via the web console, navigate to Billing, attach a payment method, and link it to the project.
2. **APIs** — Enable these APIs via the console search bar (fresh projects have them disabled by default):
   - Cloud Storage API
   - Identity and Access Management (IAM) API
3. **gcloud CLI** — Installed and on PATH. Download from [cloud.google.com/sdk](https://cloud.google.com/sdk/docs/install).

---

## Step 1: Authenticate and Set Project

```bash
gcloud auth login
gcloud config set project aam-backup-2026
```

## Step 2: Create the Storage Bucket

Bucket names must be **entirely lowercase**. If `aam-cloudbackup` is taken globally, use `aam-cloudbackup-vault`.

```bash
gcloud storage buckets create gs://aam-cloudbackup \
  --location=asia-south1 \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access
```

## Step 3: Enable Object Versioning

**Critical:** Without versioning, lifecycle rules (`numNewerVersions`, `daysSinceNoncurrentTime`) will not trigger. Deleted/overwritten files are permanently lost instead of becoming noncurrent.

```bash
gcloud storage buckets update gs://aam-cloudbackup --versioning
```

## Step 4: Strip the Default Soft Delete Policy

Removes the default 7-day retention to prevent overlapping storage charges on noncurrent file versions.

```bash
gcloud storage buckets update gs://aam-cloudbackup --clear-soft-delete-policy
```

## Step 5: Apply Object Lifecycle Management

Apply the lifecycle rules that automate COLDLINE tiering, version cleanup, 90-day expiration, and ARCHIVE fallback. See [Lifecycle Rules Explained](#lifecycle-rules-explained) above for full details.

```bash
gcloud storage buckets update gs://aam-cloudbackup \
  --lifecycle-file=deploy/gcs_lifecycle.json
```

**Requires versioning to be enabled (Step 3).**

## Step 6: Provision the Service Account

```bash
# Create the service account
gcloud iam service-accounts create aam-cloudbackup \
  --description="Automated backup agent for sync and archiving" \
  --display-name="AAM CloudBackup Agent"

# Grant Object Admin on the bucket
gcloud storage buckets add-iam-policy-binding gs://aam-cloudbackup \
  --member="serviceAccount:aam-cloudbackup@aam-backup-2026.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

# Generate the JSON key file (saves to current local directory)
gcloud iam service-accounts keys create gcs-private-key.json \
  --iam-account="aam-cloudbackup@aam-backup-2026.iam.gserviceaccount.com"
```

**Post-step:** Move `gcs-private-key.json` to `deploy/keys/aam-gcs-key.json` on the Windows server. Set restrictive file permissions — only the backup service user should read it.

## Step 7: Grant Client Access (Human Users)

Replace placeholder emails with actual addresses.

**Admins** — Storage Object Admin (can manage files):
```bash
gcloud storage buckets add-iam-policy-binding gs://aam-cloudbackup \
  --member="user:admin1_placeholder@gmail.com" \
  --role="roles/storage.objectAdmin"

gcloud storage buckets add-iam-policy-binding gs://aam-cloudbackup \
  --member="user:admin2_placeholder@gmail.com" \
  --role="roles/storage.objectAdmin"
```

**Viewers** — Storage Object Viewer (read-only):
```bash
gcloud storage buckets add-iam-policy-binding gs://aam-cloudbackup \
  --member="user:viewer1_placeholder@gmail.com" \
  --role="roles/storage.objectViewer"

gcloud storage buckets add-iam-policy-binding gs://aam-cloudbackup \
  --member="user:viewer2_placeholder@gmail.com" \
  --role="roles/storage.objectViewer"
```

## Step 8: Configure Billing Alerts

Since you're putting your own card on the account temporarily:

1. Navigate to **Billing > Budgets & alerts** in the Google Cloud Console.
2. Create a budget for the `aam-backup-2026` project.
3. Set a low target (e.g., $10) and configure email alerts at **50%, 90%, and 100%** of threshold.

This prevents runaway costs if a script loops or uploads unexpected data during early testing.

---

## Post-Provisioning: Windows Server Setup

After the GCS resources are created above, complete the deployment on the Windows server:

1. Copy `gcs-private-key.json` to `deploy\keys\aam-gcs-key.json`
2. Update `config.yaml` with the cloud section:
   ```yaml
   cloud:
     enabled: true
     bucket: "aam-cloudbackup"
     project_number: "aam-backup-2026"    # or numeric project ID
     location: "asia-south1"
     storage_class: "STANDARD"
     bandwidth_limit: "10M"
     retry_count: 3
     subprocess_timeout_seconds: 21600    # 6 hours (increase to 259200 for first sync)
     transfers: 2
     checkers: 4
     buffer_size: "64M"
   paths:
     gcs_key_path: "C:\\AAM_BACKUP_V1\\deploy\\keys\\aam-gcs-key.json"
   ```
3. Validate config: `deploy\test_config.bat`
4. Install services: `powershell -ExecutionPolicy Bypass -File deploy\install_services.ps1` (Run as Admin)

See `DEPLOYMENT_GUIDE.md` for full Windows server installation steps.

---

## Config.yaml Reference — Cloud Section

| Key | Value | Notes |
|-----|-------|-------|
| `cloud.enabled` | `true` | Set `false` to disable cloud sync entirely |
| `cloud.bucket` | `aam-cloudbackup` | GCS bucket name (lowercase, no `gs://` prefix) |
| `cloud.project_number` | `920173882190` | Google Cloud project number |
| `cloud.location` | `asia-south1` | Must match bucket region |
| `cloud.storage_class` | `STANDARD` | For daily uploads; ARCHIVE applied at FY rollover |
| `cloud.bandwidth_limit` | `10M` | rclone `--bwlimit` (10M = 10 MB/s) |
| `cloud.subprocess_timeout_seconds` | `21600` | 6 hours; set to `259200` (72h) for first large sync |
| `cloud transfers` | `2` | Concurrent file uploads |
| `cloud.checkers` | `4` | Concurrent file comparison workers |
| `cloud.buffer_size` | `64M` | Upload read buffer per transfer slot |

---

## Automated Setup Scripts

Interactive setup scripts exist in `deploy/` that automate Steps 2–6:

| Script | Platform | Run with |
|--------|----------|----------|
| `deploy/setup_gcs.sh` | Linux / Git Bash | `bash deploy/setup_gcs.sh` |
| `deploy/setup_gcs.ps1` | Windows PowerShell | `powershell -ExecutionPolicy Bypass -File deploy/setup_gcs.ps1` |

Both scripts:
- Read defaults from `config.yaml` (bucket, project, location)
- Prompt for each value with defaults
- Create bucket, enable versioning, clear soft delete, apply lifecycle rules
- Create service account, bind IAM role, generate key file
- Print the `config.yaml` snippets to paste

---

## Cost Notes (asia-south1)

| Item | Detail |
|------|--------|
| Storage (STANDARD) | ~$0.020/GB/month |
| Storage (COLDLINE) | ~$0.010/GB/month (90-day minimum charge) |
| Storage (ARCHIVE) | ~$0.004/GB/month (365-day minimum charge) |
| Class A operations (writes) | ~$0.005 per 10,000 |
| Class B operations (reads) | ~$0.001 per 10,000 |
| Data egress | Free within same region |

rclone with `--fast-list` and `--check-first` flags bundles API calls, keeping operational costs low.

---

## Architecture Summary

```
Windows Server (source)
  │
  ├── Robocopy ──→ NAS (LAN backup)
  │
  ├── rclone sync ──→ GCS bucket (gs://aam-cloudbackup)
  │     ├── writes to: gs://aam-cloudbackup/{FY prefix}/
  │     ├── storage class: STANDARD (current year)
  │     ├── uses: deploy/keys/aam-gcs-key.json
  │     └── writes temp rclone config with service_account_file
  │
  └── gcloud ──→ GCS (FY rollover archive)
        ├── rewrites: gs://aam-cloudbackup/{old FY}/* → ARCHIVE class
        └── uses: GOOGLE_APPLICATION_CREDENTIALS env var
```

**Key files involved:**

| File | Role |
|------|------|
| `config.yaml` | Bucket name, project, key path, storage class, timeouts |
| `deploy/keys/aam-gcs-key.json` | Service account JSON key (never commit to git) |
| `deploy/gcs_lifecycle.json` | Lifecycle rules (COLDLINE tiering, version retention, expiry, ARCHIVE fallback) |
| `core/rclone_config.py` | Writes temp rclone config with GCS credentials |
| `core/cloud_sync.py` | Executes `rclone sync` source → GCS |
| `core/fy_rollover.py` | Detects FY boundary, runs final backup, transitions to ARCHIVE, updates config |
| `core/health.py` | Pre-backup health checks including GCS key validation |
