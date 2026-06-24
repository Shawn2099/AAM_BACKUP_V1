# AAM Backup Automation - GCS Bucket Setup PowerShell Script
# Run as: powershell.exe -ExecutionPolicy Bypass -File setup_gcs.ps1
# Requires PowerShell 3.0+ (Windows Server 2016 ships with PowerShell 5.1 - OK)
$ErrorActionPreference = "Stop"

# Resolve directories — $PSScriptRoot is always set in PS 3.0+ (avoids empty path bug)
$scriptDir = $PSScriptRoot
if ([string]::IsNullOrEmpty($scriptDir)) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if ([string]::IsNullOrEmpty($scriptDir)) { $scriptDir = (Get-Location).Path }
$workspaceDir = Split-Path $scriptDir -Parent
$configFile = Join-Path $workspaceDir "config.yaml"
$lifecycleFile = Join-Path $scriptDir "gcs_lifecycle.json"

Write-Host "========================================================="
Write-Host "       AAM BACKUP AUTOMATION - GCS BUCKET SETUP"
Write-Host "========================================================="

# 1. Verify gcloud is installed
$gcloudCheck = Get-Command gcloud -ErrorAction SilentlyContinue
if (!$gcloudCheck) {
    Write-Error "Error: 'gcloud' CLI is not installed or not in PATH."
    Write-Host "Please install the Google Cloud SDK from:"
    Write-Host "  https://cloud.google.com/sdk/docs/install"
    exit 1
}

# 2. Check gcloud auth
Write-Host "Checking active Google Cloud account..."
$activeAccount = gcloud config get-value account 2>$null
if ([string]::IsNullOrEmpty($activeAccount)) {
    Write-Host "No active account found. Please log in..."
    gcloud auth login
}

# 3. Read default values from config.yaml
$defaultBucket = "aam-backup-bucket"
$defaultProjectNum = ""
$defaultLocation = "asia-south1"

if (Test-Path $configFile) {
    Write-Host "Reading defaults from config.yaml..."
    $content = Get-Content $configFile
    foreach ($line in $content) {
        if ($line -match '^\s*bucket:\s*["'']?([^"''\s#]+)["'']?') {
            $defaultBucket = $Matches[1]
        }
        if ($line -match '^\s*project_number:\s*["'']?([^"''\s#]+)["'']?') {
            $defaultProjectNum = $Matches[1]
        }
        if ($line -match '^\s*location:\s*["'']?([^"''\s#]+)["'']?') {
            $defaultLocation = $Matches[1]
        }
    }
}

# 4. Prompt for settings
$bucketName = Read-Host "GCS Bucket Name to create/configure [$defaultBucket]"
if ([string]::IsNullOrEmpty($bucketName)) { $bucketName = $defaultBucket }

$location = Read-Host "GCS Location/Region [$defaultLocation]"
if ([string]::IsNullOrEmpty($location)) { $location = $defaultLocation }

# Try to resolve Project ID from Project Number or active config
$defaultProjectId = ""
if (![string]::IsNullOrEmpty($defaultProjectNum)) {
    Write-Host "Attempting to resolve Project ID for project number $defaultProjectNum..."
    $resolvedId = gcloud projects list --filter="projectNumber=$defaultProjectNum" --format="value(projectId)" 2>$null
    if ($resolvedId) {
        $defaultProjectId = $resolvedId.Trim()
    }
}

if ([string]::IsNullOrEmpty($defaultProjectId)) {
    $activeProj = gcloud config get-value project 2>$null
    if ($activeProj) {
        $defaultProjectId = $activeProj.Trim()
    }
}

$projectId = Read-Host "Google Cloud Project ID [$defaultProjectId]"
if ([string]::IsNullOrEmpty($projectId)) { $projectId = $defaultProjectId }

if ([string]::IsNullOrEmpty($projectId)) {
    Write-Error "Error: Project ID is required to configure GCS resources."
    exit 1
}

$adminEmail = Read-Host "Admin Email [$activeAccount]"
if ([string]::IsNullOrEmpty($adminEmail)) { $adminEmail = $activeAccount }

$viewer1 = Read-Host "Viewer 1 Email [viewer1@example.com]"
if ([string]::IsNullOrEmpty($viewer1)) { $viewer1 = "viewer1@example.com" }

$viewer2 = Read-Host "Viewer 2 Email [viewer2@example.com]"
if ([string]::IsNullOrEmpty($viewer2)) { $viewer2 = "viewer2@example.com" }

$viewer3 = Read-Host "Viewer 3 Email [viewer3@example.com]"
if ([string]::IsNullOrEmpty($viewer3)) { $viewer3 = "viewer3@example.com" }

Write-Host "Using Project ID: $projectId"
Write-Host "Using GCS Bucket: $bucketName"
Write-Host "Using Region:     $location"
Write-Host "---------------------------------------------------------"

