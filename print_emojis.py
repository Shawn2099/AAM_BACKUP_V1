import os
import unicodedata

def print_emojis(path):
    with open(path, 'r', encoding='utf-8') as file:
        content = file.read()
        for i, char in enumerate(content):
            try:
                name = unicodedata.name(char)
                if 'EMOJI' in name or 'SMILING' in name or 'FACE' in name or name.startswith('WINK') or 'CROSS MARK' in name or 'CHECK MARK' in name:
                    # print line
                    lines = content.split('\n')
                    # find which line it's on
                    char_count = 0
                    for line_num, line in enumerate(lines):
                        char_count += len(line) + 1 # +1 for newline
                        if i < char_count:
                            print(f"{path}:{line_num+1}: {line.strip()}")
                            break
            except ValueError:
                pass

for f in ['./collect_config_data.py', './flow.py', './deploy/test_config.py']:
    print_emojis(f)
