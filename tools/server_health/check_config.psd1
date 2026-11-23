@{
    # SSH target (alias from ~/.ssh/config)
    SshHost              = 'innoviztawinserver2016'

    # Python on the machine running this script (for the LAN-side hashing)
    LocalPython          = 'python'

    # Paths on the test server
    RemotePython         = 'C:\AAMBackup\.venv\Scripts\python.exe'
    ManifestDb           = 'C:\BackupAgent\manifest.db'
    PrefectDb            = 'C:\BackupAgent\.prefect\prefect.db'
    SourceDir            = 'E:\FY26-27'

    # LAN destination. NOTE: this is a share on the machine that RUNS this
    # script (the NAS is 10.10.151.53 = this PC). Change if run elsewhere.
    LanDestination       = 'C:\AAM_NAS\FY26-27'

    # Control-plane marker excluded from content comparison
    CanaryName           = '.AAM_TARGET_MOUNTED'

    # Cloud destination (rclone on the test server)
    GcsBucket            = 'aam-backup-demo-innovizta'
    GcsPrefix            = 'FY26-27'
    GcsKeyPath           = 'C:\AAMBackup\deploy\keys\aam-gcs-key.json'
    GcsProjectNumber     = '920173882190'
    GcsLocation          = 'asia-south1'
    GcsStorageClass      = 'STANDARD'
    RcloneExe            = 'C:\AAMBackup\deploy\bin\rclone.exe'

    # Freshness / alerting thresholds
    MaxRunAgeHours       = 30      # WARN if the last LAN/cloud run is older
    MaxRunAgeHoursFail   = 48      # FAIL if older than this
    MaxWatchdogAgeMinutes = 15     # WARN if the watchdog heartbeat is stale
}
