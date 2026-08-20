# Install Google Cloud SDK silently
Write-Host "Downloading Google Cloud SDK Installer..." -ForegroundColor Cyan
$installerPath = "$env:Temp\GoogleCloudSDKInstaller.exe"
(New-Object Net.WebClient).DownloadFile("https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe", $installerPath)

Write-Host "Installing Google Cloud SDK (This will take a few minutes)..." -ForegroundColor Cyan
Start-Process -FilePath $installerPath -ArgumentList "/S", "/NoLaunch", "/NoDesktopShortcut", "/SingleUser" -Wait -NoNewWindow

Write-Host "Installation Complete! Please restart your terminal/PowerShell for 'gcloud' to be available in the PATH." -ForegroundColor Green
