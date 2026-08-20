"""S2-14: read the live bucket's current lifecycle (read-only)."""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
p = r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\_s3_pre_state\bucket_desc.json"
try:
    data = json.load(open(p, encoding="utf-8-sig"))
except Exception as e:
    print("parse fail:", e)
    try:
        print(open(p, encoding="utf-8", errors="replace").read()[:500])
    except Exception as e2:
        print("no file:", e2)
    raise SystemExit(1)

print("name:", data.get("name"))
print("versioning:", data.get("versioning"))
lc = data.get("lifecycle", {})
print("current rules:")
for r in lc.get("rule", []):
    print("  ", json.dumps(r.get("action")), "->", json.dumps(r.get("condition")))
