# Server Discovery Report 
 
**Generated:** 20-06-2026 14:17:20.11 
 
## 1. System Information 
 
- **Hostname:** AAMBDC001 
- **Domain:** caaam.com 
- **Windows Version:** Microsoft Windows Server 2016 Datacenter 
- **Build Number:** 14393 
- **Current User:** Administrator 
- **Admin Privileges:** Yes 
 
## 2. Storage Information 
 
### Drives 
 
| Drive | Type | Total | Free | File System | 
|-------|------|-------|------|-------------| 
| C: | Local | 419.3 GB | 234.8 GB | NTFS | 
| D: | Local | 418.4 GB | 52.2 GB | NTFS | 
| E: | Local | 931.5 GB | 657.5 GB | NTFS | 
 
### Source Drive Check 
 
- **C:\** exists 
- **D:\** exists 
- **E:\** exists 
 
## 3. Network Information 
 
### IP Configuration 
 
'
   Connection-specific DNS Suffix  . : 
   Connection-specific DNS Suffix  . : 
   Connection-specific DNS Suffix  . : 
   Connection-specific DNS Suffix  . : 
   Connection-specific DNS Suffix  . : 
   IPv4 Address. . . . . . . . . . . : 192.168.10.5
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : 192.168.10.1
   Connection-specific DNS Suffix  . : 
   Connection-specific DNS Suffix  . : 
   Connection-specific DNS Suffix  . : 
   Connection-specific DNS Suffix  . : 
   Connection-specific DNS Suffix  . : 
'
### Wake-on-LAN (WoL) Status 
 
- **Adapter [Ethernet 4]:** Wake on Magic Packet is ENABLED 
- **Adapter [Ethernet]:** Wake on Magic Packet is ENABLED 
- **Adapter [Ethernet 3]:** Wake on Magic Packet is ENABLED 
- **Adapter [Ethernet 2]:** Wake on Magic Packet is ENABLED 
 
### DNS Resolution Test 
 
- **DNS Resolution:** Working 
### Internet Connectivity 
 
- **Internet Access:** Available 
 
### Local Network Devices (ARP Cache) 
 
Identifying local network devices for potential WoL targets: 
 
Scanning subnet 192.168.10.x to discover all active MAC addresses... 
``` 

Interface: 192.168.10.5 --- 0x7
  Internet Address      Physical Address      Type
  192.168.10.1          04-d5-90-5f-e0-66     dynamic   
  192.168.10.20         b4-b0-24-be-67-4e     dynamic   
  192.168.10.21         8c-ec-4b-a3-6f-f2     dynamic   
  192.168.10.22         14-eb-b6-ce-23-9d     dynamic   
  192.168.10.23         3c-52-a1-b7-d8-3c     dynamic   
  192.168.10.24         50-9a-4c-2b-67-9c     dynamic   
  192.168.10.25         30-d0-42-0c-28-bf     dynamic   
  192.168.10.26         50-9a-4c-1e-48-f4     dynamic   
  192.168.10.27         14-eb-b6-46-e8-06     dynamic   
  192.168.10.29         14-eb-b6-46-e8-42     dynamic   
  192.168.10.32         28-d2-44-c8-50-ba     dynamic   
  192.168.10.35         2c-4d-54-e8-eb-07     dynamic   
  192.168.10.45         8c-ec-4b-77-c9-50     dynamic   
  192.168.10.79         80-5e-0c-2f-03-44     dynamic   
  192.168.10.244        58-03-fb-28-28-1d     dynamic   
  192.168.10.245        00-17-61-10-6c-d0     dynamic   
  192.168.10.250        3c-2a-f4-3e-b7-97     dynamic   
  192.168.10.255        ff-ff-ff-ff-ff-ff     static    
  224.0.0.22            01-00-5e-00-00-16     static    
  224.0.0.251           01-00-5e-00-00-fb     static    
  224.0.0.252           01-00-5e-00-00-fc     static    
  224.0.0.253           01-00-5e-00-00-fd     static    
  224.0.1.75            01-00-5e-00-01-4b     static    
  236.163.254.169       01-00-5e-23-fe-a9     static    
  239.255.102.18        01-00-5e-7f-66-12     static    
  239.255.255.250       01-00-5e-7f-ff-fa     static    
``` 
 
### Target Backup Server Test 
 
- No target IP provided. Skipped. 
 
## 4. Software & Tools 
 
### Python 
- **Python:** Not installed 
- **uv:** Not installed 
- **pip:** Not installed 
- **rclone:** Not installed 
- **robocopy:** Available 
- **NSSM:** Not installed 
 
## 5. Permissions & Access 
 
- **Service Query:** Allowed 
- **Existing Service (AamBackupAgent):** Not found 
### Network Share Access 
 
- Skipped automated share test to ensure zero impact on live network connections. 
### Local Administrators 
 
The following users can be used to run the backup service (AamBackupAgent) 
to ensure access to UNC paths: 
 
- aamadmin 
- Administrator 
- Domain Admins 
- Enterprise Admins 
- manosh 
- reghu 
- saranya 
### PowerShell Execution Policy 
 
- **Execution Policy:** RemoteSigned 
### User Account Control (UAC) 
 
- **UAC Status:** Enabled (Strict mode may require explicit admin elevation) 
 
## 6. Existing Installation 
 
