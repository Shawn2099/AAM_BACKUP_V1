#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

# Resolve the directory of the script to handle relative paths correctly
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
WORKSPACE_DIR="$( cd "$SCRIPT_DIR/.." &> /dev/null && pwd )"
CONFIG_FILE="$WORKSPACE_DIR/config.yaml"
LIFECYCLE_FILE="$SCRIPT_DIR/gcs_lifecycle.json"

echo "========================================================="
echo "       AAM BACKUP AUTOMATION - GCS BUCKET SETUP"
echo "========================================================="

# 1. Verify gcloud is installed
if ! command -v gcloud &> /dev/null; then
    echo "Error: 'gcloud' CLI is not installed or not in PATH."
    echo "Please install the Google Cloud SDK from:"
    echo "  https://cloud.google.com/sdk/docs/install"
    exit 1
fi

# 2. Check gcloud auth
echo "Checking active Google Cloud account..."
ACTIVE_ACCOUNT=$(gcloud config get-value account 2>/dev/null || true)
if [ -z "$ACTIVE_ACCOUNT" ]; then
    echo "No active account found. Please log in..."
    gcloud auth login
fi

# 3. Read default values from config.yaml
DEFAULT_BUCKET="aam-backup-bucket"
DEFAULT_PROJECT_NUM=""
DEFAULT_LOCATION="asia-south1"

if [ -f "$CONFIG_FILE" ]; then
    echo "Reading defaults from config.yaml..."
    BUCKET_VAL=$(grep -E '^\s*bucket:' "$CONFIG_FILE" | head -n1 | sed -E 's/.*bucket:[[:space:]]*"([^"]+)".*/\1/' | sed -E "s/.*bucket:[[:space:]]*'([^']+)'.*/\1/" | sed -E 's/.*bucket:[[:space:]]*([^[:space:]#]+).*/\1/')
    PROJECT_VAL=$(grep -E '^\s*project_number:' "$CONFIG_FILE" | head -n1 | sed -E 's/.*project_number:[[:space:]]*"([^"]+)".*/\1/' | sed -E "s/.*project_number:[[:space:]]*'([^']+)'.*/\1/" | sed -E 's/.*project_number:[[:space:]]*([^[:space:]#]+).*/\1/')
    LOCATION_VAL=$(grep -E '^\s*location:' "$CONFIG_FILE" | head -n1 | sed -E 's/.*location:[[:space:]]*"([^"]+)".*/\1/' | sed -E "s/.*location:[[:space:]]*'([^']+)'.*/\1/" | sed -E 's/.*location:[[:space:]]*([^[:space:]#]+).*/\1/')
    
    if [ ! -z "$BUCKET_VAL" ]; then DEFAULT_BUCKET="$BUCKET_VAL"; fi
    if [ ! -z "$PROJECT_VAL" ]; then DEFAULT_PROJECT_NUM="$PROJECT_VAL"; fi
    if [ ! -z "$LOCATION_VAL" ]; then DEFAULT_LOCATION="$LOCATION_VAL"; fi
fi

# 4. Prompt for settings
read -p "GCS Bucket Name to create/configure [$DEFAULT_BUCKET]: " input_bucket
BUCKET_NAME="${input_bucket:-$DEFAULT_BUCKET}"

read -p "GCS Location/Region [$DEFAULT_LOCATION]: " input_location
LOCATION="${input_location:-$DEFAULT_LOCATION}"

# Try to resolve Project ID from Project Number or active config
DEFAULT_PROJECT_ID=""
if [ ! -z "$DEFAULT_PROJECT_NUM" ]; then
    echo "Attempting to resolve Project ID for project number $DEFAULT_PROJECT_NUM..."
    DEFAULT_PROJECT_ID=$(gcloud projects list --filter="projectNumber=$DEFAULT_PROJECT_NUM" --format="value(projectId)" 2>/dev/null | head -n1 || true)
fi

if [ -z "$DEFAULT_PROJECT_ID" ]; then
    DEFAULT_PROJECT_ID=$(gcloud config get-value project 2>/dev/null || true)
fi

read -p "Google Cloud Project ID [$DEFAULT_PROJECT_ID]: " input_project
PROJECT_ID="${input_project:-$DEFAULT_PROJECT_ID}"

if [ -z "$PROJECT_ID" ]; then
    echo "Error: Project ID is required to configure GCS resources."
    exit 1
fi

read -p "Admin Email [$ACTIVE_ACCOUNT]: " input_admin
ADMIN_EMAIL="${input_admin:-$ACTIVE_ACCOUNT}"

read -p "Viewer 1 Email [viewer1@example.com]: " input_viewer1
VIEWER1_EMAIL="${input_viewer1:-viewer1@example.com}"

read -p "Viewer 2 Email [viewer2@example.com]: " input_viewer2
VIEWER2_EMAIL="${input_viewer2:-viewer2@example.com}"

read -p "Viewer 3 Email [viewer3@example.com]: " input_viewer3
VIEWER3_EMAIL="${input_viewer3:-viewer3@example.com}"

