"""S2-14 verification: live bucket lifecycle must equal deploy/gcs_lifecycle.json."""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
live = json.load(open(r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\_s3_pre_state\bucket_desc.json", encoding="utf-8-sig"))
deploy = json.load(open(r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1\deploy\gcs_lifecycle.json", encoding="utf-8"))

live_rules = live.get("lifecycle_config", {}).get("rule", [])
dep_rules = deploy.get("rule", [])
print(f"live rules: {len(live_rules)}, deploy rules: {len(dep_rules)}")
ok = len(live_rules) == len(dep_rules)
for i, (lr, dr) in enumerate(zip(live_rules, dep_rules)):
    same = json.dumps(lr, sort_keys=True) == json.dumps(dr, sort_keys=True)
    ok = ok and same
    tag = "MATCH" if same else "DIFF "
    print(f"  rule {i}: {tag}  {json.dumps(lr.get('condition'))} -> {lr.get('action', {}).get('type')}")
    if not same:
        print("     live  :", json.dumps(lr, sort_keys=True))
        print("     deploy:", json.dumps(dr, sort_keys=True))

live_norm = json.dumps(sorted(json.dumps(r, sort_keys=True) for r in live_rules))
dep_norm = json.dumps(sorted(json.dumps(r, sort_keys=True) for r in dep_rules))
print("LIVE == DEPLOY (exact, order-insensitive):", live_norm == dep_norm)
sys.exit(0 if (ok and live_norm == dep_norm) else 1)
