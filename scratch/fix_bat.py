import re
import os

path = r"deploy\server_discovery.bat"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    if line.strip().startswith("echo") and not line.strip().startswith("echo.") and not line.strip().startswith("echo {") and not line.strip().startswith("echo   ") and not line.strip().startswith("echo \""):
        # We only want to escape '(' and ')' if they are NOT already escaped
        # and are part of standard text
        # This is a bit tricky, but replacing '(' with '^(' and ')' with '^)' where there's no '^' before it.
        # Let's just do a simple replace since it's just echo strings
        l = line
        l = re.sub(r'(?<!\^)\(', '^(', l)
        l = re.sub(r'(?<!\^)\)', '^)', l)
        new_lines.append(l)
    else:
        new_lines.append(line)

with open(path, "w", encoding="utf-8") as f:
    f.writelines(new_lines)

print("Fixed batch file parentheses.")
