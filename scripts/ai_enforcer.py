#!/usr/bin/env python3
"""
AI Enforcer for KnowCode Documentation
Detects missing docstrings and auto-generates them using an LLM.

This script is designed to be run locally or within a CI/CD pipeline (e.g., GitHub Actions).
It reads target files from command-line arguments, or falls back to locally staged files.

Configuration (Environment Variables):
- LLM_API_KEY / LITELLM_MASTER_KEY: API key for the LLM provider. Falls back to macOS Keychain locally.
- LLM_API_URL / LITELLM_URL: The OpenAI-compatible completions endpoint. Defaults to localhost proxy.
- LLM_MODEL: The model string to use for generation. Defaults to 'glm-5-turbo'.
"""

import argparse
import ast
import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from typing import List, Tuple

# Configuration
LITELLM_URL = os.environ.get(
    "LLM_API_URL",
    os.environ.get("LITELLM_URL", "http://127.0.0.1:4000/chat/completions"),
)
MODEL = os.environ.get("LLM_MODEL", "glm-5-turbo")


def get_litellm_key() -> str:
    """Fetch the LLM API key from environment or keychain.

    Checks the environment variables first for an API key. If not found,
    it attempts to retrieve it from the macOS Keychain securely.

    Returns:
        str: The retrieved API key, or an empty string if not found.
    """
    if key := os.environ.get("LLM_API_KEY", os.environ.get("LITELLM_MASTER_KEY")):
        return key

    try:
        result = subprocess.run(
            [
                "/Users/deepg/.local/bin/manage-keychain.sh",
                "get",
                "LiteLLMProxy",
                "LITELLM_MASTER_KEY",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"[AI Enforcer] Failed to fetch LiteLLM key from keychain: {e}")
        return ""


def get_staged_python_files() -> List[str]:
    """Get a list of currently staged python files.

    Executes a git command to find files that are staged for commit,
    filtering specifically for added, copied, or modified Python files.

    Returns:
        List[str]: A list of relative file paths to staged Python files.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            check=True,
        )
        files = result.stdout.splitlines()
        return [f for f in files if f.endswith(".py") and os.path.isfile(f)]
    except subprocess.CalledProcessError:
        return []


def generate_docstring(signature: str, body_preview: str, api_key: str) -> str:
    """Call the LLM to generate a Google-style docstring.

    Constructs a prompt with the function's signature and a preview of its
    body, then sends it to the configured LLM API to generate a concise,
    Google-style docstring.

    Args:
        signature (str): The signature of the Python function or class.
        body_preview (str): The first few lines of the function body for context.
        api_key (str): The API key for authentication with the LLM provider.

    Returns:
        str: The generated docstring without quotes, or an empty string on failure.
    """
    prompt = f"""
Generate a concise, Google-style docstring for the following Python function.
Return ONLY the docstring text itself, without any quotes or backticks around it.
Do NOT include the function signature or any other code.

Function Signature:
{signature}

Body Preview:
{body_preview}
"""

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    data = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are an expert Python developer writing docstrings.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 1000,
    }

    req = urllib.request.Request(LITELLM_URL, json.dumps(data).encode("utf-8"), headers)

    max_retries = 5
    base_delay = 2

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
                print(f"[AI Enforcer] LLM Result: {result}")
                content = result["choices"][0]["message"]["content"].strip()

                # Clean up potential markdown formatting from the model
                if content.startswith("```"):
                    lines = content.splitlines()
                    if len(lines) > 2:
                        content = "\\n".join(lines[1:-1])

                # Clean up literal quotes if the model wrapped it
                if content.startswith('"""') and content.endswith('"""'):
                    content = content[3:-3].strip()
                print(f"[AI Enforcer] LLM returned docstring: {repr(content)}")
                return content
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt)
                    print(
                        f"[AI Enforcer] Rate limited (429). Retrying in {delay} seconds..."
                    )
                    time.sleep(delay)
                    continue
                else:
                    print("[AI Enforcer] Max retries reached for 429 Rate Limit.")
            else:
                print(f"[AI Enforcer] HTTP Error {e.code}: {e.reason}")
            break
        except Exception as e:
            print(f"[AI Enforcer] Error calling LLM: {e}")
            import traceback

            traceback.print_exc()
            break

    return ""


def apply_docstrings(filepath: str, api_key: str) -> bool:
    """Find missing docstrings in a file and apply them.

    Parses the Python file into an Abstract Syntax Tree (AST) to identify
    functions, async functions, and classes that lack a docstring. For each
    missing docstring, it calls the LLM to generate one and inserts it into
    the source code with proper indentation.

    Args:
        filepath (str): The path to the Python file to process.
        api_key (str): The API key to use for LLM generation.

    Returns:
        bool: True if any docstrings were added to the file, False otherwise.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()

    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        print(f"[AI Enforcer] Syntax error in {filepath}, skipping.")
        return False

    lines = source.splitlines()
    insertions: List[
        Tuple[int, str, int]
    ] = []  # (line_index_to_insert_before, docstring, indentation)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not ast.get_docstring(node):
                # We need to find the start of the body to insert the docstring.
                # node.body[0].lineno is 1-indexed.
                if not node.body:
                    continue

                body_start_line = node.body[0].lineno
                col_offset = node.body[0].col_offset

                # Extract signature and a bit of body for context
                start_line = node.lineno - 1
                end_line = min(body_start_line + 5, len(lines))
                preview = "\n".join(lines[start_line:end_line])

                signature = lines[start_line : body_start_line - 1]

                print(
                    f"[AI Enforcer] Generating docstring for {node.name} in {filepath}..."
                )
                docstring = generate_docstring("\n".join(signature), preview, api_key)

                if docstring:
                    insertions.append((body_start_line - 1, docstring, col_offset))

    if not insertions:
        return False

    # Sort insertions in reverse order to not mess up line indices
    insertions.sort(key=lambda x: x[0], reverse=True)

    for line_idx, docstring, indent in insertions:
        indent_str = " " * indent
        # Format the docstring
        doc_lines = docstring.splitlines()
        if len(doc_lines) == 1:
            formatted = f'{indent_str}"""{doc_lines[0]}"""'
        else:
            formatted_lines = [f'{indent_str}"""{doc_lines[0]}']
            for dl in doc_lines[1:]:
                formatted_lines.append(f"{indent_str}{dl}" if dl else "")
            formatted_lines.append(f'{indent_str}"""')
            formatted = "\n".join(formatted_lines)

        lines.insert(line_idx, formatted)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return True


def main():
    """Main entry point for the AI docstring generation process.

    Parses command-line arguments to determine target files and configuration.
    Validates the presence of an API key, falls back to staged files if no files
    are provided, processes each file to inject missing docstrings, and
    optionally re-stages the modified files via git.
    """
    parser = argparse.ArgumentParser(description="AI Enforcer for KnowCode Docstrings")
    parser.add_argument(
        "files",
        nargs="*",
        help="List of files to process. If empty, falls back to staged files.",
    )
    parser.add_argument(
        "--no-git-add",
        action="store_true",
        help="Do not run git add on modified files (useful for CI).",
    )
    args = parser.parse_args()

    api_key = get_litellm_key()
    if not api_key:
        print("[AI Enforcer] Skipping AI docstring generation (no API key).")
        return

    files_to_process = args.files
    if not files_to_process:
        print("[AI Enforcer] No files provided via CLI. Falling back to staged files.")
        files_to_process = get_staged_python_files()

    if not files_to_process:
        print("[AI Enforcer] No python files to process.")
        return

    modified_files = []
    for filepath in files_to_process:
        if not filepath.endswith(".py") or not os.path.isfile(filepath):
            continue
        if apply_docstrings(filepath, api_key):
            modified_files.append(filepath)

    if modified_files:
        print(f"[AI Enforcer] Modified files: {', '.join(modified_files)}")
        if not args.no_git_add:
            print("[AI Enforcer] Re-staging modified files...")
            subprocess.run(["git", "add"] + modified_files, check=True)


if __name__ == "__main__":
    main()