- **config.yaml:** Not found 
- **logs directory:** Not found 
- **.prefect directory:** Not found 
- **manifest.db:** Not found 
- **GCS key file:** Not found 
 
## 16. Connectivity Tests 
 
### Proxy Settings 
 
- **System Proxy:** Not configured 
 
### Google Cloud Storage 
 
- **storage.googleapis.com:** Reachable 
### Time Skew Check 
 
- **Time Skew:** Could not verify against google.com 
### NTP Accessibility 
 
Testing NTP (UDP 123) against common time servers (may take a few seconds)... 
- **time.windows.com:** Reachable (UDP 123 Open) 
- **pool.ntp.org:** Reachable (UDP 123 Open) 
 
### SMTP Connectivity 
 
Testing SMTP ports (may take 30-60 seconds if blocked)... 
- **Port 587 (SMTP TLS):** Open 
- **Port 465 (SMTP SSL):** Open 
- **Port 25 (SMTP Plain):** Open 
 
## 17. Pending Reboot & Windows Update 
 
- **Pending Reboot:** No 
- **Windows Update Reboot:** Not required 
### Recent Updates 
 
- **Recent Updates:** Could not retrieve 
 
## 18. Final Summary & Recommendations 
 
### Deployment Readiness: ISSUES FOUND 
 
**Critical Issues:** 
- Missing config.yaml\n 
 
### Next Steps 
 
1. Send both reports to deployment team 
2. Team will generate config.yaml from collected data 
3. Schedule deployment window 
4. Run install_services.bat as Administrator 
## 13. Windows Services Status 
 
### Critical Services 
 
- **LanmanServer:** RUNNING 
- **LanmanWorkstation:** RUNNING 
- **W32Time:** RUNNING 
- **EventLog:** RUNNING 
- **Schedule:** RUNNING 
- **Spooler:** RUNNING 
- **wuauserv:** RUNNING 
### Potential Interference 
 
- **WSearch:** RUNNING (may use resources) 
- **Spooler:** RUNNING (may use resources) 
### VSS (Volume Shadow Copy) Health 
 
- **VSS Service State:** STOPPED 
- Checking VSS Writers... 
- **VSS Writers:** ERRORS DETECTED (backups of locked files may fail) 
``` 
Writer name: 'Task Scheduler Writer'
   State: [1] Stable
   Last error: No error
Writer name: 'VSS Metadata Store Writer'
   State: [1] Stable
   Last error: No error
Writer name: 'Performance Counters Writer'
   State: [1] Stable
   Last error: No error
Writer name: 'System Writer'
   State: [1] Stable
   Last error: No error
Writer name: 'SqlServerWriter'
   State: [1] Stable
   Last error: No error
Writer name: 'Shadow Copy Optimization Writer'
   State: [1] Stable
   Last error: No error
Writer name: 'WMI Writer'
   State: [1] Stable
   Last error: No error
Writer name: 'ASR Writer'
   State: [1] Stable
   Last error: No error
Writer name: 'MSSearch Service Writer'
   State: [1] Stable
   Last error: No error
Writer name: 'DFS Replication service writer'
   State: [1] Stable
   Last error: No error
Writer name: 'COM+ REGDB Writer'
   State: [1] Stable
   Last error: No error
Writer name: 'Registry Writer'
   State: [1] Stable
   Last error: No error
Writer name: 'NTDS'
   State: [1] Stable
   Last error: No error
``` 
 
## 14. Installed Software 
 
### Key Software 
 
- **Installed Programs:** Retrieved (see JSON) 
### Python Installations 
 
 
## 15. Environment & System Variables 
 
### PATH Check 
 
- **PATH entries:** 
'
'
### Relevant Environment Variables 
 
### Temp Paths 
 
- **TEMP:** C:\Users\ADMINI~1.CAA\AppData\Local\Temp 
- **TMP:** C:\Users\ADMINI~1.CAA\AppData\Local\Temp 
 
## 7. Port Availability 
 
- **Port 4200 (Prefect):** Available 
- **Port 8080 (Dashboard):** Available 
 
## 8. System Resources 
 
- **Total RAM:** 128 GB 
- **CPU:** Intel(R) Xeon(R) CPU E5-2640 0 @ 2.50GHz 
- **CPU Cores:** 2 
- **Last Boot:** LastBootUpTime=20260619012913.005013+330 
 
## 9. Timezone & Power 
 
- **Timezone:** India Standard Time 
- **Auto Updates:** Enabled (may cause unexpected reboots) 
- **Power Plan:** Configured 
 
## 10. Windows Features & Dependencies 
 
- **.NET Framework 4.x:** Installed 
- **Visual C++ 2015-2022:** Installed 
- **SMB Client:** Available 
- **Windows Firewall:** Inactive 
### Long Path Support (MAX_PATH) 
 
- **Long Paths ( chars):** Disabled (Deep directory backups may fail) 
 
## 11. Potential Conflicts 
 
### Backup Software 
 
- No other backup software detected 
### Antivirus & Windows Defender 
 
- No antivirus or Defender active/detected. 
 
## 12. Recent System Errors 
 
- **Recent Errors:** None found 
 
## Summary 
 
### Deployment Readiness 
 
**Status:** Issues found 
 
### Issues to Resolve: 
- Missing config.yaml\n 
 
--- 
*Report generated by AAM Backup Discovery Script* 
