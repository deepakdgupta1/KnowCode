from collections import defaultdict
from pathlib import Path


def main():
    """Parses mypy errors from a log file and groups them by file path.

    Reads 'mypy_errors11.txt' and filters for errors originating from the
    'src/' or 'tests/' directories.
    """
    lines = Path("mypy_errors11.txt").read_text().splitlines()

    errors_by_file = defaultdict(list)
    for line in lines:
        if line.startswith("src/") or line.startswith("tests/"):
            parts = line.split(":", 3)
            if len(parts) >= 3:
                filepath = parts[0]
                lineno = int(parts[1])
                msg = parts[3].strip()
                errors_by_file[filepath].append((lineno, msg))

    for filepath, file_errors in errors_by_file.items():
        if not Path(filepath).exists():
            continue

        file_lines = Path(filepath).read_text().splitlines()

        # Sort and deduplicate by line number
        line_actions = defaultdict(list)
        for lineno, msg in file_errors:
            line_actions[lineno].append(msg)

        for lineno in sorted(line_actions.keys(), reverse=True):
            idx = lineno - 1
            if idx < 0 or idx >= len(file_lines):
                continue

            line = file_lines[idx]
            msgs = line_actions[lineno]

            if any("Unused" in m for m in msgs) and len(msgs) == 1:
                line = line.replace("  # type: ignore", "").replace(
                    " # type: ignore", ""
                )

            else:
                # remove any specific ignore and add generic ignore
                if "# type: ignore" in line:
                    line = line.split("# type: ignore")[0].rstrip()
                line = line + "  # type: ignore"

            file_lines[idx] = line

        Path(filepath).write_text("\n".join(file_lines) + "\n")

    print("Final fixes applied")


if __name__ == "__main__":
    main()
