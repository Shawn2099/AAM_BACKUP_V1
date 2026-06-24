"""
read_config.py - Lightweight YAML reader for PowerShell scripts.

Usage from PowerShell:
    python deploy/read_config.py config.yaml paths.runtime_dir
    python deploy/read_config.py config.yaml cloud.enabled
    python deploy/read_config.py config.yaml paths.runtime_dir --default C:/BackupAgent

Returns the value as a single line to stdout. Exits 1 on error.
"""
import sys
import os

def read_value(config_path: str, dotpath: str, default: str | None = None) -> str | None:
    """Read a value from a YAML config file using dot notation."""
    try:
        import yaml
    except ImportError:
        # Fallback: minimal YAML parser for simple key: value pairs
        return _read_yaml_fallback(config_path, dotpath, default)

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    keys = dotpath.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return str(current) if current is not None else default


def _read_yaml_fallback(config_path: str, dotpath: str, default: str | None = None) -> str | None:
    """Minimal YAML fallback — handles flat and one-level nested keys only."""
    import re

    keys = dotpath.split(".")
    target_key = keys[-1]
    parent_key = keys[0] if len(keys) > 1 else None

    in_parent = parent_key is None  # if no parent, we're already in scope
    found_key = False

    with open(config_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            # Check for parent key (e.g., "paths:")
            if parent_key and re.match(rf"^{re.escape(parent_key)}\s*:", stripped):
                in_parent = True
                continue

            # If in parent scope, look for target key
            if in_parent:
                # Match "key: value" or "key: "value""
                m = re.match(rf"^{re.escape(target_key)}\s*:\s*(.*)", stripped)
                if m:
                    value = m.group(1).strip().strip('"').strip("'")
                    # Handle inline comments
                    if "#" in value:
                        value = value[:value.index("#")].strip().strip('"').strip("'")
                    return value if value else default

            # If no parent and we hit a top-level key that's not our target, stop
            if not parent_key and re.match(rf"^{re.escape(target_key)}\s*:", stripped):
                m = re.match(rf"^{re.escape(target_key)}\s*:\s*(.*)", stripped)
                if m:
                    value = m.group(1).strip().strip('"').strip("'")
                    if "#" in value:
                        value = value[:value.index("#")].strip().strip('"').strip("'")
                    return value if value else default

    return default


def read_section(config_path: str, section: str) -> dict:
    """Read an entire section as a dict."""
    try:
        import yaml
    except ImportError:
        return {}

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if isinstance(data, dict) and section in data:
        result = data[section]
        return result if isinstance(result, dict) else {}
    return {}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <config.yaml> <dot.path> [--default VALUE]", file=sys.stderr)
        sys.exit(1)

    config_path = sys.argv[1]
    dotpath = sys.argv[2]
    default = None

    if "--default" in sys.argv:
        idx = sys.argv.index("--default")
        if idx + 1 < len(sys.argv):
            default = sys.argv[idx + 1]

    if not os.path.exists(config_path):
        if default is not None:
            print(default)
            sys.exit(0)
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    value = read_value(config_path, dotpath, default)
    if value is None:
        print(f"Error: Key not found: {dotpath}", file=sys.stderr)
        sys.exit(1)

    print(value)
