$ErrorActionPreference = "Stop"
Set-Location C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1
$proc = Start-Process -FilePath "C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\.venv\Scripts\python.exe" -ArgumentList "launch.py" -RedirectStandardOutput "test_shutdown_out.txt" -RedirectStandardError "test_shutdown_err.txt" -PassThru -NoNewWindow
Start-Sleep -Seconds 40
taskkill /F /PID $proc.Id
Start-Sleep -Seconds 5
Write-Output "=== STDOUT ===" | Out-File test_result.txt
Get-Content test_shutdown_out.txt | Out-File test_result.txt -Append
Write-Output "=== STDERR ===" | Out-File test_result.txt -Append
Get-Content test_shutdown_err.txt -ErrorAction SilentlyContinue | Out-File test_result.txt -Append