# 5. Check/Create Bucket
Write-Host "Checking if bucket gs://$bucketName exists..."
$bucketExists = $false
try {
    $null = gcloud storage buckets describe "gs://$bucketName" --project="$projectId" 2>$null
    $bucketExists = $true
} catch {
    # If not found or access error
}

if ($bucketExists) {
    Write-Host "Bucket gs://$bucketName already exists. Skipping creation."
} else {
    Write-Host "Bucket gs://$bucketName does not exist. Creating..."
    gcloud storage buckets create "gs://$bucketName" `
        --project="$projectId" `
        --location="$location" `
        --default-storage-class=STANDARD
    Write-Host "Bucket gs://$bucketName created successfully."
}

# 6. Enable Versioning
Write-Host "Enabling Object Versioning on gs://$bucketName..."
gcloud storage buckets update "gs://$bucketName" --versioning

# 7. Clear Soft Delete Policy
Write-Host "Disabling Soft Delete policy (clearing soft delete) on gs://$bucketName to save costs..."
gcloud storage buckets update "gs://$bucketName" --clear-soft-delete

# 7b. Enable Uniform Bucket-Level Access
Write-Host "Enabling Uniform Bucket-Level Access on gs://$bucketName..."
gcloud storage buckets update "gs://$bucketName" --uniform-bucket-level-access

# 8. Apply Lifecycle configuration
if (Test-Path $lifecycleFile) {
    Write-Host "Applying lifecycle rules from $lifecycleFile to gs://$bucketName..."
    gcloud storage buckets update "gs://$bucketName" --lifecycle-file="$lifecycleFile"
} else {
    Write-Host "Warning: Lifecycle file $lifecycleFile not found. Skipping lifecycle configuration."
}

# 9. Configure Service Account
$saName = "aam-backup-agent"
$saEmail = "$saName@$projectId.iam.gserviceaccount.com"

Write-Host "Checking if service account $saEmail exists..."
$saExists = $false
try {
    $null = gcloud iam service-accounts describe "$saEmail" --project="$projectId" 2>$null
    $saExists = $true
} catch {}

if ($saExists) {
    Write-Host "Service account $saEmail already exists."
} else {
    Write-Host "Creating service account $saName..."
    gcloud iam service-accounts create "$saName" `
        --project="$projectId" `
        --description="AAM Backup Agent Service Account" `
        --display-name="AAM Backup Agent"
}

# 10. Bind IAM Role
Write-Host "Granting roles/storage.objectAdmin on gs://$bucketName to service account..."
gcloud storage buckets add-iam-policy-binding "gs://$bucketName" `
    --member="serviceAccount:$saEmail" `
    --role="roles/storage.objectAdmin"

# 11. Bind Admin and Viewer Roles
Write-Host "Configuring Admin and Viewer access on gs://$bucketName..."

if ($adminEmail -match "@") {
    Write-Host "Granting roles/storage.admin to $adminEmail..."
    gcloud storage buckets add-iam-policy-binding "gs://$bucketName" `
        --member="user:$adminEmail" `
        --role="roles/storage.admin"
}

$viewers = @($viewer1, $viewer2, $viewer3)
foreach ($viewer in $viewers) {
    if ($viewer -notmatch "^viewer" -and $viewer -match "@") {
        Write-Host "Granting roles/storage.objectViewer to $viewer..."
        gcloud storage buckets add-iam-policy-binding "gs://$bucketName" `
            --member="user:$viewer" `
            --role="roles/storage.objectViewer"
    } elseif ($viewer -match "^viewer") {
        Write-Host "Skipping placeholder viewer: $viewer"
    }
}

# 12. Generate Key File
$keyDir = Join-Path $scriptDir "keys"
if (!(Test-Path $keyDir)) {
    New-Item -ItemType Directory -Path $keyDir -Force | Out-Null
}
$keyFile = Join-Path $keyDir "aam-gcs-key.json"

if (Test-Path $keyFile) {
    $timestamp = Get-Date -Format "yyyyMMddHHmmss"
    $backupKey = "$keyFile.bak.$timestamp"
    Write-Host "Warning: Service account key already exists at $keyFile."
    Write-Host "Backing up existing key to $backupKey"
    Rename-Item -Path $keyFile -NewName (Split-Path $backupKey -Leaf)
}

Write-Host "Creating new service account key at $keyFile..."
gcloud iam service-accounts keys create "$keyFile" `
    --iam-account="$saEmail" `
    --project="$projectId"

Write-Host "========================================================="
Write-Host "       GCS BUCKET SETUP COMPLETE SUCCESSFULLY!"
Write-Host "========================================================="
Write-Host "Service Account: $saEmail"
Write-Host "Key file saved to: $keyFile"
Write-Host ""
Write-Host "ACTION REQUIRED: Update your config.yaml with the following:"
Write-Host "  paths:"
Write-Host "    gcs_key_path: `"$keyFile`""
Write-Host "  cloud:"
Write-Host "    bucket: `"$bucketName`""
Write-Host "    project_number: `"$projectId`""
Write-Host "========================================================="
