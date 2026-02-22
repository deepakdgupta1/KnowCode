from pathlib import Path

def main():
    for f in Path('src').rglob('*.py'):
        add_any(f)
    for f in Path('tests').rglob('*.py'):
        add_any(f)

def add_any(f: Path):
    text = f.read_text()
    if (' Any' in text or 'Any:' in text or 'Any =' in text or '[Any]' in text) and 'Any' not in [line for line in text.splitlines() if line.startswith('from typing import ') or line.startswith('import typing')]:
        lines = text.splitlines()
        
        # Find the first import line or past the docstring
        insert_idx = 0
        for i, line in enumerate(lines):
            if line.startswith('import ') or line.startswith('from '):
                if 'from __future__ ' not in line:
                    insert_idx = i
                    break
            if line.strip() and not line.startswith('"""') and not line.startswith('#'):
                # just past imports
                pass

        lines.insert(insert_idx, "from typing import Any")
        f.write_text("\n".join(lines) + "\n")
        print(f"Added Any to {f}")

if __name__ == '__main__':
    main()
