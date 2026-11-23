# GCS lifecycle, versioning and Archive

Audit date: 2026-10-29. Bucket inspected: `gs://aam-backup-demo-innovizta`
(demo). Nothing in GCP was changed by this audit.

## What the bucket actually had

| Setting | Value | Verdict |
| --- | --- | --- |
| Versioning | **enabled** | correct |
| Soft delete | **disabled** (`retentionDurationSeconds: 0`) | should be enabled |
| Bucket default class | STANDARD, asia-south1 | correct |
| Uniform bucket access | on | correct |
| Retention policy / bucket lock | none | fine unless compliance requires it |
| Service-account role | `roles/storage.objectAdmin` | cannot change lifecycle/versioning |

Lifecycle rules found (identical to `deploy/gcs_lifecycle.json` before this
change):

1. noncurrent → COLDLINE after 1 day;
2. **Delete when `numNewerVersions: 2`**;
3. Delete noncurrent after 92 days;
4. ARCHIVE after `age: 365` (bucket-wide);
5. Abort incomplete multipart uploads after 7 days.

Measured effect on `FY26-27`: **5,422 version records, 691 live, 4,731
noncurrent; 2,656 objects had exactly 2 versions**. Rule 2 with
`numNewerVersions: 2` keeps **live + 1 older**, not 2 older. That is the single
most important correction.

## What changed in the repo

`deploy/gcs_lifecycle.json` now uses:

- `numNewerVersions: 3` → **live + 2 noncurrent versions**;
- `isLive: false` on the version-delete rules;
- `matchesStorageClass` on the ARCHIVE rules (avoids re-archiving);
- a **prefix-scoped ARCHIVE rule for the closed FYs** (`FY24-25/`,
  `FY25-26/`) so old years move at FY close, not 365 days after each object;
- the 92-day noncurrent cap remains (see the trade-off below);
- the same COLDLINE and abort-MPU rules.

This meets "two older versions of replaced files" for files that change within
the 92-day window. A file changed less often than every 92 days will have its
older versions pruned by the 92-day rule; remove that rule if the two versions
must be kept forever (storage cost is then up to 3× for replaced files).

## The 90-minute deletion requirement

**Native GCS cannot do 90 minutes.**

- Lifecycle time conditions are whole **days** (`daysSinceNoncurrentTime`,
  `age`, `createdBefore`). Minimum useful non-zero value: 1 day.
- Soft delete minimum retention is **7 days**, maximum 90 days; `0` disables it.
- Object Versioning has no delete markers: deleting a live object makes it a
  **noncurrent version**, which is retained until a lifecycle rule removes it
  or it is explicitly deleted by generation.

Options, in order of preference for this product:

| Option | Deleted-file recovery | Cost | Complexity |
| --- | --- | --- | --- |
| **A. Soft delete 7 days + versioning** (recommended) | at least 7 days | deleted/noncurrent bytes billed for 7 days | none, one bucket setting |
| B. Soft delete 90 days (if "90 days" was meant) | 90 days | more | none |
| C. Custom purge every 90 minutes | exactly ~90 min | small compute | a scheduled job with `storage.objects.delete`; it would also remove the 2-version history unless it distinguishes replaced vs deleted objects |
| D. Keep forever | forever | up to 3× | none |

**Recommendation:** clarify whether the client means "90 minutes" or "90
days". For a backup product, 7 days soft delete is the practical minimum and
is safer than 90 minutes. If 90 minutes is a hard compliance cap, it needs a
custom purge job and the "2 versions" promise must be limited to replaced
objects only; that is a separate component, not native lifecycle.

Enable soft delete (needs `storage.buckets.update`, i.e. Storage Admin):

```bash
gcloud storage buckets update gs://BUCKET --soft-delete-duration=7d
```

Do not set `0`; that removes the only protection against deleting noncurrent
versions.

## End-of-year ARCHIVE

Two mechanisms exist in the repo:

- `core/fy_rollover.py` runs a gcloud **rewrite** of the old FY prefix with
  `--storage-class=ARCHIVE`. It creates a new generation per object, is capped
  at 600 s, and is retried daily.
- Lifecycle rule 4 archives objects after 365 days, bucket-wide.

