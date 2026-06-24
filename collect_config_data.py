import os
import sys
import socket
import psutil
import yaml
from core.time_utils import get_fy_prefix
from models.config import AppConfig, CONFIG_PATH

# Force UTF-8 encoding for stdout to support emojis on Windows CMD
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

def get_network_info():
    interfaces = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    
    active_networks = []
    for interface_name, interface_addresses in interfaces.items():
        if interface_name in stats and getattr(stats[interface_name], "isup", False):
            if "Loopback" in interface_name or "lo" in interface_name:
                continue
                
            ip = None
            mac = None
            for addr in interface_addresses:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                elif addr.family == psutil.AF_LINK:
                    mac = addr.address
            
            if ip and mac and not ip.startswith("127.") and not ip.startswith("169.254."):
                mac = mac.replace("-", ":").upper()
                active_networks.append((interface_name, ip, mac))
                
    return active_networks

def list_drives():
    drives = []
    for partition in psutil.disk_partitions(all=False):
        if partition.fstype:
            drives.append(f"{partition.device} (Type: {partition.fstype}, Mount: {partition.mountpoint})")
    return drives

import subprocess
def is_firewall_open(port: int = 8080) -> bool:
    try:
        cmd = f"powershell -Command \"Get-NetFirewallRule | Where-Object {{ $_.Enabled -eq $true -and $_.Direction -eq 'Inbound' -and $_.Action -eq 'Allow' }} | Get-NetFirewallPortFilter | Where-Object {{ $_.LocalPort -eq '{port}' }}\""
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return bool(result.stdout.strip())
    except Exception:
        return False

def verify_with_pydantic(snippet_data: dict) -> bool:
    """Uses the existing Pydantic AppConfig model to verify the snippet is valid."""
    try:
        # Load the base config.yaml so we have all the required fields
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            full_config = yaml.safe_load(f)
            
        # Update with our snippet data
        for k, v in snippet_data.items():
            if isinstance(v, dict) and k in full_config:
                full_config[k].update(v)
            else:
                full_config[k] = v
                
        # Run through Pydantic validation
        AppConfig(**full_config)
        return True
    except Exception as e:
        print(f"\n[!] WARNING: Pydantic Validation Failed: {e}")
        return False

def main():
    print("="*70)
    print("   AAM BACKUP AUTOMATION - CONFIGURATION DATA COLLECTOR")
    print("="*70)
    print("Gathering exact details needed to manually update config.yaml.\n")
    
    networks = get_network_info()
    drives = list_drives()
    fy = get_fy_prefix()
    
    print("[ 1. AVAILABLE LOCAL DRIVES ]")
    for d in drives:
        print(f"  - {d}")
    print("\n")
    
    print("[ 2. COPY-PASTE READY YAML SNIPPETS ]")
    print("You can copy the relevant sections directly into your config.yaml.")
    
    # Detect the project root relative to this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_key_path = os.path.join(script_dir, "deploy", "keys", "aam-gcs-key.json")
    key_path_yaml = default_key_path.replace("\\", "\\\\")

    # Generate Paths snippet
    paths_snippet = {
        "paths": {
            "source_drive": f"D:\\{fy}",
            "lan_destination": f"\\\\192.168.1.100\\share\\{fy}"
        }
    }

    # Test Paths snippet
    paths_valid = verify_with_pydantic(paths_snippet)

    print()
    print("# " + "-"*50)
    if paths_valid:
        print("# PATHS [✅ Verified by Pydantic]")
    else:
        print("# PATHS [❌ Validation Failed]")
    print("# " + "-"*50)
    print("paths:")
    print(f"  # Current fiscal year: {fy}")
    print(f"  # ⚠️  FIRST DEPLOYMENT: Manually create D:\\{fy}\\ on the source PC")
    print(f"  # ⚠️  FIRST DEPLOYMENT: Manually create \\\\<NAS_IP>\\share\\{fy}\\ on the NAS")
    print(f"  source_drive: \"D:\\\\{fy}\"  # <-- UPDATE drive letter if not D:")
    print(f"  lan_destination: \"\\\\\\\\192.168.1.100\\\\share\\\\{fy}\"  # <-- UPDATE IP and share name")
    print(f"  # GCS key: place your .json key at deploy\\keys\\aam-gcs-key.json")
    print(f"  gcs_key_path: \"{key_path_yaml}\"")
    print()
    
    if not networks:
        print("  WARNING: No active IPv4 network interfaces found.\n")
    else:
        for name, ip, mac in networks:
            # Build network snippet for this interface
            net_snippet = {
                "wol": {"mac_address": mac, "server_ip": ip},
                "dashboard": {"bind_address": ip}
            }
            net_valid = verify_with_pydantic(net_snippet)
            
            print("# " + "-"*50)
            if net_valid:
                print(f"# NETWORK INTERFACE: {name} [✅ Verified by Pydantic]")
            else:
                print(f"# NETWORK INTERFACE: {name} [❌ Validation Failed]")
            print("# " + "-"*50)
            print(f"# IF THIS IS THE TARGET (NAS) SERVER, use these in 'wol':")
            print("wol:")
            print(f"  mac_address: \"{mac}\"")
            print(f"  server_ip: \"{ip}\"")
            print()
            print(f"# IF THIS IS THE SOURCE (BACKUP) SERVER, use this in 'dashboard':")
            print("dashboard:")
            print(f"  bind_address: \"{ip}\"")
            print()

    print("[ 3. MANUAL CONFIGURATION REMINDERS ]")
    print("  - Ensure 'paths.gcs_key_path' points to the valid Google Cloud .json key.")
    print("  - In paths, use double backslashes (\\\\) as shown in the snippets.")
    print("  - Make sure 'services.msc -> AAM Backup Agent' is running as an Administrator")
    print("    or a network user if you are writing to a LAN share.")
    print("\n[ 4. SYSTEM CHECKS ]")
    if is_firewall_open(8080):
        print("  [✅] Windows Firewall: Port 8080 is OPEN (Dashboard accessible over network)")
    else:
        print("  [❌] Windows Firewall: Port 8080 is CLOSED")
        print("      Run this in elevated Command Prompt to open it:")
        print("      netsh advfirewall firewall add rule name=\"AAM Backup Dashboard\" dir=in action=allow protocol=TCP localport=8080")
    print("="*70)
    input("Press Enter to exit...")

if __name__ == '__main__':
    main()
