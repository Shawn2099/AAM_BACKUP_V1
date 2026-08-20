@echo off
"C:\Users\Administrator\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" storage buckets update gs://aam-backup-demo-innovizta --lifecycle-file "C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\deploy\gcs_lifecycle.json" > "C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\_s3_pre_state\lifecycle_apply.out" 2>&1
exit /b %ERRORLEVEL%
