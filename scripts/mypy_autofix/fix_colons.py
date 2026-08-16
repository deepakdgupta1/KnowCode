from pathlib import Path


def main():
    """Recursively scans all Python files in the current directory, modifying function definitions that meet specific trailing conditions."""
    for f in Path(".").rglob("*.py"):
        if f.is_file():
            changed = False
            lines = f.read_text().splitlines()
            for i, line in enumerate(lines):
                if line.lstrip().startswith("def ") and line.endswith(
                    ")  # type: ignore"
                ):
                    lines[i] = line.replace(")  # type: ignore", "):  # type: ignore")
                    changed = True
            if changed:
                f.write_text("\n".join(lines) + "\n")
                print(f"Fixed {f}")


if __name__ == "__main__":
    main()
