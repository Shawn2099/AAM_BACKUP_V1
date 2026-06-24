import sys
import os

# Add the project root to sys.path so we can import 'models'
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, ".."))
sys.path.insert(0, project_root)

from models.config import load_config

def main():
    print("="*60)
    print("   AAM BACKUP AUTOMATION - CONFIGURATION TESTER")
    print("="*60)
    print("Testing config.yaml against Pydantic schema validation...\n")
    
    config_path = os.path.join(project_root, "config.yaml")
    
    if not os.path.exists(config_path):
        print(f"❌ ERROR: config.yaml not found at {config_path}")
        input("\nPress Enter to exit...")
        sys.exit(1)
        
    try:
        config = load_config(config_path)
        print("[SUCCESS] config.yaml is fully valid!")
        print(f"\nSummary:")
        print(f"  - Source: {config.paths.source_drive}")
        print(f"  - LAN Destination: {config.paths.lan_destination} (Enabled: {config.lan.enabled})")
        print(f"  - Cloud Bucket: {config.cloud.bucket} (Enabled: {config.cloud.enabled})")
        
        if config.wol.enabled:
            print(f"  - WOL Target: {config.wol.server_ip} ({config.wol.mac_address})")
            
        print("\nReady to run `restart_services.bat`.")
    except Exception as e:
        print("[ERROR] in config.yaml validation:")
        print("--------------------------------------------------")
        print(str(e))
        print("--------------------------------------------------")
        print("Please fix the errors above and run this test again.")
    
    print("="*60)
    input("Press Enter to exit...")

if __name__ == "__main__":
    main()
