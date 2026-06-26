import os
import unicodedata

def contains_emoji(text):
    for char in text:
        try:
            name = unicodedata.name(char)
            if 'EMOJI' in name or 'SMILING' in name or 'FACE' in name or name.startswith('WINK') or 'CROSS MARK' in name or 'CHECK MARK' in name:
                return True
        except ValueError:
            pass
    return False

for root, dirs, files in os.walk('.'):
    if '.venv' in root or '.git' in root or '__pycache__' in root:
        continue
    for f in files:
        if f.endswith(('.py', '.js', '.html', '.css')):
            path = os.path.join(root, f)
            with open(path, 'r', encoding='utf-8') as file:
                content = file.read()
                if contains_emoji(content):
                    print(f"Found emoji in {path}")
