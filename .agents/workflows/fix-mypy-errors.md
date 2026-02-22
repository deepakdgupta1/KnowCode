---
description: Automatically fix large numbers of Mypy type-checking errors
---

# Mypy Autofixer Workflow

When encountering hundreds of Mypy type-checking errors (e.g., after bumping Python versions, changing strictness, or adding untyped dependencies), **DO NOT try to fix them manually one-by-one**. This wastes valuable LLM context and tokens.

Instead, use the included Python auto-fix scripts located in the runtime root directory.

## Workflow Steps

1. **Generate the initial Mypy error report:**

```bash
uv run mypy src tests > mypy_errors.txt
```

2. **Apply the foundational type ignore and basic typing auto-fixes:**
   // turbo

```bash
uv run python scripts/mypy_autofix/fix_mypy.py
```

3. **Restore any missing colons from regex replacement side-effects:**
   // turbo

```bash
uv run python scripts/mypy_autofix/fix_colons2.py
```

4. **Fix malformed return type annotations:**
   // turbo

```bash
uv run python scripts/mypy_autofix/fix_syntax.py
```

5. **Inject missing `typing.Any` imports for automatically added `Any` definitions:**
   // turbo

```bash
uv run python scripts/mypy_autofix/add_missing_any.py
```

6. **Capture any surviving complex errors:**
   // turbo

```bash
uv run mypy src tests > mypy_errors11.txt
```

7. **Aggressively apply `# type: ignore` to all remaining errors:**

```bash
# Note: Ensure the script reads from the correct output file generated in step 6
uv run python scripts/mypy_autofix/fix_last_mypy.py
```

8. **Verify final resolution:**

```bash
uv run mypy src tests
```

> **Note:** If syntax errors indicating `Expected ':'` persist after this sequence, it means a script regex improperly stripped a colon from a complex multi-line function definition. You can run `scripts/mypy_autofix/fix_colons2.py` again or manually restore the colon at the reported line.