echo "Using Project ID: $PROJECT_ID"
echo "Using GCS Bucket: $BUCKET_NAME"
echo "Using Region:     $LOCATION"
echo "---------------------------------------------------------"

# 5. Check/Create Bucket
echo "Checking if bucket gs://$BUCKET_NAME exists..."
if gcloud storage buckets describe "gs://$BUCKET_NAME" --project="$PROJECT_ID" &>/dev/null; then
    echo "Bucket gs://$BUCKET_NAME already exists. Skipping creation."
else
    echo "Bucket gs://$BUCKET_NAME does not exist. Creating..."
    gcloud storage buckets create "gs://$BUCKET_NAME" \
        --project="$PROJECT_ID" \
        --location="$LOCATION" \
        --default-storage-class=STANDARD
    echo "Bucket gs://$BUCKET_NAME created successfully."
fi

# 6. Enable Versioning
echo "Enabling Object Versioning on gs://$BUCKET_NAME..."
gcloud storage buckets update "gs://$BUCKET_NAME" --versioning

# 7. Clear Soft Delete Policy
echo "Disabling Soft Delete policy (clearing soft delete) on gs://$BUCKET_NAME to save costs..."
gcloud storage buckets update "gs://$BUCKET_NAME" --clear-soft-delete

# 7b. Enable Uniform Bucket-Level Access
echo "Enabling Uniform Bucket-Level Access on gs://$BUCKET_NAME..."
gcloud storage buckets update "gs://$BUCKET_NAME" --uniform-bucket-level-access

# 8. Apply Lifecycle configuration
if [ -f "$LIFECYCLE_FILE" ]; then
    echo "Applying lifecycle rules from $LIFECYCLE_FILE to gs://$BUCKET_NAME..."
    gcloud storage buckets update "gs://$BUCKET_NAME" --lifecycle-file="$LIFECYCLE_FILE"
else
    echo "Warning: Lifecycle file $LIFECYCLE_FILE not found. Skipping lifecycle configuration."
fi

# 9. Configure Service Account
SA_NAME="aam-backup-agent"
SA_EMAIL="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

echo "Checking if service account $SA_EMAIL exists..."
if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" &>/dev/null; then
    echo "Service account $SA_EMAIL already exists."
else
    echo "Creating service account $SA_NAME..."
    gcloud iam service-accounts create "$SA_NAME" \
        --project="$PROJECT_ID" \
        --description="AAM Backup Agent Service Account" \
        --display-name="AAM Backup Agent"
fi

# 10. Bind IAM Role
echo "Granting roles/storage.objectAdmin on gs://$BUCKET_NAME to service account..."
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/storage.objectAdmin"

# 11. Bind Admin and Viewer Roles
echo "Configuring Admin and Viewer access on gs://$BUCKET_NAME..."

if [[ "$ADMIN_EMAIL" == *@*.* ]]; then
    echo "Granting roles/storage.admin to $ADMIN_EMAIL..."
    gcloud storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
        --member="user:$ADMIN_EMAIL" \
        --role="roles/storage.admin"
fi

for VIEWER in "$VIEWER1_EMAIL" "$VIEWER2_EMAIL" "$VIEWER3_EMAIL"; do
    if [[ "$VIEWER" != "viewer"* && "$VIEWER" == *@*.* ]]; then
        echo "Granting roles/storage.objectViewer to $VIEWER..."
        gcloud storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
            --member="user:$VIEWER" \
            --role="roles/storage.objectViewer"
    elif [[ "$VIEWER" == "viewer"* ]]; then
        echo "Skipping placeholder viewer: $VIEWER"
    fi
done

# 12. Generate Key File
KEY_DIR="$SCRIPT_DIR/keys"
mkdir -p "$KEY_DIR"
KEY_FILE="$KEY_DIR/aam-gcs-key.json"

if [ -f "$KEY_FILE" ]; then
    TIMESTAMP=$(date +%Y%m%d%H%M%S)
    BACKUP_KEY="$KEY_FILE.bak.$TIMESTAMP"
    echo "Warning: Service account key already exists at $KEY_FILE."
    echo "Backing up existing key to $BACKUP_KEY"
    mv "$KEY_FILE" "$BACKUP_KEY"
fi

echo "Creating new service account key at $KEY_FILE..."
gcloud iam service-accounts keys create "$KEY_FILE" \
    --iam-account="$SA_EMAIL" \
    --project="$PROJECT_ID"

echo "========================================================="
echo "       GCS BUCKET SETUP COMPLETE SUCCESSFULLY!"
echo "========================================================="
echo "Service Account: $SA_EMAIL"
echo "Key file saved to: $KEY_FILE"
echo ""
echo "ACTION REQUIRED: Update your config.yaml with the following:"
echo "  paths:"
echo "    gcs_key_path: \"$KEY_FILE\""
echo "  cloud:"
echo "    bucket: \"$BUCKET_NAME\""
echo "    project_number: \"$PROJECT_ID\""
echo "========================================================="
