import os
import re
import subprocess
import json

def find_markdown_files(root_dir):
    md_files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        if '.git' in dirpath or 'node_modules' in dirpath or 'venv' in dirpath or '.venv' in dirpath or 'site' in dirpath or 'knowcode_index' in dirpath or '.pytest_cache' in dirpath:
            continue
        for f in filenames:
            if f.endswith('.md'):
                md_files.append(os.path.join(dirpath, f))
    return md_files

def extract_code_references(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    # Match inline code `something`
    inline_code = re.findall(r'`([^`\s]+)`', content)
    
    symbols = set()
    for code in inline_code:
        # Check if it looks like a file path
        if '.py' in code or '/' in code:
            symbols.add(code.strip('()'))
        elif re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', code):
            if code not in ('true', 'false', 'null', 'None', 'True', 'False', 'dict', 'list', 'str', 'int'):
                symbols.add(code.strip('()'))
                
    return list(symbols)

def check_symbol_exists(root_dir, symbol):
    # Ripgrep search in src/ and apps/ directories
    try:
        # Search for the exact symbol word in python files
        cmd = ['rg', '-q', '-t', 'py', '-w', symbol, os.path.join(root_dir, 'src'), os.path.join(root_dir, 'apps')]
        result = subprocess.run(cmd)
        return result.returncode == 0
    except Exception:
        return False

def check_file_exists(root_dir, filepath):
    # Try finding the exact path from src/knowcode or apps/agent-gateway/src/agent_gateway
    # Also just find it recursively by basename
    basename = os.path.basename(filepath)
    cmd = ['find', os.path.join(root_dir, 'src'), os.path.join(root_dir, 'apps'), '-name', basename]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = res.stdout.strip()
    if not out:
        return False
    # Check if the path suffix matches
    for line in out.splitlines():
        if filepath in line:
            return True
    return False

if __name__ == "__main__":
    root_dir = "/Users/deepg/Desktop/KnowCode"
    md_files = find_markdown_files(root_dir)
    print(f"Found {len(md_files)} markdown files.")
    
    issues = {}
    for md in md_files:
        symbols = extract_code_references(md)
        missing = []
        for sym in symbols:
            if '.py' in sym or '/' in sym:
                if not check_file_exists(root_dir, sym):
                    missing.append(sym)
            else:
                if not check_symbol_exists(root_dir, sym):
                    missing.append(sym)
        if missing:
            issues[os.path.relpath(md, root_dir)] = missing

    with open('md_issues_v2.json', 'w') as f:
        json.dump(issues, f, indent=2)
    print("Done. Results in md_issues_v2.json")
