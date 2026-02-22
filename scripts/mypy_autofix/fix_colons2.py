import re
from pathlib import Path

def main():
    pattern = re.compile(r'^(\s*def\s+[a-zA-Z0-9_]+\s*\([^)]*\))\s+# type: ignore')
    for f in Path('.').rglob('*.py'):
        if f.is_file():
            changed = False
            lines = f.read_text().splitlines()
            for i, line in enumerate(lines):
                if pattern.match(line.lstrip()):
                    lines[i] = pattern.sub(r'\1:  # type: ignore', line)
                    changed = True
            if changed:
                f.write_text("\n".join(lines) + "\n")
                print(f"Fixed {f}")

if __name__ == '__main__':
    main()
