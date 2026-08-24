import os
import sys

# Add the project root to sys.path so we can import 'models'
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, ".."))
sys.path.insert(0, project_root)

from models.config import load_config


def validate(config_path: str):
    """Load and validate config.yaml against the Pydantic schema.

    Returns AppConfig on success; raises on invalid config.
    No printing, no exiting - callers own presentation and exit codes.
    """
    return load_config(config_path)


def _pause() -> None:
    """Interactive pause that never crashes non-interactive contexts
    (scheduled tasks, CI, redirected stdin all raise EOFError/OSError)."""
    try:
        input("\nPress Enter to exit...")
    except (EOFError, OSError):
        pass


def main(config_path: str = None) -> int:
    """Validate config.yaml. Returns 0 on success, 1 on any failure."""
    print("="*60)
    print("   AAM BACKUP AUTOMATION - CONFIGURATION TESTER")
    print("="*60)
    print("Testing config.yaml against Pydantic schema validation...\n")

    if config_path is None:
        config_path = os.path.join(project_root, "config.yaml")

    if not os.path.exists(config_path):
        print(f"ERROR: config.yaml not found at {config_path}")
        _pause()
        return 1

    try:
        config = validate(config_path)
    except Exception as e:
        print("[ERROR] in config.yaml validation:")
        print("-" * 50)
        print(str(e))
        print("-" * 50)
        print("Please fix the errors above and run this test again.")
        _pause()
        return 1

    print("[SUCCESS] config.yaml is fully valid!")
    print("\nSummary:")
    print(f"  - Source: {config.paths.source_drive}")
    print(f"  - LAN Destination: {config.paths.lan_destination} (Enabled: {config.lan.enabled})")
    print(f"  - Cloud Bucket: {config.cloud.bucket} (Enabled: {config.cloud.enabled})")

    if config.wol.enabled:
        print(f"  - WOL Target: {config.wol.server_ip} ({config.wol.mac_address})")

    print("\nReady to run `restart_services.bat`.")
    print("=" * 60)
    _pause()
    return 0


if __name__ == "__main__":
    sys.exit(main())
