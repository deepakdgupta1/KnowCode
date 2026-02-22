import sys
from pathlib import Path

def main():
    for f in Path('.').rglob('*.py'):
        if f.is_file():
            content = f.read_text()
            if ') -> Any:' in content or ') -> None:' in content:
                content = content.replace(') -> Any:', ') -> Any:')
                content = content.replace(') -> None:', ') -> None:')
                f.write_text(content)
                print(f"Fixed {f}")

if __name__ == '__main__':
    main()
