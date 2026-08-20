@echo off
"C:\Users\Administrator\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" storage buckets describe gs://aam-backup-demo-innovizta --format=json > "C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\_s3_pre_state\bucket_desc.json" 2> "C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\_s3_pre_state\bucket_desc.err"
exit /b %ERRORLEVEL%
