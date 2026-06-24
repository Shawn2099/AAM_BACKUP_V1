import subprocess

def test_robocopy_streams():
    print("Running Robocopy against non-existent directories to trigger a fatal error...\n")
    
    # We deliberately use a fake source path to trigger an "Accessing Source Directory" error.
    cmd = ["robocopy", "C:\\THIS_DIR_DOES_NOT_EXIST", "C:\\ANOTHER_FAKE_DIR", "/L", "/MIR"]
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )
    
    print(f"Exit Code: {result.returncode}\n")
    
    print("=== CONTENTS OF STDOUT ===")
    print(result.stdout if result.stdout.strip() else "<EMPTY>")
    print("==========================\n")
    
    print("=== CONTENTS OF STDERR ===")
    print(result.stderr if result.stderr.strip() else "<EMPTY>")
    print("==========================\n")
    
    # Programmatic assertions to definitively prove the behavior
    try:
        assert result.returncode == 16, f"Expected fatal exit code 16, got {result.returncode}"
        assert "ERROR 2" in result.stdout or "Accessing Source Directory" in result.stdout, "Expected error message in STDOUT!"
        assert result.stderr == "", f"Expected STDERR to be empty, but got: {result.stderr}"
        print("[SUCCESS] VERIFICATION SUCCESSFUL: Robocopy routes fatal errors to STDOUT. STDERR is completely empty.")
    except AssertionError as e:
        print(f"[FAILED] VERIFICATION FAILED: {e}")

if __name__ == "__main__":
    test_robocopy_streams()
