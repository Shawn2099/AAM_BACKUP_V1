import yaml
from models.config import AppConfig

snippet = """
paths:
  source_drive: "D:\\Data\\FY26-27"
  lan_destination: "\\\\<TARGET_IP>\\share\\FY26-27"
wol:
  mac_address: "11:22:33:44:55:66"
  server_ip: "192.168.1.100"
dashboard:
  bind_address: "192.168.1.100"
"""

snippet_data = yaml.safe_load(snippet)

with open("config.yaml", "r") as f:
    full_config = yaml.safe_load(f)

full_config.update(snippet_data) # update with snippet
# Note: paths needs to be a deeper update because we only provided 2 keys in paths.
# Let's do a deep update
for k, v in snippet_data.items():
    if isinstance(v, dict) and k in full_config:
        full_config[k].update(v)
    else:
        full_config[k] = v

try:
    AppConfig(**full_config)
    print("SUCCESS: Validated by Pydantic!")
except Exception as e:
    print("ERROR:", e)
