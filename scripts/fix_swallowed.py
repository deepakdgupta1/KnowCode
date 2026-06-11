import os
import re

target_dir = "/Users/deepg/Desktop/KnowCode/src/knowcode"
pattern = re.compile(r'except Exception:\s+pass')
replacement = r'''except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Ignored exception: %s", e)'''

def fix_swallowed(path):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    new_content = pattern.sub(replacement, content)
    if new_content != content:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Fixed {path}")

for root, _, files in os.walk(target_dir):
    for file in files:
        if file.endswith('.py'):
            fix_swallowed(os.path.join(root, file))