**Preferred:** use a **prefix-scoped lifecycle rule** for each closed FY. It
is metadata-only (no rewrite), creates no new generation, has no runtime
timeout, and catches later backfills into the old FY.

The repo file already includes the old FYs (`FY24-25/`, `FY25-26/`). When
`FY26-27` closes, add `FY26-27/` to `matchesPrefix` and re-apply; repeat each
April for the next closed FY:

```json
{
  "action": { "type": "SetStorageClass", "storageClass": "ARCHIVE" },
  "condition": {
    "matchesPrefix": ["FY24-25/", "FY25-26/", "FY26-27/"],
    "matchesStorageClass": ["STANDARD", "NEARLINE", "COLDLINE"]
  }
}
```

Do not add the current FY prefix before it is closed.

- `matchesPrefix` is an exact, case-sensitive prefix, not a glob. Keep the
  trailing `/`.
- Do **not** include the current FY prefix until it is closed.
- This rule archives live and noncurrent versions in the closed FY.
- `age: 365` can stay as a fallback, but it archives each object 365 days
  after **its own** creation, so a year folder trickles into ARCHIVE over
  months rather than at FY close.
- Do not use `gcloud storage objects update -s ARCHIVE` (or a rewrite) for
  bulk archive: it adds versions and can incur retrieval/early-deletion fees.
  Lifecycle `SetStorageClass` is a metadata transition with no early-deletion
  fee at transition; the 365-day Archive minimum still starts at object
  creation.

Cost note: ARCHIVE has a 365-day minimum storage duration and retrieval fees.
Transitioning a young object is free at transition, but deleting it before its
365th day incurs the remaining early-deletion charge. Budget an annual restore
drill.

## Apply the configuration

The current service account is `rclone-backup-agent@...` with
`roles/storage.objectAdmin`; it can read/write objects but **cannot**
`storage.buckets.update`. Use an owner/Storage Admin account for these
commands, from the test server or a machine with gcloud:

```bash
# 1. Apply the version/COLDLINE/ARCHIVE fallback rules in this repo
gcloud storage buckets update gs://BUCKET \
  --lifecycle-file=deploy/gcs_lifecycle.json

# 2. Keep versioning on
gcloud storage buckets update gs://BUCKET --versioning

# 3. Deletion safety net (7 days minimum; use 90d if intended)
gcloud storage buckets update gs://BUCKET --soft-delete-duration=7d

# 4. Verify
gcloud storage buckets describe gs://BUCKET \
  --format="json(versioning,lifecycle_config,soft_delete_policy)"

# 5. Inspect versions (no --full with --json; they are exclusive)
gcloud storage ls -r --all-versions --json gs://BUCKET/FY26-27/
```

Lifecycle changes can take up to 24 h to take effect and are asynchronous;
there is no dry-run. Apply to a scratch bucket first if you want to observe
the rules, then apply to the client bucket.

## Verify the two-version rule

```bash
# five generations of one test object
for i in 1 2 3 4 5; do
  echo "v$i" | gcloud storage cp - gs://BUCKET/_lc-test/obj.txt
done

gcloud storage ls --all-versions --json gs://BUCKET/_lc-test/ \
  | jq -r '.[] | [.metadata.generation,
                   (.metadata.timeDeleted // "LIVE"),
                   .metadata.storageClass] | @tsv'
```

After the next lifecycle pass, expect three generations: live + two with a
`timeDeleted` value. The third-oldest should be gone. Allow up to 24 h.

## Current gap summary

| Requirement | Status |
| --- | --- |
| Keep 2 older versions of replaced files | **fixed in `deploy/gcs_lifecycle.json`** (`numNewerVersions: 3`); must be applied to the bucket with a Storage Admin key |
| Deleted files ~90 minutes | **not possible natively**; soft delete 7 days recommended (or 90 days if intended); 90-minute cap needs a custom purge job |
| End-of-year FY → ARCHIVE | prefix-scoped lifecycle rule; add each closed FY prefix (the generic `age:365` fallback remains) |
| Lifecycle applied automatically | still manual; installer does not apply it; document/runbook step |
| Read-only restore key | create a separate `roles/storage.objectViewer` key |
| Soft delete | currently disabled; enable 7d |
| Archive rewrite cap | prefer lifecycle; if the gcloud rewrite stays, it is capped at 600 s and creates versions |
