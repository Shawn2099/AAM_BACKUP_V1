import asyncio
from core.manifest import ManifestDB
from core.report import generate_report_html
import tempfile
import os

async def main():
    db = ManifestDB("test_extreme.db")
    
    # 1. Extreme error length (10 MB of error)
    long_error = "A" * 10_000_000
    
    # 2. Extreme run duration and large numbers
    # 3. Weird characters
    weird_chars = "ñáéíóú \n \t \\ / ' \" < > & * ( ) % $ # @ ! ~ ` ^ - _ + = { } [ ] | ; : , . ?"
    
    # test inserting first run
    db.insert_run({
        "run_id": "extreme_test_run_1",
        "mode": "lan",
        "started_at": "2026-06-25T10:00:00Z",
        "ended_at": "2026-06-25T11:00:00Z",
        "status": "failed",
        "duration_seconds": 999999999.99,
        "files_copied": 999999999,
        "bytes_copied": 999999999999999999,
        "error_message": long_error + weird_chars
    })
    print("Inserted extreme run.")
    
    # Generate report
    db.insert_run({
        "run_id": "normal_test_run_1",
        "mode": "cloud",
        "started_at": "2026-06-26T10:00:00Z",
        "status": "success",
        "duration_seconds": 1.2,
        "files_copied": 10,
        "bytes_copied": 1000,
        "error_message": None
    })
    
    try:
        report_html = generate_report_html(db, "Extreme Firm", 7, "Weekly", is_email=True)
        print(f"Report generated successfully.")
        print(f"HTML size: {len(report_html)} bytes")
        
        # Verify truncation in HTML
        if len(report_html) > 10_000_000:
            print("WARNING: HTML report is too large, truncation might not have worked!")
        else:
            print("HTML report size is reasonable.")
            
        from core.report import _generate_csv_data
        runs = db.get_runs_since(7)
        csv_bytes = _generate_csv_data(runs)
        print(f"CSV size: {len(csv_bytes)} bytes")
            
    except Exception as e:
        print(f"FAILED: {e}")
    finally:
        db.close()
        # cleanup
        if os.path.exists("test_extreme.db"):
            os.remove("test_extreme.db")

if __name__ == "__main__":
    asyncio.run(main())
