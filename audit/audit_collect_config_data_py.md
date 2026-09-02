# Code Audit - File 6 of 26: collect_config_data.py
Lines: 173 | Size: 6.8 KB | Audited: 2026-09-02

---

## Summary

collect_config_data.py is a one-shot operator utility script run at
deployment time to collect system info (drives, NICs, MAC addresses)
and print ready-to-paste YAML config snippets. It is NOT part of the
runtime backup pipeline - it is run manually once during initial setup.

Syntactically clean. Given its non-production, interactive nature most
issues are LOW. However, two real bugs are notable.

---

## Findings

### CRITICAL - 0 | HIGH - 0

---

### MEDIUM - 2

MEDIUM-1: verify_with_pydantic - Deep-Merges Dicts Into Full Config Before Validating (Lines 57-76)

    for k, v in snippet_data.items():
        if isinstance(v, dict) and k in full_config:
            full_config[k].update(v)
        else:
            full_config[k] = v
    AppConfig(**full_config)

The snippet validation loads the ENTIRE config.yaml and overlays the
snippet data before validating. This means:
  (a) The validation is ALWAYS against the real production config.yaml,
      not an isolated snippet. If config.yaml doesn't exist or is corrupt
      at first-run (before the operator has configured it), the function
      raises an unhandled FileNotFoundError or yaml.YAMLError that crashes
      the script entirely.
  (b) The shallow .update() only merges top-level keys within each section.
      A snippet like {"paths": {"source_drive": "D:\\"}} will overlay
      paths.source_drive but leave all other paths.* keys from the real
      config unchanged. This is correct for the current snippets, but
      brittle if snippet structure changes.

Fix: Wrap the file read in try/except with a clear message for first-run
users. Consider generating a standalone minimal AppConfig from scratch
for snippet validation rather than mutating the real config.

MEDIUM-2: is_firewall_open - Uses shell=True with Interpolated User Data (Lines 49-55)

    cmd = f"powershell -Command \"...LocalPort -eq '{port}'\"..."
    result = subprocess.run(cmd, shell=True, ...)

The port parameter is typed as int so in practice it cannot be injected
from user input here. However, shell=True with string interpolation is a
dangerous pattern to have in the codebase - if this function signature
ever changes to accept a string or if the caller is modified, it becomes
a command injection vulnerability. The PowerShell command itself is also
passed as a single shell string, which makes quoting fragile.

Fix: Use subprocess.run with a list of arguments and no shell=True.
Example: ["powershell", "-Command", f"...LocalPort -eq '{port}'"]

---

### LOW - 3

LOW-1: subprocess import at Line 46 - Not at Top of File

    import subprocess   # line 46, after other imports at top

PEP 8 requires all imports at the top of the file. This import is inside
the module body after function definitions.

LOW-2: Hardcoded Default IP 192.168.1.100 in YAML Snippet (Lines 104-106)

    "lan_destination": f"\\\\192.168.1.100\\share\\{fy}"

The default NAS IP is hardcoded as 192.168.1.100. For deployments on a
different subnet, the operator might copy-paste this literally without
changing it, resulting in a wrong config. Should be printed with a clear
"<NAS_IP>" placeholder rather than a real-looking default IP.

LOW-3: input("Press Enter to exit...") Blocks Automation (Line 169)

If collect_config_data.py is ever called from a CI pipeline, deployment
script, or subprocess (e.g., an automated onboarding wizard), the
blocking input() will hang indefinitely. Consider checking
sys.stdin.isatty() and skipping the pause when not interactive.

---

## INFO

- Line 12-13: stdout.reconfigure for UTF-8 emoji support on Windows CMD is correct.
- Line 21: getattr(stats[interface_name], "isup", False) is defensive - good.
- Line 33: Filtering out 127.x and 169.254.x (APIPA) addresses is correct.
- Line 97: os.path.abspath(__file__) for script-relative paths is correct.

---

## Verdict
| Severity | Count |
|----------|-------|
| CRITICAL |   0   |
| HIGH     |   0   |
| MEDIUM   |   2   |
| LOW      |   3   |
| Total    |   5   |

PRODUCTION READY (utility script, not runtime). No blocking issues.
MEDIUM-1 is the most important to fix for first-run operator experience.
