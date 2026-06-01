import sys
import urllib.request
import zipfile
import os
import shutil

# Secure and ultra-reliable CDN hosted by Chocolatey
chocolatey_url = "https://community.chocolatey.org/api/v2/package/nssm"
tools_dir = r"C:\BackupAgent\tools"
nupkg_path = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "nssm_choco.nupkg")
extract_dir = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "nssm_choco_extract")
inner_zip_path = os.path.join(extract_dir, "tools", "nssm-2.24-101-g897c7ad.zip")
inner_extract_dir = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "nssm_inner_extract")
nssm_dest = os.path.join(tools_dir, "nssm.exe")

print("===================================================")
print("  AAM Backup — NSSM Downloader (Chocolatey Mirror)")
print("===================================================")

# ── Create tools directory ──
if not os.path.exists(tools_dir):
    os.makedirs(tools_dir, exist_ok=True)
    print(f"[nssm] Created tools directory: {tools_dir}")

# ── Download .nupkg from Chocolatey CDN ──
print(f"[nssm] Downloading NSSM package from Chocolatey CDN ...")
try:
    req = urllib.request.Request(chocolatey_url)
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
    with urllib.request.urlopen(req, timeout=30) as response:
        with open(nupkg_path, "wb") as f:
            f.write(response.read())
    print(f"[nssm] Package download complete")
except Exception as e:
    print(f"[nssm] ERROR: Failed to download from Chocolatey CDN: {e}")
    sys.exit(1)

# ── Extract Chocolatey Package ──
print("[nssm] Extracting NuGet package...")
if os.path.exists(extract_dir):
    shutil.rmtree(extract_dir, ignore_errors=True)

try:
    with zipfile.ZipFile(nupkg_path, "r") as zip_ref:
        zip_ref.extractall(extract_dir)
except Exception as e:
    print(f"[nssm] ERROR: NuGet extraction failed: {e}")
    sys.exit(1)

# ── Extract Inner NSSM zip ──
print("[nssm] Extracting inner service wrapper zip...")
if not os.path.exists(inner_zip_path):
    print(f"[nssm] ERROR: Could not find inner ZIP at {inner_zip_path}")
    sys.exit(1)

if os.path.exists(inner_extract_dir):
    shutil.rmtree(inner_extract_dir, ignore_errors=True)

try:
    with zipfile.ZipFile(inner_zip_path, "r") as zip_ref:
        zip_ref.extractall(inner_extract_dir)
except Exception as e:
    print(f"[nssm] ERROR: Inner ZIP extraction failed: {e}")
    sys.exit(1)

# ── Copy win64 binary ──
# Note: Inner zip has folder named 'nssm-2.24-101-g897c7ad'
nssm_src = os.path.join(inner_extract_dir, "nssm-2.24-101-g897c7ad", "win64", "nssm.exe")
if not os.path.exists(nssm_src):
    # Try alternate location without version name
    nssm_src = os.path.join(inner_extract_dir, "win64", "nssm.exe")

if not os.path.exists(nssm_src):
    print(f"[nssm] ERROR: Could not find win64 binary inside inner zip extraction")
    sys.exit(1)

try:
    shutil.copy2(nssm_src, nssm_dest)
    print(f"[nssm] Installed successfully to: {nssm_dest}")
except Exception as e:
    print(f"[nssm] ERROR: Failed to copy binary: {e}")
    sys.exit(1)

# ── Cleanup ──
try:
    if os.path.exists(nupkg_path):
        os.remove(nupkg_path)
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    if os.path.exists(inner_extract_dir):
        shutil.rmtree(inner_extract_dir, ignore_errors=True)
    print("[nssm] Cleaned up temporary files")
except Exception as e:
    print(f"[nssm] Warning during cleanup: {e}")

print("\nDone. NSSM is ready at: " + nssm_dest)
print("Next step: Run deploy\\install_services.bat as Administrator\n")
